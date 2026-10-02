"""Durable native admission, one-use input exposure and immutable publication.

These helpers never spawn, call a model, upload, or invoke any Google API.
Only the real native parent/child adapter performs those external operations.
"""
import copy
import hashlib
import hmac
from pathlib import Path
import secrets
import re
import time
from .protocol import (PROTOCOL, OUTBOX_MAX_BYTES, canonical, strict_json, sha256, require, safe_id,
                       ProtocolError, validate_grant, validate_route, parse_inbox, parse_outbox, sign_record)
from .docs import plan_write, validate_plan, accept_write, reconcile_write, snapshot
from .storage import Journal, private_read, private_write, fsync_dir, burn_fence


RESULT_CAS_CONTRACT = 'dots-lite-result-cas/1'
RESULT_CAS_MAX_ATTEMPTS = 3


def _clock():
    try:boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:boot='process-'+_PROCESS
    return time.time(),time.monotonic(),boot
_PROCESS=secrets.token_hex(16)

def _anchor(clock):
    wall,mono,boot=clock();return {'wall':wall,'mono':mono,'boot':boot}

def _active(state,clock,descriptor=None):
    wall,mono,boot=clock();anchor=state['clock'];grant=state['grant']
    require(boot==anchor['boot'] and mono>=anchor['mono'] and wall>=anchor['wall']-1,'clock_uncertain_new_begin_paused')
    # Backward clock jumps cannot extend the original monotonic authorization.
    deadline=grant['expires_at'] if descriptor is None else min(grant['expires_at'],descriptor['begin_before'])
    require(wall>=grant['created_at'] and wall<deadline and mono-anchor['mono']<deadline-anchor['wall'],'authorization_expired')


def _accepted(plan,response,readback):
    result=reconcile_write(plan,readback) if readback is not None else accept_write(plan,response)
    return result

def _task_result(actual):
    require(type(actual) is dict,'actual_native_result_required')
    require(actual.get('isError') is not True and not actual.get('error'),'native_admission_error_result')
    candidates=[]
    for name in ('task_name','agent_name'):
        if isinstance(actual.get(name),str):candidates.append(actual[name])
    for name in ('structuredContent','result'):
        if type(actual.get(name)) is dict:
            child=actual[name]
            require(child.get('isError') is not True and not child.get('error'),'native_admission_error_result')
            for field in ('task_name','agent_name'):
                if isinstance(child.get(field),str):candidates.append(child[field])
    require(candidates and len(set(candidates))==1,'native_admission_result_unresolved')
    task=candidates[0];safe_id(task);require(task.startswith('/'),'actual_native_task_path_required')
    return task

class _Actor:
    def __init__(self,state_dir,key,identity,clock=None):
        self.journal=Journal(state_dir);self.key=key;self.identity=identity;self.clock=clock or _clock
        self._validate(self.journal.snapshot())
    @property
    def state(self):return self.journal.snapshot()
    def _validate(self,state):
        validate_grant(state['grant']);validate_route(state['route'],state['grant'])
        require(state['owner']==self.identity,'actor_ownership_mismatch')
        require(state['key_sha256']==sha256(bytes.fromhex(self.key) if isinstance(self.key,str) else self.key),'join_key_mismatch')
    def _write(self,state):return self.journal.write(state)
    def _accept(self,state,response,readback):
        require(state.get('pending_plan') is not None,'no_pending_write')
        result=_accepted(state['pending_plan'],response,readback)
        if result['status'] not in {'accepted','applied'}:
            state['write_status']=result['status'];self._write(state);return result
        state['outbox']=result['snapshot'];state['record']=state['pending_plan']['record']
        state['pending_plan']=None;state['write_status']='accepted'
        return result

