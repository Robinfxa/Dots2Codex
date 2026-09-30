#!/usr/bin/env python3
"""Loopback Responses facade backed only by shared files; no model/command executor.
HTTP handler reused from the previously audited local MCP queue prototype.
"""
import argparse,http.server,os,re,select,socket,threading,time
from file_queue import FileQueue,Producer,QueueError,protocol
MAX_REQUEST=protocol.MAX_REQUEST

class StoreAdapter:
    def __init__(self,queue):self.queue=queue;self.scope={'owner':queue.meta['owner'],'session':queue.meta['session']}
    def enqueue(self,request,session,deadline):
        if session!=self.scope['session']:raise QueueError('session_scope_mismatch')
        state=self.queue.enqueue(request,**self.scope,timeout=deadline);return {'job_id':state['id']}
    def response_state(self,jid):
        state=self.queue.get(jid,**self.scope)
        if state['state']=='completed':
            with self.queue.locked():raw=self.queue.load(jid)
            state['wire_item']=protocol.validate_result(state['result'],raw['request'],jid)
        return state
    def disconnect(self,jid,*_):self.queue.cancel(jid,**self.scope,reason='client_disconnected')
    def delivery(self,jid,value):self.queue.delivery(jid,**self.scope,value=value)

class ResponsesServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    def __init__(self, store, port=0, deadline=300):
        self.store, self.deadline = store, deadline
        self.stop_event = threading.Event()
        self.clients, self.state_lock = set(), threading.Lock()
        self.inflight = threading.Lock()
        self.delivery_started = threading.Event()
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(("127.0.0.1", port), ResponsesHandler)
    @property
    def base_url(self): return f"http://127.0.0.1:{self.server_port}/v1"
    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request);return
        with self.state_lock:
            if self.stop_event.is_set():
                self.slots.release();self.shutdown_request(request);return
            self.clients.add(request)
        try:super().process_request(request,address)
        except Exception:
            with self.state_lock:self.clients.discard(request)
            self.slots.release();raise
    def process_request_thread(self, request, address):
        try:super().process_request_thread(request,address)
        finally:
            with self.state_lock:self.clients.discard(request)
            self.slots.release()
    def handle_error(self,*_):pass
    def server_close(self):
        with self.state_lock:
            self.stop_event.set();clients=list(self.clients)
        for sock in clients:
            try:sock.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            sock.close()
        super().server_close()


