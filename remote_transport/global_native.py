"""Durable helper for an ALREADY ACTIVE native Router controller.

No networking, Google writes, automatic wake, or collaboration.spawn_agent occurs
here. The trusted active agent executes each returned real tool call once, then
provides exact tool evidence. A durable reservation is burned before tool args
are exposed; missing/unknown native results never permit replacement admission.
"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import time

from . import global_control as queue
from .backend import read_private_file
from .global_gateway import private_dir, private_write, strict_json
from .model import canonical, hash_bytes, require, ProtocolError
from .selection import admission_receipt
from .global_timing import controller_active, event_deadline, observation_window_current

MAX=8*1024*1024


class NativeLedger:
    def __init__(self,root,initial_state,join_code,actual_native_task_id):
        queue.verify(initial_state,join_code,require_fresh=False)
        queue.safe_id(actual_native_task_id)
        self.root=private_dir(root,create=True);self.code=join_code;self.identity=actual_native_task_id
        self.context=queue.root_of(initial_state);self.key=queue.root_hash(initial_state)
        self.path=self.root/(self.key+'.json');self.lock=self.root/(self.key+'.lock')
    @contextlib.contextmanager
    def locked(self):
        fd=os.open(self.lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(fd);require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid()
                and not info.st_mode&0o077,'unsafe_global_ledger_lock')
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if self.path.exists():saved=strict_json(read_private_file(self.path,MAX))
            else:saved={'contract':'dots-global-native-ledger/2','root':self.context,'actual_native_task_id':self.identity,
                        'event_hashes':[],'operations':{},'spawns':{},'observed_controller':None,
                        'observed_at':None,'terminal_reason':None};self.save(saved)
            require(saved.get('contract')=='dots-global-native-ledger/2' and saved.get('root')==self.context
                    and saved.get('actual_native_task_id')==self.identity,'global_ledger_identity_mismatch')
            yield saved
        finally:os.close(fd)
    def save(self,saved):private_write(self.path,canonical(saved,max_bytes=MAX))
    def child_state_dir(self,route_id):
        queue.token(route_id)
        return self.root/(self.key+'.children')/route_id
    def observe(self,saved,source):
        state=queue.verify(source.state,self.code,expected_root=self.context,require_fresh=False)
        now=time.time();c=state['logical']['controller'];old=saved['observed_controller']
        reason=saved['terminal_reason']
        if now<state['created'] or (state['events'] and now<state['events'][-1]['at']):
            reason=reason or 'global_controller_clock_rollback'
        if now>=state['expires']:reason=reason or 'global_controller_not_active_restart_required'
        if saved['observed_at'] is not None and now < saved['observed_at']:
            reason='global_controller_clock_rollback'
        if old is not None and not controller_active(old['heartbeat_at'],old['lease_expires'],
                                                    state['controller_timing'],now):
            reason=reason or 'global_controller_not_active_restart_required'
        if c is not None and not controller_active(c['heartbeat_at'],c['lease_expires'],
                                                  state['controller_timing'],now):
            reason=reason or 'global_controller_not_active_restart_required'
        if not observation_window_current(state,len(saved['event_hashes']),now):
            reason=reason or 'global_controller_not_active_prepared_event_expired'
        if state['logical']['closed']:reason='global_queue_closed'
        if reason:
            saved['terminal_reason']=reason;self.save(saved)
            raise ProtocolError(reason)
        hashes=[hash_bytes(canonical(e)) for e in state['events']]
        require(hashes[:len(saved['event_hashes'])]==saved['event_hashes'],'global_observation_rollback_or_fork')
        require(c is None or c['native_task_id']==self.identity,'global_actual_controller_identity_mismatch')
        saved['event_hashes']=hashes;saved['observed_controller']=c;saved['observed_at']=now;self.save(saved)
        return state
    def _new_path(self,path):
        path=Path(path).absolute();private_dir(path.parent)
        require(not path.exists() and not path.is_symlink(),'new_private_global_plan_path_required');return path
    def _issue(self,saved,source,kind,args,destination,now=None):
        state=self.observe(saved,source);now=int(time.time()) if now is None else now
        require(not any(r['status']!='verified' for r in saved['operations'].values()),
                'global_unresolved_cas_readonly_reconciliation_required')
        semantic=kind+':'+str(args.get('route_id',now if kind=='heartbeat' else 'controller'))
        require(semantic not in saved['operations'],'global_operation_already_issued_no_replay')
        destination=self._new_path(destination)
        new=queue.transition(state,self.code,kind,'native',args,now=now)
        packet=queue.plan(source,new,self.code);digest=hash_bytes(canonical(packet))
        saved['operations'][semantic]={'status':'issued_outcome_unknown','path':str(destination),'sha256':digest,
                                      'operation_id':packet['operation_id'],'kind':kind,'route_id':args.get('route_id')}
        self.save(saved);private_write(destination,canonical(packet))
        return {'plan_file':str(destination),'operation_id':packet['operation_id'],'tool_arguments':packet['tool_arguments'],
                'execute_before':packet['execute_before'],'one_attempt_only':True,'retry_on_unknown':False,'native_invoked_by_python':False}
    def inspect(self,source):
        with self.locked() as saved:
            state=self.observe(saved,source);c=state['logical']['controller']
            return {'contract':queue.CONTRACT,'activation_id':state['activation_id'],'expires':state['expires'],
                    'controller':c,'closed':state['logical']['closed'],'pending':[
                        {'route_id':d['route_id'],'selection':d['selection'],'expires':d['expires']}
                        for d in state['logical']['demands'].values() if d['state']=='pending'],
                    'controller_timing':state['controller_timing'],
                    'first_heartbeat_required':bool(c and c['heartbeat_at']==c['joined_at']),
                    'automatic_wake':False,'native_capacity_is_not_reserved_platform_slots':True}
    def plan_event(self,source,kind,destination,*,route_id=None,capacity=None,seconds=None,now=None):
        now=int(time.time()) if now is None else now
        with self.locked() as saved:
            state=self.observe(saved,source);c=state['logical']['controller']
            if kind=='join':
                require(type(capacity) is int and type(seconds) is int and seconds>=1,'global_controller_limits_required')
                args={'native_task_id':self.identity,'controller_epoch':secrets.token_hex(16),
                      'lease_expires':min(now+seconds,state['expires']),'capacity':capacity}
            else:
                require(c is not None,'global_controller_join_required')
                args={'native_task_id':self.identity,'controller_epoch':c['controller_epoch']}
                if kind=='claim':args.update(route_id=route_id,claim_id=secrets.token_hex(16))
                elif kind in ('begin','unknown','admitted'):
                    d=state['logical']['demands'].get(route_id);require(d is not None,'unknown_global_route')
                    args.update(route_id=route_id,claim_id=d['claim_id'])
                    if kind=='begin':args['dispatch_id']=queue.dispatch_id(state,route_id)
                    elif kind=='admitted':
                        record=saved['spawns'].get(route_id);require(record and record['status']=='recorded','global_recorded_native_result_required')
                        args.update(admission=record['receipt'],spawn_arguments_sha256=record['arguments_sha256'])
                else:require(kind=='heartbeat','unsupported_native_queue_event')
            return self._issue(saved,source,kind,args,destination,now=now)
    def check_plan(self,source,plan_path):
        packet=strict_json(read_private_file(plan_path,MAX));path=str(Path(plan_path).absolute())
        with self.locked() as saved:
            self.observe(saved,source)
            records=[r for r in saved['operations'].values() if r['path']==path]
            require(len(records)==1 and records[0]['status']=='issued_outcome_unknown'
                    and records[0]['sha256']==hash_bytes(canonical(packet)), 'global_plan_not_reserved')
            now=time.time()
            require(packet['expected_state']['events'][-1]['at']<=now<packet['execute_before'],
                    'global_cas_dispatch_window_expired_no_replay')
            return {'dispatch_allowed':True,'checked_at':now,'execute_before':packet['execute_before']}
    def verify_plan(self,plan_path,response,readback):
        packet=strict_json(read_private_file(plan_path,MAX));path=str(Path(plan_path).absolute())
        with self.locked() as saved:
            records=[r for r in saved['operations'].values() if r['path']==path]
            require(len(records)==1 and records[0]['sha256']==hash_bytes(canonical(packet)),'global_plan_not_reserved')
            record=records[0]
            try:
                fresh=queue.verify_update(packet,response,readback,self.code)
                self.observe(saved,fresh)
            except Exception as exc:
                # Diagnosis is read-only and cannot accept an event, refresh
                # liveness, or clear the burned CAS reservation. A valid closed
                # readback and an unobserved event are independent facts: neither
                # proves that the attempted write was never dispatched/committed.
                diagnostic={'authenticated':False,'queue_closed':None,'expected_event_observed':None}
                try:
                    candidate=queue.snapshot(readback,self.context['document_id'],self.context['tab_id'])
                    state=queue.verify(candidate.state,self.code,expected_root=self.context,require_fresh=False)
                    diagnostic={'authenticated':True,'queue_closed':state['logical']['closed'],
                        'expected_event_observed':state['epoch']>=packet['expected_state']['epoch']
                            and state['events'][:packet['expected_state']['epoch']]==packet['expected_state']['events']}
                except Exception:
                    pass
                record['status']='outcome_unknown_no_replay'
                record['last_failure']={'code':str(exc) if isinstance(exc,ProtocolError) else type(exc).__name__,
                                        'readback':diagnostic}
                self.save(saved)
                exc.controller_diagnostic=diagnostic
                raise
            record.update(status='verified',readback_hash=hash_bytes(canonical(readback)),revision=fresh.revision_id)
            self.save(saved)
            c=fresh.state['logical']['controller']
            return {'verified':True,'epoch':fresh.state['epoch'],'operation_id':packet['operation_id'],
                    'first_heartbeat_required':bool(c and c['heartbeat_at']==c['joined_at']),
                    'heartbeat_due_at':c['heartbeat_at']+self.context['controller_timing']['heartbeat_interval_seconds'] if c else None,
                    'reconciled_from_event':response is None,'native_retry_allowed':False}
    def plan_spawn(self,source,route_id,destination,package_root):
        with self.locked() as saved:
            state=self.observe(saved,source)
            require(not any(r['status']!='verified' for r in saved['operations'].values()),
                    'global_unresolved_cas_readonly_reconciliation_required')
            begin=saved['operations'].get('begin:'+route_id)
            require(begin and begin['status']=='verified','verified_global_begin_required')
            require(route_id not in saved['spawns'],'global_native_attempt_already_reserved_no_replay')
            d=state['logical']['demands'].get(route_id);require(d and d['state']=='spawn_intent','global_spawn_intent_required')
            packet0=strict_json(read_private_file(begin['path'],MAX))
            require(d['dispatch_id']==packet0['expected_state']['logical']['demands'][route_id]['dispatch_id'],
                    'global_native_dispatch_mismatch')
            child_directory=self.child_state_dir(route_id)
            require(not child_directory.exists() and not child_directory.is_symlink(),
                    'fresh_global_child_ledger_directory_required')
            private_dir(child_directory,create=True)
            args=queue.native_arguments(state,self.code,route_id,package_root,child_directory);destination=self._new_path(destination)
            packet={'contract':'dots-global-native-plan/2','queue_root_hash':self.key,'route_id':route_id,
                    'dispatch_id':d['dispatch_id'],'selection':d['selection'],'child_state_dir':str(child_directory),
                    'created':time.time(),'execute_before':min(event_deadline(state,'begin',time.time()),
                        time.time()+state['controller_timing']['spawn_check_seconds'],d['child_bootstrap']['expires']),
                    'tool':'collaboration.spawn_agent','arguments':args}
            saved['spawns'][route_id]={'status':'reserved_outcome_unknown','plan_path':str(destination),
                                       'plan_sha256':hash_bytes(canonical(packet)),'receipt':None}
            self.save(saved);private_write(destination,canonical(packet))
            return {'plan_file':str(destination),'tool':'collaboration.spawn_agent','arguments':args,
                    'execute_before':packet['execute_before'],
                    'one_attempt_only':True,'native_invoked_by_python':False,
                    'next':'Active trusted Router calls the real native tool once. Record exact arguments and returned task_name. Never retry unknown admission.'}
    def check_spawn(self,source,plan_path):
        packet=strict_json(read_private_file(plan_path,MAX));rid=packet.get('route_id')
        with self.locked() as saved:
            state=self.observe(saved,source);record=saved['spawns'].get(rid)
            require(not any(r['status']!='verified' for r in saved['operations'].values()),
                    'global_unresolved_cas_readonly_reconciliation_required')
            require(record and record['status']=='reserved_outcome_unknown'
                    and record['plan_sha256']==hash_bytes(canonical(packet)), 'global_native_plan_mismatch')
            require(packet['created']<=time.time()<packet['execute_before'],
                    'global_native_dispatch_window_expired_no_replay')
            d=state['logical']['demands'].get(rid)
            require(d is not None and d['state']=='spawn_intent' and d['dispatch_id']==packet['dispatch_id'],
                    'global_native_dispatch_mismatch')
            return {'dispatch_allowed':True,'execute_before':packet['execute_before'],'retry_allowed':False}
    def record_spawn(self,plan_path,actual_arguments,actual_result,receipt_path):
        packet=strict_json(read_private_file(plan_path,MAX));rid=packet.get('route_id')
        with self.locked() as saved:
            record=saved['spawns'].get(rid)
            require(record and record['status']=='reserved_outcome_unknown'
                    and record['plan_path']==str(Path(plan_path).absolute())
                    and record['plan_sha256']==hash_bytes(canonical(packet)),'global_native_plan_mismatch')
            require(actual_arguments==packet['arguments'],'global_actual_native_arguments_mismatch')
            require(isinstance(actual_result,dict) and not actual_result.get('error')
                    and isinstance(actual_result.get('task_name'),str),'global_successful_native_result_required')
            receipt=admission_receipt(packet['selection'],actual_arguments,actual_result['task_name'])
            receipt_path=self._new_path(receipt_path)
            record.update(status='recorded',receipt=receipt,result_sha256=hash_bytes(canonical(actual_result)),
                          arguments_sha256=hash_bytes(canonical(actual_arguments)),receipt_path=str(receipt_path))
            self.save(saved);private_write(receipt_path,canonical(receipt))
            return {'admission_receipt_file':str(receipt_path),'native_task_id':receipt['native_task_id'],
                    'underlying_model_verified':False,'next':'Publish and verify the admitted queue event, then run import-child-admission with this exact receipt. Only after verified import send the receipt and fixed child state-dir to the same child. No new spawn.'}

    def import_child_admission(self,source,route_id,receipt_path,actual_native_task_id):
        """One parent-authorized bridge between two distinct local ledgers.

        Reserve before touching child evidence. An interrupted reservation can
        only reconcile the exact already-written record, never seed another
        ledger, identity or spawn. No external tool is called by this method.
        """
        from .router_join import JoinLedger
        from . import router_bootstrap as child
        with self.locked() as saved:
            state=self.observe(saved,source)
            require(not any(r['status']!='verified' for r in saved['operations'].values()),
                    'global_unresolved_cas_readonly_reconciliation_required')
            d=state['logical']['demands'].get(route_id);c=state['logical']['controller']
            require(d is not None and d['state']=='admitted' and c is not None and
                    d['controller_epoch']==c['controller_epoch'] and time.time()<d['expires'],
                    'global_admitted_owned_child_required')
            record=saved['spawns'].get(route_id)
            require(record and record['status']=='recorded','global_recorded_native_result_required')
            receipt=strict_json(read_private_file(receipt_path,MAX))
            require(str(Path(receipt_path).absolute())==record['receipt_path'] and
                    receipt==record['receipt']==d['admission'] and
                    receipt['native_task_id']==actual_native_task_id,
                    'global_child_actual_admission_mismatch')
            packet=strict_json(read_private_file(record['plan_path'],MAX))
            directory=self.child_state_dir(route_id)
            require(record['plan_sha256']==hash_bytes(canonical(packet)) and
                    packet['queue_root_hash']==self.key and packet['route_id']==route_id and
                    packet['dispatch_id']==d['dispatch_id'] and packet['selection']==d['selection'] and
                    packet['child_state_dir']==str(directory) and
                    hash_bytes(canonical(packet['arguments']))==record['arguments_sha256']==d['spawn_arguments_sha256'] and
                    admission_receipt(d['selection'],packet['arguments'],actual_native_task_id)==receipt,
                    'global_child_spawn_binding_mismatch')
            admitted=saved['operations'].get('admitted:'+route_id)
            require(admitted and admitted['status']=='verified','verified_global_admitted_required')
            cas=strict_json(read_private_file(admitted['path'],MAX));expected=cas['expected_state']
            require(admitted['sha256']==hash_bytes(canonical(cas)) and
                    admitted['operation_id']==cas['operation_id'] and
                    state['events'][:expected['epoch']]==expected['events'] and
                    expected['events'][-1]['kind']=='admitted' and
                    expected['events'][-1]['arguments']['route_id']==route_id and
                    expected['logical']['demands'][route_id]==d,
                    'global_child_verified_admitted_binding_mismatch')
            bootstrap=d['child_bootstrap'];cc=queue.child_code(self.code,state['activation_id'],route_id)
            child.verify_context(bootstrap,cc,bootstrap['bootstrap_document_id'],bootstrap['bootstrap_tab_id'])
            require(bootstrap['stage']=='WAITING_FOR_WORKER' and not bootstrap['events'] and
                    bootstrap['session_id']==route_id and bootstrap['required_selection']==d['selection'],
                    'global_child_bootstrap_binding_mismatch')
            # Never accept caller-selected destinations or symlink traversal.
            private_dir(directory)
            provenance={'contract':'dots-global-child-admission-import/1','queue_root_hash':self.key,
                'route_id':route_id,'controller_epoch':d['controller_epoch'],'claim_id':d['claim_id'],
                'dispatch_id':d['dispatch_id'],'admitted_epoch':expected['epoch'],
                'admitted_event_sha256':hash_bytes(canonical(expected['events'][-1])),
                'child_context_hash':child.context_hash(bootstrap),'child_state_dir':str(directory),
                'receipt_sha256':hash_bytes(canonical(receipt)),
                'spawn_plan_sha256':record['plan_sha256'],'arguments_sha256':record['arguments_sha256'],
                'result_sha256':record['result_sha256']}
            pending=record.get('child_admission_import')
            if pending is None:
                # Must be genuinely unused, even if a caller selected this path
                # early. Do not replace an existing child pairing ledger.
                require(not any(directory.iterdir()),'fresh_parent_child_ledger_required')
                pending={'status':'reserved_outcome_unknown','provenance':provenance}
                record['child_admission_import']=pending;self.save(saved)
                readback_only=False
            else:
                require(pending['provenance']==provenance,'global_child_import_binding_mismatch')
                readback_only=True
            child_ledger=JoinLedger(directory,bootstrap)
            if readback_only:
                require(child_ledger.path.exists(),'parent_child_import_outcome_unknown_no_replay')
            child_ledger.import_parent_admission(receipt,provenance,readback_only=readback_only)
            if pending['status']!='imported':
                pending['status']='imported';self.save(saved)
            return {'imported':True,'reconciled_existing':readback_only,'route_id':route_id,
                    'native_task_id':actual_native_task_id,'child_state_dir':str(directory),
                    'admission_receipt_file':record['receipt_path'],'underlying_model_verified':False,
                    'native_retry_allowed':False}


def emit_cell(ledger,source,destination,package_root,join_code_file,operation,capacity,seconds,route_id=None):
    """Emit complete reviewed one-shot source; never executes tools or wakes agents."""
    require(operation in ('join','heartbeat','claim-begin'),'invalid_global_cell_operation')
    if operation=='claim-begin':queue.token(route_id)
    ledger.inspect(source)
    package=Path(package_root).absolute()
    for relative,digest in ledger.context['controller_source_hashes'].items():
        path=package/relative
        require(path.is_file() and not path.is_symlink() and hash_bytes(path.read_bytes())==digest,
                'global_controller_cell_source_mismatch')
    adapter=package/'native_connector/tool_adapter.js'
    require(adapter.is_file() and not adapter.is_symlink()
            and hash_bytes(adapter.read_bytes())==ledger.context['runtime_source_hashes']['native_connector/tool_adapter.js'],
            'global_controller_cell_source_mismatch')
    config={'cwd':str(package),'stateDir':str(ledger.root),'nativeTaskId':ledger.identity,
            'documentId':source.document_id,'tabId':source.tab_id,'joinCodeFile':str(Path(join_code_file).absolute())}
    capture={'cwd':str(package),'root':str(ledger.root),'nativeTaskId':ledger.identity}
    call=('joinAndFirstHeartbeat('+json.dumps({'capacity':capacity,'seconds':seconds})+')'
          if operation=='join' else 'claimAndBegin('+json.dumps({'routeId':route_id})+')'
          if operation=='claim-begin' else 'heartbeat()')
    script=('// @exec: {"yield_time_ms": 1000, "max_output_tokens": 300}\n'
            +adapter.read_text()+'\n'+(package/'native_connector/global_controller_cell.js').read_text()
            +'\nconst privateCapture=createNativeToolAdapter(tools,'+json.dumps(capture)+');\n'
            +'const activeController=createGlobalControllerToolAdapter(tools,'+json.dumps(config)+',privateCapture.captureValue);\n'
            +'text(await activeController.cell.'+call+');\n')
    path=ledger._new_path(destination);private_write(path,script.encode())
    return {'cell_file':str(path),'sha256':hash_bytes(script.encode()),'operation':operation,
            'executes_on_emission':False,'requires_active_native_agent':True}


def main():
    p=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    p.add_argument('operation',choices=['inspect','plan-join','plan-heartbeat','plan-claim','plan-begin','plan-unknown',
                                       'plan-admitted','emit-cell','verify','check-cas','plan-native','check-native','record-native',
                                       'import-child-admission'])
    for arg in ('snapshot','document-id','tab-id','join-code-file','state-dir','native-task-id'):
        p.add_argument('--'+arg,required=True)
    for arg in ('route-id','save','result-file','plan-file','response','readback','package-root','actual-arguments','native-result',
                'admission-receipt','child-native-task-id'):
        p.add_argument('--'+arg)
    p.add_argument('--cell-operation',choices=['join','heartbeat','claim-begin'])
    p.add_argument('--capacity',type=int,default=2);p.add_argument('--seconds',type=int,default=3600)
    a=p.parse_args();os.umask(0o077)
    def read(path):return strict_json(read_private_file(path,MAX))
    code=read_private_file(a.join_code_file,256).decode('ascii').strip()
    source=queue.snapshot(read(a.snapshot),a.document_id,a.tab_id)
    ledger=NativeLedger(a.state_dir,source.state,code,a.native_task_id)
    if a.operation=='emit-cell':value=emit_cell(ledger,source,a.save,a.package_root,a.join_code_file,a.cell_operation,a.capacity,a.seconds,a.route_id)
    elif a.operation=='inspect':value=ledger.inspect(source)
    elif a.operation=='check-cas':value=ledger.check_plan(source,a.plan_file)
    elif a.operation=='verify':value=ledger.verify_plan(a.plan_file,read(a.response) if a.response else None,read(a.readback))
    elif a.operation=='plan-native':value=ledger.plan_spawn(source,a.route_id,a.save,a.package_root)
    elif a.operation=='check-native':value=ledger.check_spawn(source,a.plan_file)
    elif a.operation=='record-native':value=ledger.record_spawn(a.plan_file,read(a.actual_arguments),read(a.native_result),a.save)
    elif a.operation=='import-child-admission':value=ledger.import_child_admission(source,a.route_id,a.admission_receipt,a.child_native_task_id)
    else:value=ledger.plan_event(source,a.operation.removeprefix('plan-'),a.save,route_id=a.route_id,capacity=a.capacity,seconds=a.seconds)
    if a.result_file:
        destination=ledger._new_path(a.result_file);private_write(destination,canonical(value,max_bytes=MAX))
        print(json.dumps({'result_file':str(destination)}))
    else:print(json.dumps(value,ensure_ascii=False))

if __name__=='__main__':
    try:main()
    except Exception as exc:
        error={'error':str(exc) if isinstance(exc,ProtocolError) else type(exc).__name__}
        diagnostic=getattr(exc,'controller_diagnostic',None)
        if diagnostic is not None:error['diagnostic']=diagnostic
        print(json.dumps(error));raise SystemExit(1)