class ParentController(_Actor):
    @staticmethod
    def recover_handoff(state_dir,key,parent_task_id):
        journal=Journal(state_dir)
        with journal.locked():
            state=journal.read()
            require(state['parent_task_id']==parent_task_id,'actual_parent_identity_mismatch')
            require(state['key_sha256']==sha256(bytes.fromhex(key) if isinstance(key,str) else key),'join_key_mismatch')
            require(state['handoff'] is not None and state['record'] is not None,'handoff_not_committed')
            parse_outbox(state['record'],state['route'],key,state['grant'])
            require(state['owner']==state['record']['child_task_id']==_task_result(state['actual_tool_result']),'handoff_owner_mismatch')
            handoff=state['handoff']
            require(sha256(private_read(handoff['receipt_path']))==handoff['receipt_sha256'],'admission_receipt_mismatch')
            return copy.deepcopy(handoff)

    @classmethod
    def create(cls,state_dir,grant,key,route,parent_task_id,outbox_snapshot,clock=None):
        validate_grant(grant);validate_route(route,grant);safe_id(parent_task_id)
        require(outbox_snapshot['document_id']==route['outbox_id'] and outbox_snapshot['text']=='\n','fresh_outbox_required')
        require(outbox_snapshot['max_bytes']==OUTBOX_MAX_BYTES,'outbox_cap_required')
        state={'grant':copy.deepcopy(grant),'route':copy.deepcopy(route),'owner':parent_task_id,'parent_task_id':parent_task_id,
               'key_sha256':sha256(bytes.fromhex(key) if isinstance(key,str) else key),'clock':_anchor(clock or _clock),
               'outbox':copy.deepcopy(outbox_snapshot),'record':None,'phase':'EMPTY','spawn_fence':'NOT_ISSUED',
               'pending_plan':None,'write_status':None,'actual_arguments':None,'actual_tool_result':None,
               'current':None,'history':[],'handoff':None}
        _active(state,clock or _clock)
        Journal.create(state_dir,state)
        return cls(state_dir,key,parent_task_id,clock)

    def reserve_spawn(self,actual_arguments):
        with self.journal.locked():
            state=self.journal.read();self._validate(state);_active(state,self.clock)
            require(state['phase']=='EMPTY' and state['spawn_fence']=='NOT_ISSUED','spawn_already_reserved_no_retry')
            route=state['route'];args=actual_arguments
            require(type(args) is dict and set(args)=={'task_name','message','fork_turns','model','reasoning_effort'},'invalid_spawn_arguments')
            require(args['fork_turns']=='none' and args['model']==route['model'] and args['reasoning_effort']==route['reasoning_effort'],'spawn_pair_mismatch')
            require(isinstance(args['task_name'],str) and re.fullmatch(r'[a-z0-9_]{1,100}',args['task_name']) is not None,'invalid_native_task_name')
            require(isinstance(args['message'],str) and 0<len(args['message'].encode())<=128*1024,'invalid_native_message')
            op=secrets.token_hex(16)
            record={'protocol':PROTOCOL,'kind':'outbox','activation_id':state['grant']['activation_id'],
                    **{k:route[k] for k in ('route_id','identity_sha256','model','reasoning_effort')},
                    'phase':'SPAWN_RESERVED','operation_id':op,'spawn_operation_id':op,'parent_task_id':self.identity,
                    'child_task_id':None,'admission':None,'consumed_seq':0,'request':None,'begin_operation_id':None,'result':None}
            record=sign_record(record,self.key);parse_outbox(record,route,self.key,state['grant'])
            plan=plan_write(state['outbox'],record,op)
            state.update(phase='SPAWN_RESERVED',actual_arguments=copy.deepcopy(args),pending_plan=plan,write_status='prepared')
            self._write(state);return copy.deepcopy(plan)

    def accept_spawn_reserved_and_issue(self,actual_response=None,readback=None):
        with self.journal.locked():
            state=self.journal.read();self._validate(state);_active(state,self.clock)
            require(state['phase']=='SPAWN_RESERVED' and state['spawn_fence']=='NOT_ISSUED','spawn_already_issued_or_unknown')
            result=self._accept(state,actual_response,readback)
            if result['status'] not in {'accepted','applied'}:return result
            burn_fence(self.journal.directory,'spawn.once',{'operation_id':state['record']['spawn_operation_id'],
                       'arguments_sha256':sha256(canonical(state['actual_arguments'])),'parent_task_id':self.identity})
            state['spawn_fence']='ISSUED';self._write(state)
            return copy.deepcopy(state['actual_arguments'])

    def record_actual_admission(self,actual_arguments,actual_tool_result):
        with self.journal.locked():
            state=self.journal.read();self._validate(state)
            require(state['spawn_fence']=='ISSUED' and state['phase']=='SPAWN_RESERVED' and state['pending_plan'] is None,'spawn_result_not_recordable')
            require(actual_arguments==state['actual_arguments'],'actual_spawn_arguments_mismatch')
            task=_task_result(actual_tool_result)
            require(task==self.identity+'/'+actual_arguments['task_name'],'actual_child_parent_mismatch')
            admission={'adapter':'collaboration.spawn_agent','native_task_id':task,
                       'submitted_model':actual_arguments['model'],'submitted_reasoning_effort':actual_arguments['reasoning_effort'],
                       'fork_turns':'none','arguments_sha256':sha256(canonical(actual_arguments)),
                       'tool_result_sha256':sha256(canonical(actual_tool_result)),
                       'verification':'parent_recorded_platform_admission','underlying_model_verified':False}
            record=copy.deepcopy(state['record']);record.update(phase='ADMITTED',child_task_id=task,admission=admission,operation_id=secrets.token_hex(16));record=sign_record(record,self.key)
            parse_outbox(record,state['route'],self.key,state['grant']);plan=plan_write(state['outbox'],record,record['operation_id'])
            state.update(phase='ADMISSION_PREPARED',actual_tool_result=copy.deepcopy(actual_tool_result),pending_plan=plan,write_status='prepared')
            self._write(state);return copy.deepcopy(plan)

    def accept_admission(self,actual_response=None,readback=None):
        with self.journal.locked():
            state=self.journal.read();self._validate(state)
            require(state['phase']=='ADMISSION_PREPARED','admission_not_pending')
            result=self._accept(state,actual_response,readback)
            if result['status'] not in {'accepted','applied'}:return result
            receipt=canonical({'actual_arguments':state['actual_arguments'],'actual_tool_result':state['actual_tool_result'],'admission':state['record']['admission']})
            path=self.journal.directory/'admission.json';private_write(path,receipt,immutable=True)
            handoff={'state_dir':str(self.journal.directory.resolve()),'receipt_path':str(path.resolve()),'receipt_sha256':sha256(receipt),
                     'child_task_id':state['record']['child_task_id'],'route_id':state['route']['route_id'],
                     'activation_id':state['grant']['activation_id'],'snapshot':copy.deepcopy(state['outbox'])}
            # Transfer is durable before handing it to the one actual child.
            state.update(phase='ADMITTED',owner=state['record']['child_task_id'],handoff=handoff)
            self._write(state);return handoff