class ResponsesHandler(http.server.BaseHTTPRequestHandler):
    protocol_version="HTTP/1.1"
    def setup(self):
        super().setup();self.connection.settimeout(5)
    def log_message(self,*_):pass
    def send_json(self,status,code):
        data=protocol.encode({"error":{"type":"local_queue_error","code":code}})
        self.send_response(status);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(data)));self.send_header("Connection","close");self.end_headers();self.wfile.write(data);self.close_connection=True
    def do_POST(self):
        job=None;sent=False;acquired=False
        try:
            if self.headers.get_all("Host") != [f"127.0.0.1:{self.server.server_port}"] or self.headers.get_all("Origin") or self.headers.get_all("Authorization"):
                self.send_json(403,"invalid_local_headers");return
            if self.path!="/v1/responses":self.send_json(404,"unsupported_endpoint");return
            if self.headers.get_all("Transfer-Encoding") or self.headers.get("Content-Encoding","identity")!="identity":
                self.send_json(415,"unsupported_encoding");return
            if len(self.headers.get_all("Content-Type") or [])!=1 or self.headers.get_content_type()!="application/json":self.send_json(415,"unsupported_media_type");return
            lengths=self.headers.get_all("Content-Length") or []
            if len(lengths)!=1 or not re.fullmatch(r"[0-9]{1,10}",lengths[0]):self.send_json(411,"invalid_length");return
            length=int(lengths[0])
            if not 1<=length<=MAX_REQUEST:self.send_json(413,"request_too_large_or_empty");return
            acquired=self.server.inflight.acquire(timeout=.1)
            if not acquired and self.server.delivery_started.is_set():
                acquired=self.server.inflight.acquire(timeout=5.9)
            if not acquired:self.send_json(409,"request_inflight");return
            self.server.delivery_started.clear()
            body=self.rfile.read(length)
            if len(body)!=length:self.send_json(400,"truncated_body");return
            try:request=protocol.decode(body)
            except (ValueError,UnicodeError,RecursionError):self.send_json(400,"invalid_json");return
            for h in ("session_id","x-client-request-id"):
                if len(self.headers.get_all(h) or [])>1:self.send_json(400,"duplicate_session_header");return
            session=self.headers.get("session_id") or self.headers.get("x-client-request-id") or (request.get("prompt_cache_key") if isinstance(request,dict) else None)
            with self.server.state_lock:
                if self.server.stop_event.is_set():self.send_json(503,"service_stopping");return
                job=self.server.store.enqueue(request,session,self.server.deadline)["job_id"]
            while True:
                state=self.server.store.response_state(job)
                if state["state"]=="completed":break
                if state["state"] in ("failed","expired","cancelled"):
                    self.send_json(504 if state["state"]=="expired" else 502,state.get("error",{}).get("code","job_failed"));return
                if self.server.stop_event.is_set():raise QueueError("service_stopped")
                if select.select([self.connection],[],[],0)[0] and not self.connection.recv(1,socket.MSG_PEEK):
                    self.server.store.disconnect(job);return
                self.server.store.queue.wake.wait()
            item=state["wire_item"]
            events=[{"type":"response.output_item.done","output_index":0,"item":item},{"type":"response.completed","response":{"id":"resp_"+job,"end_turn":item["type"]=="message"}}]
            data=b"".join(b"event: "+e["type"].encode()+b"\ndata: "+protocol.encode(e)+b"\n\n" for e in events)
            self.server.delivery_started.set()
            self.send_response(200);self.send_header("Content-Type","text/event-stream");self.send_header("Content-Length",str(len(data)));self.send_header("Cache-Control","no-cache");self.send_header("X-Request-ID",job);self.send_header("Connection","close");self.end_headers()
            sent=True;self.wfile.write(data);self.wfile.flush();self.server.store.delivery(job,"delivered")
        except QueueError as exc:
            if job:
                try:self.server.store.disconnect(job,exc.code)
                except QueueError:pass
            try:self.send_json(409 if exc.code in ("request_replay","request_inflight","session_scope_mismatch") else 400,exc.code)
            except OSError:pass
        except (OSError,TimeoutError):
            if job:
                try:self.server.store.disconnect(job,"client_disconnected")
                except QueueError:pass
        except Exception:
            if job:
                try:self.server.store.disconnect(job,"internal_error")
                except QueueError:pass
            try:
                if not sent:self.send_json(500,"internal_error")
            except OSError:pass
        finally:
            if acquired:
                self.server.delivery_started.clear();self.server.inflight.release()
            self.close_connection=True

class LocalFacade:
    def __init__(self,root,port=0,deadline=180):
        self.queue=self.producer=self.server=self.thread=None;self.closed=False
        if type(port) is not int or not 0<=port<=65535 or not .05<=deadline<=1800:raise QueueError('invalid_limit')
        try:
            self.queue=FileQueue(root);self.producer=Producer(self.queue)
            self.server=ResponsesServer(StoreAdapter(self.queue),port,deadline)
            self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        except Exception:self.close();raise
    def start(self):self.thread.start();return self
    @property
    def base_url(self):return self.server.base_url
    def close(self):
        if self.closed:return
        self.closed=True
        if self.server:
            self.server.stop_event.set()
            if self.thread and self.thread.is_alive():self.server.shutdown()
            self.server.server_close()
        if self.thread and self.thread.ident is not None:self.thread.join(3)
        if self.producer:self.producer.close()
        if self.queue:self.queue.close()

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True);p.add_argument('--port',type=int,default=0);p.add_argument('--deadline',type=float,default=180);p.add_argument('--lifetime',type=float,default=300);a=p.parse_args();os.umask(0o077)
    if not .1<=a.lifetime<=3600:raise QueueError('invalid_lifetime')
    service=LocalFacade(a.root,a.port,a.deadline).start()
    protocol.atomic_json(service.queue.root/'facade.json',{'base_url':service.base_url,'session':service.queue.meta['session'],'pid':os.getpid()})
    print(protocol.encode({'base_url':service.base_url,'lifetime':a.lifetime}).decode(),flush=True)
    try:time.sleep(a.lifetime)
    except KeyboardInterrupt:pass
    finally:service.close()
if __name__=='__main__':main()
