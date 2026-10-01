"""Durable, offline action planner for an ALREADY ACTIVE native connector worker.

The native worker runs this small CLI between direct connector calls. Python never
calls connectors, spawns/wakes a native context, or invokes inference. Every emitted
write packet is one-attempt; unknown begin results are deliberately not replayed.
"""
import argparse
import contextlib
import fcntl
import json
import os
import time
from pathlib import Path
from .backend import filename, read_private_file
from .cli import write_new
from .connector_smoke import ConnectorEvidenceMessages, prepare, verify, consume_begin, structured
from .control import GoogleDocsCASControlStore, binding_for, _ref, MAX_CONTROL_BYTES
from .model import Object, canonical, hash_bytes, require, MAX_BYTES
from .session import _save, _check_payload, _check_request_binding, MAX_STATE
from .timing import timed_stage


class ConnectorWorker:
    @classmethod
    def provision(cls, root, pin, config, native_task_id, *, poll_seconds=15, max_reads=4096):
        require(native_task_id==pin.body['identity']['native_task_id'],'actual_native_identity_mismatch')
        require(type(poll_seconds) in (int,float) and 5<=poll_seconds<=60,'invalid_connector_poll_interval')
        require(type(max_reads) is int and 1<=max_reads<=4096,'invalid_connector_read_budget')
        require(set(config)=={'document_id','tab_id','control_id','writer_identity','folder_id'},'invalid_connector_config')
        root=Path(root);root.mkdir(mode=0o700,parents=True,exist_ok=False)
        write_new(root/'pin.json',pin.raw);write_new(root/'config.json',canonical(config))
        write_new(root/'lock',b'')
        _save(root/'worker.json',{'pin':pin.oid,'native_task_id':native_task_id,'last_epoch':-1,
            'last_state_hash':None,'pending':None,'records':{},'stopped':False,'next_poll_at':0,
            'poll_seconds':poll_seconds,'max_reads':max_reads,'reads':0})
        return cls(root,native_task_id)

    def __init__(self,root,native_task_id):
        self.root=Path(root)
        require(not self.root.is_symlink() and self.root.is_dir() and
                self.root.stat().st_uid==os.getuid() and not self.root.stat().st_mode&0o077,'unsafe_worker_runtime')
        self.pin=Object.parse(read_private_file(self.root/'pin.json',131072))
        require(self.pin.body['identity']['native_task_id']==native_task_id,'actual_native_identity_mismatch')
        self.config=json.loads(read_private_file(self.root/'config.json',131072))
        self.store=GoogleDocsCASControlStore(None,self.config['document_id'],self.config['tab_id'],
            self.config['control_id'],self.pin.body['identity']['session_id'],self.config['writer_identity'])

    @contextlib.contextmanager
    def locked(self):
        fd=os.open(self.root/'lock',os.O_RDWR|os.O_NOFOLLOW)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            s=json.loads(read_private_file(self.root/'worker.json',MAX_STATE))
            require(s['pin']==self.pin.oid and s['native_task_id']==self.pin.body['identity']['native_task_id'],
                    'worker_runtime_pin_mismatch')
            yield s
        finally:os.close(fd)

    @timed_stage('journal.save')
    def save(self,s):_save(self.root/'worker.json',s)
    def path(self,name):return self.root/name
    @timed_stage('object.read')
    def object(self,oid):return Object.parse(read_private_file(self.path(oid+'.json'),MAX_BYTES))
    @timed_stage('object.save')
    def save_object(self,obj):
        path=self.path(obj.oid+'.json')
        if path.exists():require(read_private_file(path,MAX_BYTES)==obj.raw,'saved_object_conflict')
        else:write_new(path,obj.raw)
        return {'object_id':obj.oid,'path':str(path),'name':filename(obj),'mime_type':'application/json',
                'folder_id':self.config['folder_id']}

    @timed_stage('control.observe')
    def _observe(self,s,resource):
        state=self.store.snapshot_from_document(structured(resource)).state
        require(state['binding']==binding_for(self.pin),'control_stale_worker_binding')
        digest=hash_bytes(canonical(state,max_bytes=MAX_CONTROL_BYTES))
        require(state['control_epoch']>=s['last_epoch'],'control_epoch_rollback')
        require(state['control_epoch']!=s['last_epoch'] or digest==s['last_state_hash'],'control_epoch_conflict')
        require(s['reads']<s['max_reads'],'connector_read_budget_exceeded')
        s.update(last_epoch=state['control_epoch'],last_state_hash=digest,reads=s['reads']+1)
        self.save(s);return state

    @timed_stage('connector.poll')
    def poll(self):
        with self.locked() as s:
            if s['stopped']:return {'action':'stopped','native_retry':False}
            if time.time()>=self.pin.body['payload']['expires']:
                known_result_pending=any(r['phase']=='result_saved' for r in s['records'].values())
                if not known_result_pending:
                    return {'action':'expired','native_retry':False,
                            'unresolved_execution':any(r['phase'] not in {'delivered','result_committed'} for r in s['records'].values())}
            require(s['reads']<s['max_reads'],'connector_read_budget_exceeded')
            wait=max(0,s['next_poll_at']-time.time())
            if wait:return {'action':'wait','seconds':wait,'reason':'connector_quota_pacing'}
            s['next_poll_at']=time.time()+s['poll_seconds'];self.save(s)
            return {'action':'read_control','document_id':self.config['document_id'],
                    'fields':'documentId,revisionId,suggestionsViewMode,tabs','scope':'direct_connector_only'}

    @timed_stage('control.plan')
    def _plan(self,s,resource,messages,kind,args,seq):
        op=hash_bytes(canonical({'pin':self.pin.oid,'seq':seq,'kind':kind}))
        plan=prepare(resource,self.pin,self.config,messages,kind,args,op)
        path=self.path('plan-'+op+'.json');write_new(path,canonical(plan,max_bytes=MAX_STATE))
        s['pending']={'operation_id':op,'kind':kind,'seq':seq,'plan':str(path),
                      'prepared_monotonic_ns':time.monotonic_ns(),'prepared_at':time.time()};self.save(s)
        return {'action':'cas_write_once','operation_id':op,'kind':kind,'plan':str(path),
                'tool_arguments':plan['tool_arguments'],'on_unknown':'stop_and_reconcile_no_begin_replay'}

    @timed_stage('connector.tick')
    def tick(self,resource,entries):
        messages=ConnectorEvidenceMessages(self.config['folder_id'],entries)
        with self.locked() as s:
            state=self._observe(s,resource)
            if s['pending']:
                pending=s['pending']
                # Result commit reconciliation is safe: no execution permit is recreated.
                if pending['kind']=='result':
                    plan=json.loads(read_private_file(Path(pending['plan']),MAX_STATE))
                    if plan['candidate']['operations'][-1] in state['operations']:
                        s['records'][str(pending['seq'])]['phase']='result_committed';s['pending']=None;self.save(s)
                    else:return {'action':'reconcile_cas','pending':pending,'native_retry':False}
                else:return {'action':'await_exact_cas_response','pending':pending,'native_retry':False}
            if (s['stopped'] or state.get('closed',False)) and not any(
                    r['phase']=='result_saved' for r in s['records'].values()):
                return {'action':'stopped','native_retry':False}
            phase=state['phase']
            if phase in {'IDLE','DELIVERED','RESULT_COMMITTED'}:
                if phase=='DELIVERED' and str(state['request']['message_seq']) in s['records']:
                    record=s['records'][str(state['request']['message_seq'])]
                    require(record.get('result')==state['result']['reference']['object_id'],'worker_result_receipt_mismatch')
                    record.update(phase='delivered',receipt=state['receipt']);self.save(s)
                return {'action':'expired' if time.time()>=self.pin.body['payload']['expires'] else 'wait',
                        'seconds':s['poll_seconds'],'phase':phase,'native_retry':False}
            seq=state['request']['message_seq'];key=str(seq)
            if phase=='REQUESTED':
                require(time.time()<self.pin.body['payload']['expires'],'control_deployment_expired')
                require(key not in s['records'],'request_already_attempted')
                require(seq==len(s['records'])+1,'worker_sequence_gap')
                previous=None
                if seq>1:
                    prior=s['records'][str(seq-1)]
                    matches=[h for h in state['history'] if h['request']['message_seq']==seq-1]
                    require(len(matches)==1 and matches[0]['phase']=='DELIVERED' and
                            matches[0]['result']['reference']['object_id']==prior.get('result'),'previous_delivery_unconfirmed')
                    previous=matches[0]['receipt'];prior.update(phase='delivered',receipt=previous)
                s['records'][key]={'request':_ref(state['request']),'phase':'claim_prepared','previous_receipt':previous}
                self.save(s)
                claim_id=hash_bytes(canonical({'pin':self.pin.oid,'seq':seq,'kind':'claim'}))
                return self._plan(s,resource,messages,'claim',{'binding':binding_for(self.pin),'claim_id':claim_id},seq)
            require(key in s['records'],'missing_worker_execution_journal')
            record=s['records'][key]
            require(record['request']==_ref(state['request']),'worker_request_changed')
            if phase=='CLAIMED':
                require(record['phase']=='claimed','unknown_claim_outcome')
                dispatch=hash_bytes(canonical({'request':record['request']['object_id'],
                                              'incarnation':self.pin.body['identity']['worker_journal_id']}))
                record['dispatch_id']=dispatch;self.save(s)
                return self._plan(s,resource,messages,'begin',{'binding':binding_for(self.pin),
                    'claim_id':state['claim']['id'],'dispatch_id':dispatch},seq)
            require(phase in {'DISPATCH_INTENT','AMBIGUOUS'},'unexpected_worker_phase')
            if record['phase']=='result_saved':
                batch=record.get('upload_batch')
                if s.get('router_execution_mode') == 'router_parallel_cells_v1' and not batch:
                    return {'action':'reconcile_uploads','native_retry':False,
                            'no_automatic_upload_retry':True,'reason':'router_parallel_upload_batch_required'}
                if batch and not all(i['status']=='verified' for i in batch['items'].values()):
                    return {'action':'reconcile_uploads','native_retry':False,
                            'no_automatic_upload_retry':True,'reason':'all_uploads_must_be_verified'}
                ids=[record['request']['object_id'],record['claim'],record['started'],record['result']]
                refs={}
                for kind,oid in zip(('request','claim','started','result'),ids):
                    found=[e['reference'] for e in entries if e['reference']['object_id']==oid]
                    if len(found)!=1:return {'action':'reconcile_uploads','missing_object_id':oid,'native_retry':False,
                                            'no_automatic_upload_retry':True}
                    messages.fetch(found[0]);refs[kind]=found[0]
                return self._plan(s,resource,messages,'result',{'binding':binding_for(self.pin),
                    'dispatch_id':record['dispatch_id'],**refs},seq)
            return {'action':'execution_outcome_unknown','request_id':record['request']['object_id'],
                    'native_retry':False,'hint':'Complete only an answer actually obtained with this consumed permit'}

    @timed_stage('connector.accept')
    def accept(self,response,readback):
        with self.locked() as s:
            require(s['pending'] is not None,'no_pending_cas')
            pending=s['pending'];plan=json.loads(read_private_file(Path(pending['plan']),MAX_STATE))
            state=verify(plan,response,readback)
            self._observe(s,readback)
            record=s['records'][str(pending['seq'])]
            if pending['kind']=='begin':
                require(not state.get('closed',False),'control_session_closed')
                permit=consume_begin(plan,response,readback,self.root)
                record.update(phase='begin_consumed',permit=permit)
                action={'action':'fetch_request_once','request_reference':record['request'],
                        'previous_receipt':record['previous_receipt'],'seq':pending['seq'],
                        'next':'Use input with exact downloaded request and metadata; only its first successful output is a native input'}
            else:
                record['phase']='claimed' if pending['kind']=='claim' else 'result_committed'
                action={'action':'read_control','document_id':self.config['document_id'],'native_permit':None}
            s['pending']=None;self.save(s);return action

    @timed_stage('connector.input')
    def input(self,entries,seq,expose_path=None):
        with self.locked() as s:
            record=s['records'].get(str(seq));require(record and record['phase']=='begin_consumed','one_use_input_unavailable')
            require(not s['stopped'] and time.time()<self.pin.body['payload']['expires'],'worker_stopped_or_expired')
            messages=ConnectorEvidenceMessages(self.config['folder_id'],entries)
            request=messages.fetch(record['request']);_check_payload(request)
            require(request.body['identity']==self.pin.body['identity'] and request.body['deployment']==self.pin.oid and
                    request.body['kind']=='request' and request.body['seq']==seq,'request_pin_mismatch')
            expected={} if seq==1 else {'previous_receipt':record['previous_receipt']['object_id']}
            require(request.body['links']==expected,'control_predecessor_mismatch')
            if seq>1:
                receipt=messages.fetch(record['previous_receipt']);_check_payload(receipt)
                previous=s['records'][str(seq-1)]
                require(receipt.body['identity']==self.pin.body['identity'] and receipt.body['deployment']==self.pin.oid and
                        receipt.body['seq']==seq-1 and receipt.body['links']=={'request':previous['request']['object_id'],
                        'result':previous['result']},'previous_receipt_mismatch')
            _check_request_binding(request.body['payload'],self.pin)
            self.save_object(request)
            record['phase']='input_exposed';self.save(s)  # irreversible before plaintext is returned
            value={'action':'native_inference_once','native_task_id':s['native_task_id'],
                    'request_id':request.oid,'seq':seq,'payload':request.body['payload'],
                    'native_invoked_by_python':False,'no_automatic_retry':True,
                    'inference_binding':self.pin.body['payload'].get('inference')}
            if expose_path is not None:
                path=Path(expose_path)
                require(path.parent.resolve()==self.root.resolve(),'input_file_must_be_in_runtime')
                raw=canonical(value,max_bytes=MAX_STATE)
                write_new(path,raw)  # consumed marker is durable before plaintext exposure
                return {'action':'native_input_file_once','path':str(path),'bytes':len(raw),
                        'sha256':hash_bytes(raw),'native_task_id':s['native_task_id'],
                        'no_automatic_retry':True,'native_invoked_by_python':False}
            return value

    @timed_stage('connector.result')
    def result(self,seq,text):
        with self.locked() as s:
            record=s['records'].get(str(seq));require(record and record['phase']=='input_exposed','no_exposed_input_for_result')
            req=self.object(record['request']['object_id'])
            claim=Object.make(self.pin.body['identity'],'claim',seq,self.pin.oid,{'status':'claimed'},{'request':req.oid})
            started=Object.make(self.pin.body['identity'],'started',seq,self.pin.oid,
                {'status':'dispatch_intent','dispatch_id':record['dispatch_id']},{'request':req.oid,'claim':claim.oid})
            payload={'text':text} if isinstance(text,str) else {'response_result':text}
            require(isinstance(text,str) or self.pin.body['payload']['scope']=='responses_tools','text_only_result_required')
            if 'responses_request' in req.body['payload']:
                from .wire import result_item
                result_item(payload,req.body['payload']['responses_request'],req.oid,self.pin.body['payload']['scope'])
            result=Object.make(self.pin.body['identity'],'result',seq,self.pin.oid,payload,
                {'request':req.oid,'started':started.oid})
            _check_payload(result)
            packets=[self.save_object(obj) for obj in (claim,started,result)]
            record.update(phase='result_saved',claim=claim.oid,started=started.oid,result=result.oid)
            self.save(s)  # all upload intents fixed before any uploads are suggested
            if s.get('router_execution_mode') == 'router_parallel_cells_v1':
                return {'action':'prepare_parallel_upload_cell','seq':seq,
                        'module':'remote_transport.connector_cell','phase':'upload-commit',
                        'upload_batch_required':True,'native_retry':False,
                        'on_unknown':'preserve exact evidence; never substitute serial uploads or repeat inference'}
            return {'action':'upload_once','objects':packets,'verify_exact_bytes_and_metadata':True,
                    'on_unknown':'reconcile exact file IDs; do not repeat inference or blindly upload'}

    @timed_stage('connector.status')
    def status(self,stop=False):
        with self.locked() as s:
            if stop:s['stopped']=True;self.save(s)
            return {'native_task_id':s['native_task_id'],'stopped':s['stopped'],'expires':self.pin.body['payload']['expires'],
                'reads':s['reads'],'max_reads':s['max_reads'],'pending':s['pending'],
                'records':s['records'],'native_retry':False,'automatic_wake':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation',choices=['init','poll','tick','accept','input','result','status','stop'])
    p.add_argument('--root',required=True);p.add_argument('--native-task-id',required=True)
    for name in ('pin','config','snapshot','manifest','response','readback','text-file','result-file','save','expose-path'):p.add_argument('--'+name)
    p.add_argument('--seq',type=int);p.add_argument('--poll-seconds',type=float,default=15)
    a=p.parse_args();os.umask(0o077)
    def read(path):return json.loads(read_private_file(Path(path),MAX_STATE))
    if a.operation=='init':
        w=ConnectorWorker.provision(a.root,Object.parse(read_private_file(a.pin,131072)),read(a.config),
                                    a.native_task_id,poll_seconds=a.poll_seconds)
        value=w.status()
    else:
        w=ConnectorWorker(a.root,a.native_task_id)
        if a.operation=='poll':value=w.poll()
        elif a.operation=='tick':value=w.tick(read(a.snapshot),read(a.manifest) if a.manifest else [])
        elif a.operation=='accept':value=w.accept(read(a.response),read(a.readback))
        elif a.operation=='input':value=w.input(read(a.manifest),a.seq,expose_path=a.expose_path)
        elif a.operation=='result':value=w.result(a.seq,read(a.result_file) if a.result_file else read_private_file(a.text_file,65536).decode())
        else:value=w.status(a.operation=='stop')
    if a.save:
        _save(Path(a.save),value)
        return {'packet_saved':a.save,'action':value.get('action'),'native_invoked_by_python':False}
    return value


if __name__=='__main__':print(json.dumps(main(),ensure_ascii=False,indent=2))