class Worker(_Actor):
    def _cleanup_acknowledged(self,state):
        current=state['current'];keep=set()
        if current is not None:
            keep.add(Path(current['input_path']))
            if current['artifact'] is not None:keep.add(Path(current['artifact']['path']))
        changed=False
        for item in state['history']:
            paths=[self.journal.directory/('input-'+item['request_sha256']+'.json'),
                   self.journal.directory/('result-'+item['result_id']+'.json')]
            if item.get('result_publication_operation_id'):
                paths.append(self._result_seal_path(item['result_publication_operation_id']))
            for path in paths:
                if path.resolve() not in keep and path.exists():
                    require(not path.is_symlink(),'unsafe_cached_payload')
                    path.unlink();changed=True
        if changed:fsync_dir(self.journal.directory)

    def _validate(self,state):
        super()._validate(state)
        require(state.get('record') is not None and state['record']['child_task_id']==self.identity,'actual_child_identity_mismatch')
        parse_outbox(state['record'],state['route'],self.key,state['grant'])
        require(state['actual_tool_result'] is not None and _task_result(state['actual_tool_result'])==self.identity,'actual_admission_missing')

    def takeover(self,handoff):
        with self.journal.locked():
            state=self.journal.read();self._validate(state)
            require(handoff==state['handoff'],'handoff_mismatch')
            require(sha256(private_read(handoff['receipt_path']))==handoff['receipt_sha256'],'admission_receipt_mismatch')
            return {'status':'accepted','snapshot':copy.deepcopy(state['outbox']),'child_task_id':self.identity}

    def observe_outbox(self,resource):
        with self.journal.locked():
            state=self.journal.read();self._validate(state)
            require(state['pending_plan'] is None,'pending_write_reconcile_required')
            fresh=snapshot(resource,state['route']['outbox_id'],state['outbox']['tab_id'],OUTBOX_MAX_BYTES)
            record=parse_outbox(fresh,state['route'],self.key,state['grant'])
            require(record==state['record'],'outbox_conflict_no_execution')
            state['outbox']=fresh;self._write(state);return fresh

    def prepare_begin(self,inbox_record,raw_path,metadata=None):
        with self.journal.locked():
            state=self.journal.read();self._validate(state)
            self._cleanup_acknowledged(state)
            inbox=parse_inbox(inbox_record,state['grant'],self.key)
            routes=[route for route in inbox['routes'] if route['route_id']==state['route']['route_id']]
            require(len(routes)==1,'route_missing');route=routes[0]
            require(all(route[k]==state['route'][k] for k in ('route_id','identity_sha256','model','reasoning_effort','outbox_id')),'immutable_route_mismatch')
            require(not inbox['stop'] and not route['stop'],'new_begin_stopped')
            desc=route['request'];require(desc is not None,'request_missing');_active(state,self.clock,desc)
            require(state['phase'] in {'ADMITTED','RESULT_COMMITTED'} and state['pending_plan'] is None,'route_busy_or_execution_unknown')
            require(desc['seq']==state['record']['consumed_seq']+1,'sequence_replay_or_gap')
            require(all(item['request_id']!=desc['request_id'] for item in state['history']),'request_id_reused')
            previous=state['current']
            if previous is not None:
                require(desc['request_id']!=previous['descriptor']['request_id'],'request_id_reused')
                expected={k:previous['artifact'][k] for k in ('result_id','result_sha256')}
                require(desc['previous_result_ack']==expected,'previous_result_not_acknowledged')
            if metadata is not None:
                require(type(metadata) is dict and metadata.get('id',metadata.get('file_id'))==desc['file_id'],'request_metadata_id_mismatch')
                if set(metadata)=={'file_id','folder_id','byte_length'}:
                    require(metadata['folder_id']==desc['folder_id'] and metadata['byte_length']==desc['byte_length'],'request_metadata_mismatch')
                else:
                    parents=metadata.get('parents');require(type(parents) is list and parents==[desc['folder_id']],'request_metadata_folder_mismatch')
                    require(str(metadata.get('size'))==str(desc['byte_length']) and metadata.get('trashed',False) is False,'request_metadata_size_mismatch')
            raw=private_read(raw_path,state['grant']['limits']['max_request_bytes'])
            require(len(raw)==desc['byte_length'] and sha256(raw)==desc['request_sha256'],'request_bytes_mismatch')
            request=strict_json(raw);require(type(request) is dict,'invalid_request')
            from .wire import validate_request
            validate_request(request)
            require(request.get('model')==route['model'] and type(request.get('reasoning')) is dict and request['reasoning'].get('effort')==route['reasoning_effort'],'request_pair_mismatch')
            require('reasoning_effort' not in request and 'model_reasoning_effort' not in request,'ambiguous_reasoning_effort')
            path=self.journal.directory/('input-'+desc['request_sha256']+'.json');private_write(path,raw,immutable=True)
            op=secrets.token_hex(16);record=copy.deepcopy(state['record'])
            record.update(phase='BEGIN',operation_id=op,consumed_seq=desc['seq'],request=copy.deepcopy(desc),begin_operation_id=op,result=None)
            record=sign_record(record,self.key);parse_outbox(record,route,self.key,state['grant'])
            plan=plan_write(state['outbox'],record,op)
            if previous is not None:
                state['history'].append({'request_id':previous['descriptor']['request_id'],'request_sha256':previous['descriptor']['request_sha256'],
                                         'seq':previous['descriptor']['seq'],'result_id':previous['artifact']['result_id'],'result_sha256':previous['artifact']['result_sha256'],
                                         **({'result_publication_operation_id':previous['result_publication']['operation_id']}
                                            if previous.get('result_publication') else {})})
            state.update(phase='BEGIN_PREPARED',route=copy.deepcopy(route),pending_plan=plan,write_status='prepared',
                         current={'descriptor':copy.deepcopy(desc),'input_path':str(path.resolve()),'exposure':'INPUT_NOT_EXPOSED',
                                  'artifact':None,'upload_attempts':0,'upload_receipt':None})
            self._write(state)
            # Payloads are disposable only after the next authenticated ACK;
            # compact identity/fence history remains in the bounded journal.
            self._cleanup_acknowledged(state)
            return copy.deepcopy(plan)

    def accept_begin_and_expose(self,actual_response=None,readback=None,expose_to_path=False):
        with self.journal.locked():
            state=self.journal.read();self._validate(state)
            require(state['phase']=='BEGIN_PREPARED' and state['current']['exposure']=='INPUT_NOT_EXPOSED','input_already_exposed_or_journal_unknown')
            _active(state,self.clock,state['current']['descriptor'])
            result=self._accept(state,actual_response,readback)
            if result['status'] not in {'accepted','applied'}:return result
            current=state['current'];raw=private_read(current['input_path'],state['grant']['limits']['max_request_bytes'])
            require(sha256(raw)==current['descriptor']['request_sha256'] and len(raw)==current['descriptor']['byte_length'],'durable_input_changed')
            request=strict_json(raw)
            burn_fence(self.journal.directory,'exposure-'+str(current['descriptor']['seq'])+'.once',
                       {'request_id':current['descriptor']['request_id'],'request_sha256':current['descriptor']['request_sha256'],
                        'begin_operation_id':state['record']['begin_operation_id'],'child_task_id':self.identity})
            state['phase']='INPUT_EXPOSED';current['exposure']='EXPOSED';self._write(state)
            if expose_to_path:return {'status':'exposed','exposed_path':current['input_path'],'request_sha256':current['descriptor']['request_sha256'],'byte_length':len(raw),'request_id':current['descriptor']['request_id']}
            return request

    def save_actual_result(self,request_id,actual_output):
        with self.journal.locked():
            state=self.journal.read();self._validate(state)
            require(state['current'] is not None and state['current']['exposure']=='EXPOSED','result_requires_original_exposure')
            current=state['current'];desc=current['descriptor'];require(request_id==desc['request_id'],'result_request_mismatch')
            if state['phase'] in {'RESULT_SAVED','RESULT_PREPARED','RESULT_COMMITTED'}:
                artifact=current['artifact'];raw=private_read(artifact['path'],state['grant']['limits']['max_result_bytes'])
                require(sha256(raw)==artifact['result_sha256'] and strict_json(raw)['output']==actual_output,'immutable_result_conflict')
                return copy.deepcopy(artifact)
            require(state['phase']=='INPUT_EXPOSED','result_requires_original_exposure')
            from .wire import validate_response
            request=strict_json(private_read(current['input_path'],state['grant']['limits']['max_request_bytes']))
            validate_response(actual_output,request)
            record=state['record'];result_id=secrets.token_hex(16)
            payload={'protocol':PROTOCOL,'kind':'result','result_id':result_id,'output':copy.deepcopy(actual_output),
                     **{k:record[k] for k in ('activation_id','route_id','begin_operation_id','child_task_id','model','reasoning_effort')},
                     **{k:desc[k] for k in ('request_id','request_sha256','seq')}}
            raw=canonical(payload);require(len(raw)<=state['grant']['limits']['max_result_bytes'],'result_too_large')
            path=self.journal.directory/('result-'+result_id+'.json');private_write(path,raw,immutable=True)
            artifact={'path':str(path.resolve()),'result_id':result_id,'result_sha256':sha256(raw),'byte_length':len(raw)}
            current['artifact']=artifact;state['phase']='RESULT_SAVED';self._write(state);return copy.deepcopy(artifact)

    def record_upload_attempt(self):
        with self.journal.locked():
            state=self.journal.read();self._validate(state)
            require(state['phase']=='RESULT_SAVED' and state['pending_plan'] is None,'result_upload_not_allowed')
            current=state['current'];artifact=current['artifact']
            require(current['upload_attempts']<3,'upload_attempt_budget_exhausted')
            raw=private_read(artifact['path'],state['grant']['limits']['max_result_bytes'])
            require(len(raw)==artifact['byte_length'] and sha256(raw)==artifact['result_sha256'],'immutable_result_changed')
            current['upload_attempts']+=1;self._write(state);return copy.deepcopy(artifact)

    def _result_seal_mac(self,value):
        key=bytes.fromhex(self.key) if isinstance(self.key,str) else self.key
        return hmac.new(key,(RESULT_CAS_CONTRACT+'\0').encode()+canonical(value),hashlib.sha256).hexdigest()

    def _result_seal_path(self,operation_id):
        require(isinstance(operation_id,str) and re.fullmatch('[0-9a-f]{32}',operation_id) is not None,
                'invalid_result_publication_operation')
        return self.journal.directory/('publication-'+operation_id+'.json')

    def _result_attempt_evidence(self,state,plan,attempt):
        return {'contract':RESULT_CAS_CONTRACT,'journal_id':state['journal_id'],
                'operation_id':plan['operation_id'],'attempt':attempt,
                'plan_sha256':plan['plan_sha256'],'body_sha256':sha256(canonical(plan['body']))}

    def _result_attempts(self,state,plan):
        # The independent fsynced markers, not a mutable counter, own the
        # dispatch budget. A crash after burning a marker consumes that attempt.
        count=0;gap=False
        for attempt in range(1,RESULT_CAS_MAX_ATTEMPTS+1):
            path=self.journal.directory/('result-cas-'+plan['operation_id']+'-'+str(attempt)+'.once')
            if not path.exists() and not path.is_symlink():gap=True;continue
            require(not gap,'result_publication_attempt_history_corrupt')
            require(private_read(path,8192)==canonical(self._result_attempt_evidence(state,plan,attempt)),
                    'result_publication_attempt_history_corrupt')
            count=attempt
        recorded=state['current']['result_publication']['attempts']
        require(type(recorded) is int and 0<=recorded<=count,'result_publication_attempt_history_corrupt')
        return count

    def _saved_result_plan(self,state):
        require(state['phase']=='RESULT_PREPARED' and state.get('pending_plan') is not None,'result_not_pending')
        current=state['current'];publication=current.get('result_publication')
        # Never retrofit a seal/permit onto a live journal from an older release.
        require(type(publication) is dict and set(publication)=={'operation_id','seal_sha256','attempts','quarantined'},
                'result_publication_seal_required_new_release')
        operation=publication['operation_id'];path=self._result_seal_path(operation)
        raw=private_read(path,131072)
        require(sha256(raw)==publication['seal_sha256'],'result_publication_seal_changed')
        seal=strict_json(raw)
        require(type(seal) is dict and set(seal)=={'binding','mac'} and canonical(seal)==raw,
                'result_publication_seal_changed')
        binding=seal['binding']
        require(type(binding) is dict and set(binding)=={'contract','journal_id','owner','grant_sha256','plan'},
                'result_publication_seal_changed')
        require(isinstance(seal['mac'],str) and hmac.compare_digest(self._result_seal_mac(binding),seal['mac']),
                'result_publication_seal_authentication_failed')
        require(binding['contract']==RESULT_CAS_CONTRACT and binding['journal_id']==state['journal_id']
                and binding['owner']==self.identity and binding['grant_sha256']==sha256(canonical(state['grant'])),
                'result_publication_seal_binding_mismatch')
        plan=validate_plan(binding['plan'])
        require(plan['operation_id']==operation and canonical(state['pending_plan'])==canonical(plan),
                'result_publication_plan_changed')
        require(plan['source']==state['outbox'] and plan['source']['text']==canonical(state['record']).decode()+'\n'
                and state['record']['phase']=='BEGIN' and current['exposure']=='EXPOSED',
                'result_publication_source_changed')
        record=parse_outbox(plan['record'],state['route'],self.key,state['grant'])
        require(record['phase']=='RESULT' and record['request']==current['descriptor'],'result_publication_binding_mismatch')
        artifact=current['artifact'];receipt=current['upload_receipt']
        require(type(artifact) is dict and type(receipt) is dict,'result_publication_artifact_missing')
        locator={k:artifact[k] for k in ('result_id','result_sha256','byte_length')}
        locator.update(file_id=receipt['file_id'],folder_id=receipt['folder_id'])
        expected=copy.deepcopy(state['record']);expected.update(phase='RESULT',operation_id=operation,result=locator)
        require(sign_record(expected,self.key)==record and receipt['byte_length']==artifact['byte_length'],
                'result_publication_binding_mismatch')
        expected_path=self.journal.directory/('result-'+artifact['result_id']+'.json')
        require(Path(artifact['path'])==expected_path.resolve(),'result_publication_artifact_path_changed')
        payload=private_read(expected_path,state['grant']['limits']['max_result_bytes'])
        require(len(payload)==artifact['byte_length'] and sha256(payload)==artifact['result_sha256'],
                'immutable_result_changed')
        from .protocol import validate_result_envelope
        validate_result_envelope(strict_json(payload),state['grant'],state['route'],record)
        return plan

    def _result_quarantined(self,state,plan):
        path=self.journal.directory/('result-cas-'+plan['operation_id']+'-quarantine.once')
        return state['current']['result_publication']['quarantined'] is not False or path.exists() or path.is_symlink()

    def _issue_result_attempt(self,state,plan,expected_attempt):
        require(not self._result_quarantined(state,plan),'result_publication_quarantined')
        count=self._result_attempts(state,plan)
        require(count<RESULT_CAS_MAX_ATTEMPTS,'result_publication_attempt_budget_exhausted')
        require(type(expected_attempt) is int and expected_attempt==count+1,'result_publication_attempt_mismatch')
        burn_fence(self.journal.directory,'result-cas-'+plan['operation_id']+'-'+str(expected_attempt)+'.once',
                   self._result_attempt_evidence(state,plan,expected_attempt))
        state['current']['result_publication']['attempts']=expected_attempt
        state['write_status']='prepared' if expected_attempt==1 else 'retry_prepared'
        self._write(state)
        # Initial dispatch and explicit retries both leave through these same
        # canonical saved bytes. No fresh revision, replan, upload or exposure.
        return copy.deepcopy(plan)

    def publish_result(self,upload_receipt):
        with self.journal.locked():
            state=self.journal.read();self._validate(state)
            require(state['phase']=='RESULT_SAVED' and state['current']['upload_attempts']>0 and state['pending_plan'] is None,'result_not_publishable')
            receipt=upload_receipt;artifact=state['current']['artifact']
            require(type(receipt) is dict and set(receipt)=={'file_id','folder_id','byte_length'},'actual_upload_receipt_required')
            safe_id(receipt['file_id']);require(receipt['folder_id']==state['grant']['folder_id'] and receipt['byte_length']==artifact['byte_length'],'upload_receipt_mismatch')
            locator={k:artifact[k] for k in ('result_id','result_sha256','byte_length')};locator.update(file_id=receipt['file_id'],folder_id=receipt['folder_id'])
            record=copy.deepcopy(state['record']);record.update(phase='RESULT',operation_id=secrets.token_hex(16),result=locator);record=sign_record(record,self.key)
            parse_outbox(record,state['route'],self.key,state['grant']);plan=plan_write(state['outbox'],record,record['operation_id'])
            binding={'contract':RESULT_CAS_CONTRACT,'journal_id':state['journal_id'],'owner':self.identity,
                     'grant_sha256':sha256(canonical(state['grant'])),'plan':plan}
            seal=canonical({'binding':binding,'mac':self._result_seal_mac(binding)})
            private_write(self._result_seal_path(plan['operation_id']),seal,immutable=True)
            state['current']['upload_receipt']=copy.deepcopy(receipt)
            state['current']['result_publication']={'operation_id':plan['operation_id'],'seal_sha256':sha256(seal),
                                                    'attempts':0,'quarantined':False}
            state.update(phase='RESULT_PREPARED',pending_plan=plan,write_status='prepared')
            # Persist the sealed original before allocating any dispatch permit.
            # If this crashes, an explicit reviewed attempt can resume only this
            # exact plan; a consumed marker can never be recovered or reused.
            state=self._write(state)
            plan=self._saved_result_plan(state)
            return self._issue_result_attempt(state,plan,1)

    def result_retry_status(self):
        """Read-only metadata for the active controller's permission review."""
        with self.journal.locked():
            state=self.journal.read();self._validate(state);plan=self._saved_result_plan(state)
            count=self._result_attempts(state,plan)
            return {'operation_id':plan['operation_id'],'attempts_used':count,
                    'next_attempt':count+1 if count<RESULT_CAS_MAX_ATTEMPTS else None,
                    'max_attempts':RESULT_CAS_MAX_ATTEMPTS,'write_status':state['write_status'],
                    'quarantined':self._result_quarantined(state,plan),'plan_sha256':plan['plan_sha256'],
                    'required_revision_id':plan['body']['writeControl']['requiredRevisionId']}

    def retry_result(self,expected_operation_id,expected_attempt):
        """Issue one exact RESULT-only CAS after explicit controller review.

        This local protocol primitive is not a permission attestation. The
        active native controller must review the actual prior outcome and its
        current authority before calling; neither Docs nor saved booleans grant
        permission. The explicit operation AND attempt prevent an old review
        being reused for a subsequent dispatch. No automatic retry calls this.
        """
        with self.journal.locked():
            state=self.journal.read();self._validate(state);plan=self._saved_result_plan(state)
            require(expected_operation_id==plan['operation_id'],'result_publication_operation_mismatch')
            return self._issue_result_attempt(state,plan,expected_attempt)

    def accept_result(self,actual_response=None,readback=None):
        with self.journal.locked():
            state=self.journal.read();self._validate(state);require(state['phase']=='RESULT_PREPARED','result_not_pending')
            # Existing journals retain read-only exact acceptance/reconciliation;
            # only freshly sealed publications gain the new retry primitive.
            sealed=state['current'].get('result_publication') is not None
            if not sealed:
                operation=state['pending_plan']['operation_id']
                if isinstance(operation,str) and re.fullmatch('[0-9a-f]{32}',operation):
                    require(not self._result_seal_path(operation).exists(),'result_publication_seal_required_new_release')
            plan=self._saved_result_plan(state) if sealed else state['pending_plan']
            if sealed and self._result_quarantined(state,plan):
                return {'status':'unknown','reason':'result_publication_quarantined','quarantined':True}
            result=_accepted(plan,actual_response,readback)
            if result['status'] not in {'accepted','applied'}:
                if sealed and readback is not None:
                    try:
                        fresh=snapshot(readback,plan['document_id'],plan['source']['tab_id'],plan['source']['max_bytes'])
                        observed=parse_outbox(fresh,state['route'],self.key,state['grant'])
                    except ProtocolError:observed=None
                    if observed is not None and observed not in (state['record'],plan['record']):
                        path=self.journal.directory/('result-cas-'+plan['operation_id']+'-quarantine.once')
                        if not path.exists() and not path.is_symlink():
                            burn_fence(self.journal.directory,path.name,{'operation_id':plan['operation_id'],
                                       'observed_record_sha256':sha256(canonical(observed))})
                        state['current']['result_publication']['quarantined']=True
                if sealed:
                    result={**result,'status':'unknown','quarantined':self._result_quarantined(state,plan)}
                state['write_status']=result['status'];self._write(state);return result
            state['outbox']=result['snapshot'];state['record']=plan['record']
            state.update(pending_plan=None,write_status='accepted',phase='RESULT_COMMITTED')
            self._write(state);return result
