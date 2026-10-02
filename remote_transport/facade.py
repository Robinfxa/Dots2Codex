"""Separate remote-object Responses facade. No model execution, no Drive sync."""
import contextlib
import json
import math
import fcntl
import os
import re
import select
import socket
import threading
import time
from pathlib import Path
from .model import ProtocolError, canonical, hash_bytes, require, MAX_WIRE_BYTES
from .backend import read_private_file
from .global_response import ResponseDeadline, parse_response_deadline, response_operation
from .session import Controller, _save
from .selection import pin_selection, validate_request_selection
from .wire import ResponsesHandler, ResponsesServer, QueueError, protocol, validate_text_request, validate_remote_request, result_item


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
        # Publish the controller request index and matching facade job as one
        # in-process observation window. read_state keeps its strict equality
        # check; status/stop readers cannot see the intermediate disk pair.
        with self.owner.lock:
            budget=getattr(self.owner.context,'response_deadline',None) or ResponseDeadline(
                self.owner.response_expires,self.owner.controller.pin.body['payload']['expires'],seconds=deadline)
            with response_operation(budget):
                return self._call(self._enqueue,request,session,deadline,budget)

    def _enqueue(self, request, session, deadline, budget):
        owner=self.owner
        budget.remaining('remote_wait_budget_expired')
        scope=owner.controller.pin.body['payload']['scope']
        validate_request_selection(request,pin_selection(owner.controller.pin))
        validate_remote_request(request,scope)
        require(session == owner.controller.pin.body['identity']['session_id'], 'session_scope_mismatch')
        identity=getattr(owner.context,'identity',None)
        require(identity is not None,'canonical_runtime_session_required')
        old=owner.read_state()
        key=hash_bytes(canonical({'client':identity,'request':request}))
        with owner.controller.journal.locked() as s:
            prior=s['requests'].get(key)
        if prior is not None:
            require(owner.long_session,'request_replay')
            require(old['binding']==identity and prior in old['jobs'],'session_scope_mismatch')
            # Re-emitting a tool intent after an uncertain socket outcome could
            # execute it twice on the client, so only text is automatically replayed.
            original=owner.response_budget(prior,old['jobs'][prior])
            if (owner.response_expires is not None or old['jobs'][prior].get('global_response',False)):
                budget.tighten(original.expires);budget.until=min(budget.until,original.until)
            budget.remaining('remote_wait_budget_expired')
            result=owner.controller.result(prior)
            budget.remaining('remote_wait_budget_expired')
            if result is not None:
                item=result_item(result.body['payload'],request,prior,scope)
                require(item['type']=='message' or not old['jobs'][prior].get('tool_emission_started',False),'tool_emission_outcome_unknown')
            # Return the SAME durable job. This never calls submit or native begin.
            return {'job_id':prior}
        require(not owner.closed_for_admission(),'session_closed')
        require(old['binding'] is None or old['binding']==identity,'session_scope_mismatch')
        # Validate the complete next input before any delivery mutation. Rebuild
        # each emitted item against its ORIGINAL request's advertised tool schema.
        with owner.controller.journal.locked() as journal:
            originals={rid:journal['objects'][rid]['body']['payload']['responses_request'] for rid in old['jobs']}
            allowed={}
            if scope=='responses_tools':
                for obj in journal['objects'].values():
                    body=obj['body']
                    if body['kind']!='result':continue
                    rid=body['links']['request']
                    if rid not in originals:continue
                    item=result_item(body['payload'],originals[rid],rid,scope)
                    if item['type'] in {'function_call','custom_tool_call'}:allowed[item['call_id']]=item
        if scope=='responses_tools':
            for item in request['input']:
                if item.get('type') in {'function_call','custom_tool_call'}:
                    require(allowed.get(item.get('call_id'))==item,'unissued_tool_history')
        confirmations=[]
        for rid,job in old['jobs'].items():
            if job['status']=='confirmed':continue
            budget.remaining('remote_wait_budget_expired')
            obj=owner.controller.result(rid)
            budget.remaining('remote_wait_budget_expired')
            require(obj is not None,'previous_delivery_unconfirmed')
            expected=result_item(obj.body['payload'],originals[rid],rid,scope)
            require(expected in request['input'],'previous_delivery_unconfirmed')
            if expected['type'] in {'function_call','custom_tool_call'}:
                output_type='function_call_output' if expected['type']=='function_call' else 'custom_tool_call_output'
                require(any(i.get('type')==output_type and i.get('call_id')==expected['call_id'] for i in request['input']),
                        'tool_outcome_unknown')
            confirmations.append((rid,obj))
        for rid,obj in confirmations:
            budget.remaining('remote_wait_budget_expired')
            owner.controller.record_delivery(rid,obj.oid,'canonical-client-full-history:'+hash_bytes(canonical(request)))
            old['jobs'][rid].update(status='confirmed',result=obj.oid)
        old['binding']=identity
        owner.save_state(old)
        # Publication latency consumes this same frozen budget. Persist the job
        # even if submit returns late; recovery must retain its exact request ID.
        budget.remaining('remote_wait_budget_expired')
        global_response=owner.response_expires is not None or getattr(owner.context,'global_response',False)
        try:rid=owner.controller.submit_request(request,key)
        except BaseException:
            # A provider may time out after the request index became durable.
            # Preserve that same job for exact recovery, never submit it again.
            with owner.controller.journal.locked() as saved:rid=saved['requests'].get(key)
            if rid is not None:
                owner.response_budgets[rid]=budget
                old['jobs'][rid]={'status':'uncertain','deadline':budget.expires,'result':None,
                    'tool_emission_started':False,'response_budget':budget.checkpoint(),'global_response':global_response}
                owner.save_state(old)
            raise
        owner.response_budgets[rid]=budget
        old['jobs'][rid]={'status':'accepted','deadline':budget.expires,'result':None,'tool_emission_started':False,
                         'response_budget':budget.checkpoint(),'global_response':global_response}
        owner.save_state(old)
        return {'job_id':rid}

    def response_state(self, rid):
        return self._call(self._response_state,rid)

    def _response_state(self,rid):
        state=self.owner.read_state();job=state['jobs'][rid]
        strict=(self.owner.response_expires is not None or job.get('global_response',False))
        if strict:
            try:self.owner.response_budget(rid,job).remaining('remote_wait_budget_expired')
            except ProtocolError as exc:return {'state':'expired','error':{'code':str(exc)}}
        with response_operation(self.owner.response_budget(rid,job)) if strict else contextlib.nullcontext():
            result=self.owner.controller.result(rid)
        if strict:
            try:self.owner.response_budget(rid,job).remaining('remote_wait_budget_expired')
            except ProtocolError as exc:return {'state':'expired','error':{'code':str(exc)}}
        if result is not None:
            request=result.body['links']['request']
            with self.owner.controller.journal.locked() as s:
                wire_request=s['objects'][request]['body']['payload']['responses_request']
            item=result_item(result.body['payload'],wire_request,rid,self.owner.controller.pin.body['payload']['scope'])
            job['result']=result.oid;self.owner.save_state(state)
            if strict:
                try:self.owner.response_budget(rid,job).remaining('remote_wait_budget_expired')
                except ProtocolError as exc:return {'state':'expired','error':{'code':str(exc)}}
            return {'state':'completed','wire_item':item}
        if self.owner.closed_for_admission():
            return {'state':'cancelled','error':{'code':'session_closed'}}
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
    def __init__(self, controller, *, port=0, request_deadline=60, long_session=False,
                 poll_interval=5, heartbeat_interval=15, response_expires=None):
        require(isinstance(controller,Controller),'controller_required')
        require(not long_session or hasattr(controller,'coordinator'),'long_session_requires_docs_cas')
        require(controller.pin.body['payload']['scope']=='text_only' or long_session,'tools_require_long_session_handler')
        require(type(port) is int and 0 <= port <= 65535 and
                type(request_deadline) in (int,float) and .1 <= request_deadline <= (28800 if long_session else 180),
                'invalid_facade_limits')
        require(type(long_session) is bool and type(poll_interval) in (int,float) and
                .05 <= poll_interval <= 60 and type(heartbeat_interval) in (int,float) and
                .05 <= heartbeat_interval <= 60,'invalid_stream_limits')
        self.long_session=long_session;self.poll_interval=poll_interval;self.heartbeat_interval=heartbeat_interval
        if response_expires is not None:ResponseDeadline(response_expires)
        self.response_expires=response_expires;self.response_budgets={}
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
            self.server.remote_owner=self;self.server.RequestHandlerClass=LongSessionHandler if long_session else RemoteHandler
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
                    require(self.controller.pin.body['payload']['scope']!='responses_tools' or
                            (isinstance(job,dict) and 'tool_emission_started' in job),
                            'tool_emission_marker_missing')
                    require(isinstance(job,dict) and set(job)=={'status','deadline','result'} | ({'tool_emission_started'} if 'tool_emission_started' in job else set()) | ({'response_budget'} if 'response_budget' in job else set()) | ({'global_response'} if 'global_response' in job else set()) and
                            type(job.get('global_response',False)) is bool and
                            type(job.get('tool_emission_started',False)) is bool and
                            job['status'] in {'accepted','socket_flushed','confirmed','uncertain','emission_reserved'} and
                            type(job['deadline']) in (int,float) and math.isfinite(job['deadline']) and
                            0 < job['deadline'] < 1e12,'invalid_facade_job')
                    if 'response_budget' in job:
                        require(job['response_budget'].get('expires')==job['deadline'],'facade_response_deadline_mismatch')
                        self.response_budget(rid,job)
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

    def response_budget(self,rid,job):
        with self.lock:
            if rid not in self.response_budgets:
                # Legacy generic facade jobs retain their absolute expiry. New
                # jobs also retain the same-host monotonic cap across reattach.
                try:
                    budget=(ResponseDeadline.restore(job['response_budget'])
                        if 'response_budget' in job else ResponseDeadline(job['deadline'],seconds=28800))
                except ProtocolError as exc:
                    if str(exc) not in {'global_response_clock_domain_changed','global_response_clock_rollback'}:raise
                    # A reboot/rollback forbids resuming the original wait, but
                    # must not hide a committed result from read-only recovery.
                    budget=ResponseDeadline(job['deadline'],seconds=28800)
                    budget.expires=job['deadline'];budget.until=time.monotonic();budget.failure=str(exc)
                self.response_budgets[rid]=budget
            budget=self.response_budgets[rid]
            require(budget.expires==job['deadline'],'facade_response_deadline_mismatch')
            return budget

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
            with self.controller.journal.locked() as journal:
                original=journal['objects'][request_id]['body']['payload']['responses_request']
            item=result_item(result.body['payload'],original,request_id,self.controller.pin.body['payload']['scope'])
            require(item['type']=='message','tool_receipt_requires_correlated_output')
            if state['jobs'][request_id]['status']=='confirmed':
                with self.controller.journal.locked() as s:
                    return s['deliveries'][str(result.body['seq'])]
            receipt=self.controller.record_delivery(request_id,result.oid,evidence)
            state['jobs'][request_id].update(status='confirmed',result=result.oid)
            self.save_state(state);return receipt
        finally:self.server.inflight.release()

    def closed_for_admission(self):
        return (self.controller.journal.root/'session-closed.json').exists()

    def close_session(self):
        # Competes against claim/begin on the SAME CAS record, even while an HTTP
        # request is waiting. Closing cannot revoke a permit already consumed.
        require(hasattr(self.controller,'close_session'),'cas_required_for_session_close')
        outcome=self.controller.close_session()
        _save(self.controller.journal.root/'session-closed.json',{'pin':self.controller.pin.oid,'closed':True})
        state=outcome['state']
        return {'closed':True,'phase':state['phase'],
                'execution_may_be_running':state['phase'] in {'DISPATCH_INTENT','AMBIGUOUS'},
                'late_results_recoverable':True}

    def request_status(self,rid):
        state=self.read_state();require(rid in state['jobs'],'unknown_facade_request')
        result=self.controller.result(rid)
        job=state['jobs'][rid]
        try:self.response_budget(rid,job).remaining('remote_wait_budget_expired');expired=False
        except ProtocolError:expired=True
        value={'request_id':rid,'delivery':job['status'],'result_id':result.oid if result else None,
               'status':'result_available' if result else 'pending_or_execution_unknown',
               'wait_expired':expired,'native_retry':False}
        if result:
            with self.controller.journal.locked() as journal:
                request=journal['objects'][rid]['body']['payload']['responses_request']
            value['item']=result_item(result.body['payload'],request,rid,self.controller.pin.body['payload']['scope'])
        return value

    def close(self):
        if self.closed:return
        self.closed=True;self.stop.set();self.server.stop_event.set()
        if self.thread.is_alive():self.server.shutdown()
        self.server.server_close()
        if self.thread.ident:self.thread.join(3)
        os.close(self.lock_fd)


