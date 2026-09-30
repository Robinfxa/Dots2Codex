"""Separate remote-object Responses facade. No model execution, no Drive sync."""
import json
import math
import fcntl
import os
import re
import threading
import time
from pathlib import Path
from .model import ProtocolError, canonical, hash_bytes, require
from .backend import read_private_file
from .session import Controller, _save
from .wire import ResponsesHandler, ResponsesServer, QueueError, protocol, validate_text_request


class _Wake:
    def __init__(self, stop): self.stop,self.delay = stop,.25
    def wait(self):
        self.stop.wait(self.delay); self.delay=min(2,self.delay*2)


class RemoteStore:
    def __init__(self, owner):
        self.owner = owner
        # Frozen handler only uses queue.wake, not a POSIX queue implementation.
        self.queue = type('RemotePollWake',(),{'wake':_Wake(owner.stop)})()

    def _call(self, fn, *args):
        try: return fn(*args)
        except ProtocolError as exc: raise QueueError(str(exc)) from None

    def enqueue(self, request, session, deadline):
        return self._call(self._enqueue,request,session,deadline)

    def _enqueue(self, request, session, deadline):
        validate_text_request(request)
        owner=self.owner
        require(session == owner.controller.pin.body['identity']['session_id'], 'session_scope_mismatch')
        identity=getattr(owner.context,'identity',None)
        require(identity is not None,'canonical_runtime_session_required')
        old=owner.read_state()
        key=hash_bytes(canonical({'client':identity,'request':request}))
        with owner.controller.journal.locked() as s:
            require(key not in s['requests'],'request_replay')
        require(old['binding'] is None or old['binding']==identity,'session_scope_mismatch')
        # Full-history confirmation must contain the exact prior emitted assistant item.
        for rid,job in old['jobs'].items():
            if job['status'] == 'confirmed': continue
            obj=owner.controller.result(rid)
            if obj is None: raise ProtocolError('previous_delivery_unconfirmed')
            expected=protocol.validate_result({'kind':'message','text':obj.body['payload']['text']},request,rid)
            if expected not in request['input']:
                raise ProtocolError('previous_delivery_unconfirmed')
            owner.controller.record_delivery(rid,obj.oid,'canonical-client-full-history:'+hash_bytes(canonical(request)))
            job.update(status='confirmed',result=obj.oid)
        old['binding']=identity
        owner.save_state(old)
        rid=owner.controller.submit_request(request,key)
        old['jobs'][rid]={'status':'accepted','deadline':time.time()+deadline,'result':None}
        owner.save_state(old)
        return {'job_id':rid}

    def response_state(self, rid):
        return self._call(self._response_state,rid)

    def _response_state(self,rid):
        state=self.owner.read_state();job=state['jobs'][rid]
        result=self.owner.controller.result(rid)
        if result is not None:
            request=result.body['links']['request']
            with self.owner.controller.journal.locked() as s:
                wire_request=s['objects'][request]['body']['payload']['responses_request']
            item=protocol.validate_result({'kind':'message','text':result.body['payload']['text']},wire_request,rid)
            job['result']=result.oid;self.owner.save_state(state)
            return {'state':'completed','wire_item':item}
        if time.time() >= job['deadline']:
            return {'state':'expired','error':{'code':'remote_wait_budget_expired'}}
        return {'state':'running'}

    def disconnect(self,rid,*_):
        def update():
            state=self.owner.read_state()
            if rid in state['jobs'] and state['jobs'][rid]['status']!='confirmed':
                state['jobs'][rid]['status']='uncertain'
                self.owner.save_state(state)
        return self._call(update)

    def delivery(self,rid,value):
        def update():
            require(value=='delivered','unsupported_delivery_status')
            state=self.owner.read_state()
            state['jobs'][rid]['status']='socket_flushed'
            self.owner.save_state(state)
            # Deliberately NOT a delivery receipt. User/client observation is separate.
        return self._call(update)


class RemoteHandler(ResponsesHandler):
    def do_POST(self):
        if self.headers.get_all('session_id') or self.headers.get_all('thread_id'):
            self.send_json(400,'ambiguous_legacy_session_header');return
        identities={}
        for name in ('session-id','thread-id'):
            values=self.headers.get_all(name) or []
            pattern=r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}' if name=='thread-id' else r'[!-~]{1,256}'
            if len(values)!=1 or not re.fullmatch(pattern,values[0]):
                self.send_json(400,'canonical_runtime_session_required');return
            identities[name]=values[0]
        traces=self.headers.get_all('x-client-request-id') or []
        if traces and (len(traces)!=1 or traces[0]!=identities['thread-id']):
            self.send_json(400,'conflicting_client_request_id');return
        owner=self.server.remote_owner
        self.headers['session_id']=owner.controller.pin.body['identity']['session_id']
        owner.context.identity=identities
        try:super().do_POST()
        finally:del owner.context.identity