class LongSessionHandler(RemoteHandler):
    """Streaming remote adapter; frozen POSIX handler intentionally unchanged.

    Text replay returns the same job. Tool intents have a durable pre-emission
    marker and cannot be replayed after an uncertain delivery; correlated actual
    Mac tool outputs, rather than a socket flush, allow the next inference.
    """
    def _identity(self):
        require(self.headers.get_all('Host')==[f'127.0.0.1:{self.server.server_port}'] and
                not self.headers.get_all('Origin') and not self.headers.get_all('Authorization'),
                'invalid_local_headers')
        require(not self.headers.get_all('session_id') and not self.headers.get_all('thread_id'),
                'ambiguous_legacy_session_header')
        identity={}
        for name,pattern in [('session-id',r'[!-~]{1,256}'),
                             ('thread-id',r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')]:
            values=self.headers.get_all(name) or []
            require(len(values)==1 and re.fullmatch(pattern,values[0]),'canonical_runtime_session_required')
            identity[name]=values[0]
        traces=self.headers.get_all('x-client-request-id') or []
        require(not traces or traces==[identity['thread-id']],'conflicting_client_request_id')
        binding=self.server.remote_owner.read_state()['binding']
        require(binding is None or binding==identity,'session_scope_mismatch')
        return identity

    def _body(self):
        require(not self.headers.get_all('Transfer-Encoding') and
                self.headers.get('Content-Encoding','identity')=='identity','unsupported_encoding')
        require(len(self.headers.get_all('Content-Type') or [])==1 and
                self.headers.get_content_type()=='application/json','unsupported_media_type')
        values=self.headers.get_all('Content-Length') or []
        require(len(values)==1 and re.fullmatch(r'[0-9]{1,10}',values[0]),'invalid_length')
        length=int(values[0]);require(1<=length<=MAX_WIRE_BYTES,'request_too_large_or_empty')
        raw=self.rfile.read(length);require(len(raw)==length,'truncated_body')
        try:return protocol.decode(raw)
        except (ValueError,UnicodeError,RecursionError):raise ProtocolError('invalid_json') from None

    def _json(self,value,status=200):
        raw=protocol.encode(value)
        self.send_response(status);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)));self.send_header('Connection','close')
        self.end_headers();self.wfile.write(raw);self.close_connection=True

    def _event(self,value):
        budget=getattr(self,'response_deadline',None)
        if budget is not None:
            # sendall has an aggregate timeout. Only a terminal error gets this
            # short diagnostic allowance; no result/tool can outlive the budget.
            self.connection.settimeout(.1 if value['type']=='response.failed' else
                budget.remaining('remote_wait_budget_expired'))
        self.wfile.write(b'event: '+value['type'].encode()+b'\ndata: '+protocol.encode(value)+b'\n\n')
        self.wfile.flush()

    def do_GET(self):
        try:
            self._identity();owner=self.server.remote_owner
            if self.path=='/v1/bridge/status':
                state=owner.read_state()
                return self._json({'deployment':owner.controller.pin.oid,
                    'native_task_id':owner.controller.pin.body['identity']['native_task_id'],
                    'expires':owner.controller.pin.body['payload']['expires'],
                    'max_requests':owner.controller.pin.body['payload']['max_requests'],
                    'selection':pin_selection(owner.controller.pin),
                    'inference_evidence':owner.controller.pin.body['payload'].get('inference'),
                    'admitted_requests':len(state['jobs']),'jobs':state['jobs'],
                    'closed':owner.closed_for_admission(),'scope':owner.controller.pin.body['payload']['scope'],'automatic_wake':False})
            match=re.fullmatch(r'/v1/bridge/requests/([0-9a-f]{64})(/result)?',self.path)
            require(match is not None,'unsupported_endpoint')
            value=owner.request_status(match[1])
            if not match[2]:value.pop('item',None)
            self._json(value)
        except (ProtocolError,QueueError) as exc:self.send_json(400,str(exc))
        finally:self.close_connection=True

    def do_POST(self):
        job=None;sent=False;acquired=False;owner=self.server.remote_owner
        guards=contextlib.ExitStack();budget=None
        try:
            identity=self._identity()
            if self.path=='/v1/responses':
                header_deadline=parse_response_deadline(self.headers)
                budget=ResponseDeadline(header_deadline,owner.response_expires,
                    owner.controller.pin.body['payload']['expires'],seconds=self.server.deadline)
                self.connection.settimeout(budget.remaining('remote_wait_budget_expired'))
                guards.enter_context(budget.watch_socket(self.connection))
                self.response_deadline=budget
            body=self._body();guards.close()
            if self.path=='/v1/bridge/close':
                require(body=={'confirm':True},'explicit_close_required')
                return self._json(owner.close_session())
            match=re.fullmatch(r'/v1/bridge/requests/([0-9a-f]{64})/ack',self.path)
            if match:
                require(isinstance(body,dict) and set(body)=={'result_id','evidence'},'result_and_evidence_required')
                result=owner.controller.result(match[1])
                require(result is not None and result.oid==body['result_id'],'unverified_result')
                return self._json({'receipt':owner.confirm_delivery(match[1],body['evidence'])})
            require(self.path=='/v1/responses','unsupported_endpoint')
            acquired=self.server.inflight.acquire(timeout=min(.1,budget.remaining('remote_wait_budget_expired')))
            if not acquired and self.server.delivery_started.is_set():
                acquired=self.server.inflight.acquire(timeout=min(5.9,budget.remaining('remote_wait_budget_expired')))
            budget.remaining('remote_wait_budget_expired')
            require(acquired,'request_inflight')
            require(not self.server.stop_event.is_set(),'service_stopping')
            owner.context.identity=identity;owner.context.response_deadline=budget
            owner.context.global_response=owner.response_expires is not None or header_deadline is not None
            try:job=self.server.store.enqueue(body,owner.controller.pin.body['identity']['session_id'],self.server.deadline)['job_id']
            finally:del owner.context.identity;del owner.context.response_deadline;del owner.context.global_response
            saved_job=owner.read_state()['jobs'][job];original=owner.response_budget(job,saved_job)
            if (owner.response_expires is not None or saved_job.get('global_response',False)):
                budget.tighten(original.expires)
                budget.until=min(budget.until,original.until)
            budget.remaining('remote_wait_budget_expired')
            self.send_response(200);self.send_header('Content-Type','text/event-stream')
            self.send_header('Cache-Control','no-cache');self.send_header('X-Request-ID',job)
            self.send_header('Connection','close');self.end_headers();sent=True
            self._event({'type':'response.created','response':{'id':'resp_'+job,'status':'in_progress'}})
            heartbeat=time.monotonic()+owner.heartbeat_interval
            poll=0;state=None
            while True:
                self.connection.settimeout(budget.remaining('remote_wait_budget_expired'))
                now=time.monotonic()
                if now>=poll:
                    state=self.server.store.response_state(job);poll=now+owner.poll_interval
                if state['state']=='completed':
                    item=state['wire_item']
                    if item['type']!='message':
                        saved=owner.read_state()
                        require(not saved['jobs'][job].get('tool_emission_started',False),'tool_emission_outcome_unknown')
                        saved['jobs'][job].update(status='emission_reserved',tool_emission_started=True);owner.save_state(saved)
                    budget.remaining('remote_wait_budget_expired')
                    self.server.delivery_started.set()
                    self._event({'type':'response.output_item.done','output_index':0,'item':item})
                    self._event({'type':'response.completed','response':{'id':'resp_'+job,'end_turn':item['type']=='message',
                        'status':'completed','output':[item]}})
                    self.server.store.delivery(job,'delivered');return
                if state['state'] in {'expired','cancelled'}:
                    self._event({'type':'response.failed','response':{'id':'resp_'+job,'error':{
                        'code':state.get('error',{}).get('code','remote_wait_budget_expired'),'message':'Result remains recoverable by request ID; do not create a replacement request'}}})
                    self.server.store.disconnect(job);return
                if self.server.stop_event.is_set():raise ProtocolError('service_stopped')
                if select.select([self.connection],[],[],0)[0] and not self.connection.recv(1,socket.MSG_PEEK):
                    self.server.store.disconnect(job);return
                if now>=heartbeat:
                    # JSON events, not SSE comments: Codex times parsed stream.next().
                    self._event({'type':'response.in_progress','response':{'id':'resp_'+job,'status':'in_progress'}})
                    heartbeat=now+owner.heartbeat_interval
                self.server.stop_event.wait(min(budget.remaining('remote_wait_budget_expired'),.25,max(.01,min(poll,heartbeat)-time.monotonic())))
        except (ProtocolError,QueueError) as exc:
            if job:
                try:self.server.store.disconnect(job)
                except Exception:pass
            try:
                if sent:self._event({'type':'response.failed','response':{'id':'resp_'+job,'error':{'code':str(exc),'message':str(exc)}}})
                else:self.send_json(409 if str(exc) in {'request_inflight','session_scope_mismatch'} else 400,str(exc))
            except OSError:pass
        except (OSError,TimeoutError):
            if job:
                try:self.server.store.disconnect(job)
                except Exception:pass
        finally:
            guards.close()
            if acquired:self.server.delivery_started.clear();self.server.inflight.release()
            self.close_connection=True