class RemoteResponsesFacade:
    def __init__(self, controller, *, port=0, request_deadline=60):
        require(isinstance(controller,Controller),'controller_required')
        require(type(port) is int and 0 <= port <= 65535 and
                type(request_deadline) in (int,float) and .1 <= request_deadline <= 180,
                'invalid_facade_limits')
        self.controller=controller;self.context=threading.local();self.stop=threading.Event()
        self.path=controller.journal.root/'remote-facade-state.json'
        self.lock=threading.RLock();self.closed=False
        with controller.journal.locked() as s:
            if not self.path.exists():
                require(not s['requests'],'facade_state_missing_after_requests')
                _save(self.path,{'pin':controller.pin.oid,'binding':None,'jobs':{}})
        self.read_state()
        self.lock_fd=os.open(controller.journal.root/'remote-facade.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:fcntl.flock(self.lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock_fd);raise ProtocolError('facade_already_running') from None
        try:
            self.server=ResponsesServer(RemoteStore(self),port,request_deadline)
            self.server.remote_owner=self;self.server.RequestHandlerClass=RemoteHandler
            self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.02},daemon=True)
        except Exception:
            os.close(self.lock_fd);raise

    def read_state(self):
        with self.lock:
            s=json.loads(read_private_file(self.path,131072))
            require(isinstance(s,dict) and set(s)=={'pin','binding','jobs'} and
                    s['pin']==self.controller.pin.oid and isinstance(s['jobs'],dict),'invalid_facade_state')
            binding=s['binding']
            if binding is not None:
                require(isinstance(binding,dict) and set(binding)=={'session-id','thread-id'} and
                        isinstance(binding['session-id'],str) and re.fullmatch(r'[!-~]{1,256}',binding['session-id']) and
                        isinstance(binding['thread-id'],str) and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',binding['thread-id']),
                        'invalid_facade_binding')
            require(not s['jobs'] or binding is not None,'facade_binding_missing')
            with self.controller.journal.locked() as state:
                require(set(s['jobs'])==set(state['requests'].values()),'facade_request_index_mismatch')
                for rid,job in s['jobs'].items():
                    require(isinstance(job,dict) and set(job)=={'status','deadline','result'} and
                            job['status'] in {'accepted','socket_flushed','confirmed','uncertain'} and
                            type(job['deadline']) in (int,float) and math.isfinite(job['deadline']) and
                            0 < job['deadline'] < 1e12,'invalid_facade_job')
                    request=state['objects'][rid]['body']
                    require('responses_request' in request['payload'],'invalid_facade_request')
                    key=hash_bytes(canonical({'client':binding,'request':request['payload']['responses_request']}))
                    require(state['requests'].get(key)==rid,'facade_request_binding_mismatch')
                    if job['result'] is not None:
                        require(job['result'] in state['objects'] and
                                state['objects'][job['result']]['body']['kind']=='result' and
                                state['objects'][job['result']]['body']['links']['request']==rid,
                                'invalid_facade_result')
                    if job['status']=='confirmed':
                        receipt=state['deliveries'].get(str(request['seq']))
                        require(receipt is not None and job['result'] is not None and
                                state['objects'][receipt]['body']['links']=={'request':rid,'result':job['result']},
                                'facade_receipt_missing')
            return s

    def save_state(self,state):
        with self.lock:_save(self.path,state)

    @property
    def base_url(self):return self.server.base_url

    def start(self):self.thread.start();return self

    def confirm_delivery(self,request_id,evidence):
        # Operator must actually observe the client result; cannot infer from socket flush.
        require(self.server.inflight.acquire(blocking=False),'request_inflight')
        try:
            state=self.read_state();require(request_id in state['jobs'],'unknown_facade_request')
            result=self.controller.result(request_id);require(result is not None,'result_not_available')
            if state['jobs'][request_id]['status']=='confirmed':
                with self.controller.journal.locked() as s:
                    return s['deliveries'][str(result.body['seq'])]
            receipt=self.controller.record_delivery(request_id,result.oid,evidence)
            state['jobs'][request_id].update(status='confirmed',result=result.oid)
            self.save_state(state);return receipt
        finally:self.server.inflight.release()

    def close(self):
        if self.closed:return
        self.closed=True;self.stop.set();self.server.stop_event.set()
        if self.thread.is_alive():self.server.shutdown()
        self.server.server_close()
        if self.thread.ident:self.thread.join(3)
        os.close(self.lock_fd)
