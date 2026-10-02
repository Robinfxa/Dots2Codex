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
import sys
import time

from . import global_control as queue
from . import global_handoff as handoff
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
    def handoff_path(self,route_id):
        queue.token(route_id)
        return self.root/(self.key+'.handoffs')/(route_id+'.json')
    def _verified_begin(self,saved,route_id,state=None):
        """Resolve one complete accepted single/group plan, never a partial pair."""
        records=[saved['operations'][key] for key in ('begin:'+route_id,'claim-begin:'+route_id,'heartbeat-claim-begin:'+route_id)
                 if key in saved['operations']]
        require(len(records)==1 and records[0]['status']=='verified','verified_global_begin_required')
        record=records[0];packet=strict_json(read_private_file(record['path'],MAX))
        require(record['sha256']==hash_bytes(canonical(packet)),'global_child_spawn_binding_mismatch')
        queue.validate_plan(packet,self.code)
        require(all(record.get(k)==v for k,v in queue.plan_identity(packet).items())
                and record['kind'] in ('begin','claim-begin','heartbeat-claim-begin') and record['route_id']==route_id
                and (record['kind']!='begin')==(packet['contract']==queue.GROUP_PLAN_CONTRACT)
                and (packet['contract']!=queue.GROUP_PLAN_CONTRACT or packet['group_kind']==record['kind']),
                'global_child_spawn_binding_mismatch')
        expected=packet['expected_state'];last=expected['events'][-1]
        require(last['kind']=='begin' and last['arguments']['route_id']==route_id
                and last['arguments']['native_task_id']==self.identity
                and queue.root_of(expected)==self.context,'global_child_spawn_binding_mismatch')
        if state is not None:
            require(state['events'][:expected['epoch']]==expected['events'],
                    'global_child_spawn_binding_mismatch')
        return packet
    def _verify_handoff(self,saved,packet,record,*,fresh=True,state=None):
        ref=packet.get('handoff')
        require(ref==record.get('handoff') and ref is not None,
                'global_native_handoff_binding_mismatch')
        value=handoff.read(ref,fresh=fresh)
        rid=packet['route_id'];original=self._verified_begin(saved,rid,state)
        expected=handoff.build(original['expected_state'],self.code,rid,packet['package_root'],
            self.child_state_dir(rid),self.handoff_path(rid),created=packet['created'],fresh=False)
        require(value==expected and packet['contract']=='dots-global-native-plan/3' and
                packet['queue_root_hash']==self.key and packet['dispatch_id']==value['dispatch_id'] and
                packet['selection']==value['selection'] and packet['child_state_dir']==value['state_dir'] and
                packet['arguments']==handoff.arguments(value,ref),'global_native_handoff_binding_mismatch')
        return value
    def _recorded_admission(self,saved,state,route_id):
        """Return only admission bound to the exact durably recorded native call."""
        record=saved['spawns'].get(route_id)
        require(record and record['status']=='recorded','global_recorded_native_result_required')
        packet=strict_json(read_private_file(record['plan_path'],MAX))
        require(record['plan_sha256']==hash_bytes(canonical(packet)),
                'global_child_spawn_binding_mismatch')
        self._verify_handoff(saved,packet,record,state=state)
        d=state['logical']['demands'].get(route_id)
        receipt=strict_json(read_private_file(record['receipt_path'],MAX))
        require(d and packet['route_id']==route_id and packet['dispatch_id']==d['dispatch_id']
                and packet['selection']==d['selection'] and receipt==record['receipt']
                and hash_bytes(canonical(packet['arguments']))==record['arguments_sha256']
                and admission_receipt(d['selection'],packet['arguments'],receipt['native_task_id'])==receipt,
                'global_child_actual_admission_mismatch')
        return {'admission':receipt,'spawn_arguments_sha256':record['arguments_sha256']}
    def _verified_admitted(self,saved,route_id,state):
        """Accept one exact verified admitted event, alone or in its allowed pair."""
        records=[saved['operations'][key] for key in ('admitted:'+route_id,'heartbeat-admitted:'+route_id)
                 if key in saved['operations']]
        require(len(records)==1 and records[0]['status']=='verified','verified_global_admitted_required')
        record=records[0];packet=strict_json(read_private_file(record['path'],MAX))
        require(record['sha256']==hash_bytes(canonical(packet)),
                'global_child_verified_admitted_binding_mismatch')
        queue.validate_plan(packet,self.code)
        require(all(record.get(k)==v for k,v in queue.plan_identity(packet).items())
                and record['kind'] in ('admitted','heartbeat-admitted') and record['route_id']==route_id
                and (record['kind']=='heartbeat-admitted')==(packet['contract']==queue.GROUP_PLAN_CONTRACT)
                and (packet['contract']!=queue.GROUP_PLAN_CONTRACT or packet['group_kind']=='heartbeat-admitted'),
                'global_child_verified_admitted_binding_mismatch')
        expected=packet['expected_state'];last=expected['events'][-1]
        require(queue.root_of(expected)==self.context
                and state['events'][:expected['epoch']]==expected['events']
                and last['kind']=='admitted' and last['arguments']['route_id']==route_id
                and last['arguments']['native_task_id']==self.identity
                and expected['logical']['demands'][route_id]==state['logical']['demands'][route_id],
                'global_child_verified_admitted_binding_mismatch')
        return packet
    def observe(self,saved,source):
        require(source.document_id==self.context['document_id'] and source.tab_id==self.context['tab_id'],
                'global_document_mismatch')
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
        if kind=='join':
            require('join-heartbeat:controller' not in saved['operations'],
                    'global_operation_already_issued_no_replay')
        if kind in ('claim','begin'):
            require(not any(k+':'+str(args.get('route_id')) in saved['operations']
                            for k in ('claim-begin','heartbeat-claim-begin')),
                    'global_operation_already_issued_no_replay')
        if kind=='admitted':
            require('heartbeat-admitted:'+str(args.get('route_id')) not in saved['operations'],
                    'global_operation_already_issued_no_replay')
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
    def _heartbeat_ready_handoff(self,state):
        """Yield the admitted->ready writer turn before reserving any heartbeat.

        This is conflict avoidance, not evidence that an attempted CAS failed.
        Every admitted route participates: one ready child cannot release the
        handoff while another child may still publish readiness. The caller
        must obtain a new signed snapshot on its next bounded invocation.
        """
        pending=[d for d in state['logical']['demands'].values() if d['state']=='admitted']
        if not pending:return None
        c=state['logical']['controller'];checked=time.time()
        deadline=min(state['expires'],c['lease_expires'],
                     c['heartbeat_at']+state['controller_timing']['freshness_seconds'],
                     *(min(d['expires'],d['child_bootstrap']['expires']) for d in pending))
        require(checked<deadline,'global_heartbeat_readiness_window_expired')
        return {'action':'heartbeat_deferred_for_ready','read_only':True,
                'write_attempted':False,'heartbeat_verified':False,'native_spawn_allowed':False,
                'pending_ready_count':len(pending),'heartbeat_at':c['heartbeat_at'],
                'checked_at':checked,'observe_before':deadline,
                'recheck_after_seconds':min(state['controller_timing']['heartbeat_interval_seconds'],deadline-checked)}
    def plan_event(self,source,kind,destination,*,route_id=None,capacity=None,seconds=None,now=None):
        if kind=='join-heartbeat':
            require(now is None,'global_join_heartbeat_requires_host_clock')
            return self.plan_join_heartbeat(source,destination,capacity,seconds)
        if kind=='claim-begin':return self.plan_claim_begin(source,route_id,destination,now=now)
        if kind=='claim-startup':return self.plan_claim_begin(source,route_id,destination,now=now,heartbeat_if_due=True)
        if kind=='heartbeat-admitted':return self.plan_heartbeat_admitted(source,route_id,destination,now=now)
        now=int(time.time()) if now is None else now
        with self.locked() as saved:
            state=self.observe(saved,source);c=state['logical']['controller']
            require(not any(r['status']!='verified' for r in saved['operations'].values()),
                    'global_unresolved_cas_readonly_reconciliation_required')
            if kind=='join':
                require(type(capacity) is int and type(seconds) is int and seconds>=1,'global_controller_limits_required')
                args={'native_task_id':self.identity,'controller_epoch':secrets.token_hex(16),
                      'lease_expires':min(now+seconds,state['expires']),'capacity':capacity}
            else:
                require(c is not None,'global_controller_join_required')
                if kind=='heartbeat':
                    # The lock, authenticated snapshot, original authority and
                    # unresolved-operation fence all precede this no-write path.
                    # Do not allocate a plan file or burn/release a reservation.
                    deferred=self._heartbeat_ready_handoff(state)
                    if deferred is not None:return deferred
                args={'native_task_id':self.identity,'controller_epoch':c['controller_epoch']}
                if kind=='claim':args.update(route_id=route_id,claim_id=secrets.token_hex(16))
                elif kind in ('begin','unknown','admitted'):
                    d=state['logical']['demands'].get(route_id);require(d is not None,'unknown_global_route')
                    args.update(route_id=route_id,claim_id=d['claim_id'])
                    if kind=='begin':args['dispatch_id']=queue.dispatch_id(state,route_id)
                    elif kind=='admitted':
                        args.update(self._recorded_admission(saved,state,route_id))
                else:require(kind=='heartbeat','unsupported_native_queue_event')
            return self._issue(saved,source,kind,args,destination,now=now)
    def plan_join_heartbeat(self,source,destination,capacity,seconds):
        """Sample both real times; bounded host wait never invents a heartbeat."""
        with self.locked() as saved:
            state=self.observe(saved,source)
            require(not any(r['status']!='verified' for r in saved['operations'].values()),
                    'global_unresolved_cas_readonly_reconciliation_required')
            require(not any(kind+':controller' in saved['operations'] for kind in ('join','join-heartbeat')),
                    'global_operation_already_issued_no_replay')
            require(type(capacity) is int and type(seconds) is int and seconds>=1,
                    'global_controller_limits_required')
            destination=self._new_path(destination)
            started=time.monotonic();wall=time.time();joined_at=int(wall)
            args={'native_task_id':self.identity,'controller_epoch':secrets.token_hex(16),
                  'lease_expires':min(joined_at+seconds,state['expires']),'capacity':capacity}
            joined=queue.transition(state,self.code,'join','native',args,now=joined_at)
            remaining=joined_at+1-time.time()
            if remaining>0:
                require(remaining<=1.001,'global_join_heartbeat_clock_changed')
                time.sleep(remaining+.001)
            sampled=time.time();heartbeat_at=int(sampled)
            require(sampled>=wall and heartbeat_at==joined_at+1 and time.monotonic()-started<=1.1,
                    'global_join_heartbeat_clock_changed')
            self.observe(saved,source)
            ticked=queue.transition(joined,self.code,'heartbeat','native',
                {k:args[k] for k in ('native_task_id','controller_epoch')},now=heartbeat_at)
            packet=queue.plan_join_heartbeat(source,ticked,self.code)
            saved['operations']['join-heartbeat:controller']={
                'status':'issued_outcome_unknown','path':str(destination),'sha256':hash_bytes(canonical(packet)),
                **queue.plan_identity(packet),'kind':'join-heartbeat','route_id':None}
            self.save(saved);private_write(destination,canonical(packet))
            return {'plan_file':str(destination),**queue.plan_identity(packet),'tool_arguments':packet['tool_arguments'],
                    'execute_before':packet['execute_before'],'one_attempt_only':True,'retry_on_unknown':False,
                    'native_invoked_by_python':False}
    def plan_claim_begin(self,source,route_id,destination,*,now=None,heartbeat_if_due=False):
        """Durably reserve one atomic claim/begin CAS before exposing arguments."""
        queue.token(route_id);now=int(time.time()) if now is None else now
        with self.locked() as saved:
            state=self.observe(saved,source);c=state['logical']['controller']
            require(c is not None,'global_controller_join_required')
            require(not any(r['status']!='verified' for r in saved['operations'].values()),
                    'global_unresolved_cas_readonly_reconciliation_required')
            require(not any(kind+':'+route_id in saved['operations']
                            for kind in ('claim','begin','claim-begin','heartbeat-claim-begin')),
                    'global_operation_already_issued_no_replay')
            destination=self._new_path(destination)
            args={'native_task_id':self.identity,'controller_epoch':c['controller_epoch'],
                  'route_id':route_id,'claim_id':secrets.token_hex(16)}
            due=heartbeat_if_due and now>=c['heartbeat_at']+state['controller_timing']['heartbeat_interval_seconds']
            if due:
                state=queue.transition(state,self.code,'heartbeat','native',
                    {k:args[k] for k in ('native_task_id','controller_epoch')},now=now)
            claimed=queue.transition(state,self.code,'claim','native',args,now=now)
            begun=queue.transition(claimed,self.code,'begin','native',
                {**args,'dispatch_id':queue.dispatch_id(claimed,route_id)},now=now)
            packet=(queue.plan_heartbeat_claim_begin if due else queue.plan_claim_begin)(source,begun,self.code)
            kind=packet['group_kind']
            saved['operations'][kind+':'+route_id]={
                'status':'issued_outcome_unknown','path':str(destination),'sha256':hash_bytes(canonical(packet)),
                **queue.plan_identity(packet),'kind':kind,'route_id':route_id}
            # One group record is the durable fence for both component semantics.
            # A crash or failed output write cannot free either event for retry.
            self.save(saved);private_write(destination,canonical(packet))
            return {'plan_file':str(destination),**queue.plan_identity(packet),'tool_arguments':packet['tool_arguments'],
                    'execute_before':packet['execute_before'],'one_attempt_only':True,'retry_on_unknown':False,
                    'native_invoked_by_python':False}
    def plan_heartbeat_admitted(self,source,route_id,destination,*,now=None):
        """One CAS only after the genuine native outcome is durably recorded."""
        queue.token(route_id);explicit_time=now is not None
        with self.locked() as saved:
            state=self.observe(saved,source);c=state['logical']['controller']
            require(c is not None,'global_controller_join_required')
            require(not any(r['status']!='verified' for r in saved['operations'].values()),
                    'global_unresolved_cas_readonly_reconciliation_required')
            require(not any(kind+':'+route_id in saved['operations'] for kind in ('admitted','heartbeat-admitted')),
                    'global_operation_already_issued_no_replay')
            evidence=self._recorded_admission(saved,state,route_id)
            destination=self._new_path(destination)
            if not explicit_time:
                started=time.monotonic();wall=time.time();now=int(wall)
                if now<=c['heartbeat_at']:
                    remaining=c['heartbeat_at']+1-time.time()
                    if remaining>0:
                        require(remaining<=1.001,'global_admitted_heartbeat_clock_changed')
                        time.sleep(remaining+.001)
                    sampled=time.time();now=int(sampled)
                    require(sampled>=wall and now==c['heartbeat_at']+1 and time.monotonic()-started<=1.1,
                            'global_admitted_heartbeat_clock_changed')
                    # Waiting can expire the lease. No refresh after that point.
                    state=self.observe(saved,source)
            heartbeat_args={'native_task_id':self.identity,'controller_epoch':c['controller_epoch']}
            ticked=queue.transition(state,self.code,'heartbeat','native',heartbeat_args,now=now)
            admitted=queue.transition(ticked,self.code,'admitted','native',
                {**heartbeat_args,'route_id':route_id,
                 'claim_id':state['logical']['demands'][route_id]['claim_id'],**evidence},now=now)
            packet=queue.plan_heartbeat_admitted(source,admitted,self.code)
            saved['operations']['heartbeat-admitted:'+route_id]={
                'status':'issued_outcome_unknown','path':str(destination),'sha256':hash_bytes(canonical(packet)),
                **queue.plan_identity(packet),'kind':'heartbeat-admitted','route_id':route_id}
            self.save(saved);private_write(destination,canonical(packet))
            return {'plan_file':str(destination),**queue.plan_identity(packet),'tool_arguments':packet['tool_arguments'],
                    'execute_before':packet['execute_before'],'one_attempt_only':True,'retry_on_unknown':False,
                    'native_invoked_by_python':False}
    def check_plan(self,source,plan_path):
        packet=strict_json(read_private_file(plan_path,MAX));path=str(Path(plan_path).absolute())
        with self.locked() as saved:
            self.observe(saved,source)
            records=[r for r in saved['operations'].values() if r['path']==path]
            require(len(records)==1 and records[0]['status']=='issued_outcome_unknown'
                    and records[0]['sha256']==hash_bytes(canonical(packet)), 'global_plan_not_reserved')
            queue.validate_plan(packet,self.code)
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
            return {'verified':True,'epoch':fresh.state['epoch'],**queue.plan_identity(packet),
                    'first_heartbeat_required':bool(c and c['heartbeat_at']==c['joined_at']),
                    'heartbeat_due_at':c['heartbeat_at']+self.context['controller_timing']['heartbeat_interval_seconds'] if c else None,
                    'reconciled_from_event':response is None,'native_retry_allowed':False}
    def plan_spawn(self,source,route_id,destination,package_root):
        with self.locked() as saved:
            state=self.observe(saved,source)
            require(not any(r['status']!='verified' for r in saved['operations'].values()),
                    'global_unresolved_cas_readonly_reconciliation_required')
            packet0=self._verified_begin(saved,route_id,state)
            require(route_id not in saved['spawns'],'global_native_attempt_already_reserved_no_replay')
            d=state['logical']['demands'].get(route_id);require(d and d['state']=='spawn_intent','global_spawn_intent_required')
            require(d['dispatch_id']==packet0['expected_state']['logical']['demands'][route_id]['dispatch_id'],
                    'global_native_dispatch_mismatch')
            child_directory=self.child_state_dir(route_id)
            require(not child_directory.exists() and not child_directory.is_symlink(),
                    'fresh_global_child_ledger_directory_required')
            handoff_path=self.handoff_path(route_id)
            require(not handoff_path.exists() and not handoff_path.is_symlink(),
                    'new_private_global_handoff_path_required')
            created=time.time();destination=self._new_path(destination)
            descriptor=handoff.build(state,self.code,route_id,package_root,child_directory,handoff_path,created=created)
            ref=handoff.reference(descriptor);args=handoff.arguments(descriptor,ref)
            packet={'contract':'dots-global-native-plan/3','queue_root_hash':self.key,'route_id':route_id,
                    'dispatch_id':d['dispatch_id'],'selection':d['selection'],'child_state_dir':str(child_directory),
                    'package_root':descriptor['package_root'],'handoff':ref,
                    'created':created,'execute_before':min(event_deadline(state,'begin',created),
                        created+state['controller_timing']['spawn_check_seconds'],d['child_bootstrap']['expires']),
                    'tool':'collaboration.spawn_agent','arguments':args}
            saved['spawns'][route_id]={'status':'reserved_outcome_unknown','plan_path':str(destination),
                                       'plan_sha256':hash_bytes(canonical(packet)),'handoff':ref,'receipt':None}
            # Every local output, including directory allocation, is after this
            # durable one-attempt fence. A partial/missing output stays burned.
            self.save(saved)
            private_dir(child_directory,create=True);private_dir(handoff_path.parent,create=True)
            handoff.write_new(handoff_path,canonical(descriptor,max_bytes=handoff.MAX))
            handoff.write_new(destination,canonical(packet))
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
                    and record['plan_path']==str(Path(plan_path).absolute())
                    and record['plan_sha256']==hash_bytes(canonical(packet)), 'global_native_plan_mismatch')
            require(packet['created']<=time.time()<packet['execute_before'],
                    'global_native_dispatch_window_expired_no_replay')
            d=state['logical']['demands'].get(rid)
            require(d is not None and d['state']=='spawn_intent' and d['dispatch_id']==packet['dispatch_id'],
                    'global_native_dispatch_mismatch')
            self._verify_handoff(saved,packet,record,state=state)
            checked_at=time.time()
            require(packet['created']<=checked_at<packet['execute_before'],
                    'global_native_dispatch_window_expired_no_replay')
            return {'dispatch_allowed':True,'checked_at':checked_at,'execute_before':packet['execute_before'],'retry_allowed':False}
    def record_spawn(self,plan_path,actual_arguments,actual_result,receipt_path):
        packet=strict_json(read_private_file(plan_path,MAX));rid=packet.get('route_id')
        with self.locked() as saved:
            record=saved['spawns'].get(rid)
            require(record and record['status']=='reserved_outcome_unknown'
                    and record['plan_path']==str(Path(plan_path).absolute())
                    and record['plan_sha256']==hash_bytes(canonical(packet)),'global_native_plan_mismatch')
            # An actual dispatch can return late. Preserve its real result even
            # after expiry; this grants no liveness or downstream admission.
            self._verify_handoff(saved,packet,record,fresh=False)
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
            descriptor=self._verify_handoff(saved,packet,record,state=state)
            directory=self.child_state_dir(route_id)
            require(record['plan_sha256']==hash_bytes(canonical(packet)) and
                    packet['queue_root_hash']==self.key and packet['route_id']==route_id and
                    packet['dispatch_id']==d['dispatch_id'] and packet['selection']==d['selection'] and
                    packet['child_state_dir']==str(directory) and
                    hash_bytes(canonical(packet['arguments']))==record['arguments_sha256']==d['spawn_arguments_sha256'] and
                    admission_receipt(d['selection'],packet['arguments'],actual_native_task_id)==receipt,
                    'global_child_spawn_binding_mismatch')
            cas=self._verified_admitted(saved,route_id,state);expected=cas['expected_state']
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
                'receipt_sha256':hash_bytes(canonical(receipt)),'receipt_path':record['receipt_path'],
                'handoff':packet['handoff'],
                'spawn_plan_sha256':record['plan_sha256'],'arguments_sha256':record['arguments_sha256'],
                'result_sha256':record['result_sha256']}
            require(descriptor['created']<=time.time()<descriptor['expires'], 'global_handoff_expired')
            require(controller_active(c['heartbeat_at'],c['lease_expires'],state['controller_timing'],time.time()),
                    'global_controller_not_active')
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


def emit_cell(ledger,source,destination,package_root,join_code_file,operation,capacity,seconds,route_id=None,*,
              plan_file=None,actual_arguments_file=None,native_result_file=None,record_snapshot_file=None):
    """Emit complete reviewed one-shot source; never executes tools or wakes agents."""
    require(operation in ('join','heartbeat','claim-begin','claim-prepare-native','pre-native','post-spawn'),'invalid_global_cell_operation')
    if operation in ('claim-begin','claim-prepare-native','pre-native','post-spawn'):queue.token(route_id)
    if operation=='post-spawn':
        require(all(isinstance(p,(str,Path)) and str(p) for p in
                    (plan_file,actual_arguments_file,native_result_file,record_snapshot_file)),
                'global_post_spawn_evidence_paths_required')
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
    if operation in ('pre-native','claim-prepare-native') and plan_file is not None:
        require(isinstance(plan_file,(str,Path)) and str(plan_file),'global_native_plan_path_required')
        config['nativePlanFile']=str(Path(plan_file).absolute())
    capture={'cwd':str(package),'root':str(ledger.root),'nativeTaskId':ledger.identity}
    call=('joinAndFirstHeartbeat('+json.dumps({'capacity':capacity,'seconds':seconds})+')'
          if operation=='join' else 'claimAndBegin('+json.dumps({'routeId':route_id})+')'
          if operation=='claim-begin' else 'claimAndPrepareNative('+json.dumps({'routeId':route_id})+')'
          if operation=='claim-prepare-native' else 'prepareNative('+json.dumps({'routeId':route_id})+')'
          if operation=='pre-native' else 'completeAdmission('+json.dumps({
              'routeId':route_id,'planFile':str(Path(plan_file).absolute()),
              'actualArgumentsFile':str(Path(actual_arguments_file).absolute()),
              'nativeResultFile':str(Path(native_result_file).absolute()),
              'recordSnapshotFile':str(Path(record_snapshot_file).absolute())})+')'
          if operation=='post-spawn' else 'heartbeat()')
    metadata=({'yield_time_ms':120000,'max_output_tokens':4000} if operation in ('pre-native','claim-prepare-native')
              else {'yield_time_ms':1000,'max_output_tokens':300})
    script=('// @exec: '+json.dumps(metadata)+'\n'
            +adapter.read_text()+'\n'+(package/'native_connector/global_controller_cell.js').read_text()
            +'\nconst privateCapture=createNativeToolAdapter(tools,'+json.dumps(capture)+');\n'
            +'const activeController=createGlobalControllerToolAdapter(tools,'+json.dumps(config)+',privateCapture.captureValue);\n'
            +'text(await activeController.cell.'+call+');\n')
    path=ledger._new_path(destination);private_write(path,script.encode())
    return {'cell_file':str(path),'sha256':hash_bytes(script.encode()),'operation':operation,
            'executes_on_emission':False,'requires_active_native_agent':True}


def main():
    p=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    p.add_argument('operation',choices=['inspect','inspect-heartbeat','plan-join','plan-join-heartbeat','plan-heartbeat','plan-claim','plan-begin','plan-claim-begin','plan-claim-startup','plan-unknown',
                                       'plan-admitted','plan-heartbeat-admitted','emit-cell','verify','check-cas','plan-native','check-native','record-native',
                                       'import-child-admission'])
    for arg in ('document-id','tab-id','join-code-file','state-dir','native-task-id'):
        p.add_argument('--'+arg,required=True)
    p.add_argument('--snapshot')
    for arg in ('route-id','save','result-file','plan-file','response','readback','package-root','actual-arguments','native-result',
                'admission-receipt','child-native-task-id','record-snapshot-file'):
        p.add_argument('--'+arg)
    p.add_argument('--cell-operation',choices=['join','heartbeat','claim-begin','claim-prepare-native','pre-native','post-spawn'])
    p.add_argument('--capacity',type=int,default=2);p.add_argument('--seconds',type=int,default=3600)
    p.add_argument('--check-cas-now',action='store_true')
    p.add_argument('--check-native-now',action='store_true')
    p.add_argument('--import-child-admission',action='store_true')
    p.add_argument('--inline-evidence',action='store_true')
    p.add_argument('--result-first-chunk',action='store_true')
    p.add_argument('--result-large-chunk',action='store_true')
    a=p.parse_args();os.umask(0o077)
    event_plans=('plan-join','plan-join-heartbeat','plan-heartbeat','plan-claim','plan-begin',
                 'plan-claim-begin','plan-claim-startup','plan-unknown','plan-admitted','plan-heartbeat-admitted')
    require(not a.inline_evidence or (a.operation in event_plans+('inspect-heartbeat','verify','plan-native')
            and not a.snapshot and not a.readback and not a.response),'global_invalid_inline_evidence_operation')
    require(a.inline_evidence or a.snapshot,'global_snapshot_required')
    require(not a.check_cas_now or a.operation in ('plan-join','plan-join-heartbeat','plan-heartbeat','plan-claim','plan-begin',
            'plan-claim-begin','plan-claim-startup','plan-unknown','plan-admitted','plan-heartbeat-admitted'),'global_inline_cas_check_requires_event_plan')
    require(not a.check_native_now or a.operation=='plan-native','global_inline_native_check_requires_native_plan')
    require(not a.import_child_admission or (a.operation=='verify' and a.route_id
            and a.admission_receipt and a.child_native_task_id),'global_inline_import_requires_verified_admission')
    if a.import_child_admission:
        queue.token(a.route_id);queue.safe_id(a.child_native_task_id)
    require(not a.result_first_chunk or a.result_file,'global_first_chunk_requires_result_file')
    require(not a.result_large_chunk or a.result_first_chunk,'global_large_chunk_requires_first_chunk')
    def read(path):return strict_json(read_private_file(path,MAX))
    inline_files=None
    if a.inline_evidence:
        from .connector_files import capture
        raw=sys.stdin.buffer.read(60001)
        require(len(raw)<=60000,'global_inline_evidence_size_exceeded')
        envelope=strict_json(raw)
        require(isinstance(envelope,dict) and set(envelope)=={'snapshot','response'}
                and isinstance(envelope['snapshot'],dict)
                and (envelope['response'] is None or isinstance(envelope['response'],dict))
                and (a.operation=='verify' or envelope['response'] is None),'global_invalid_inline_evidence')
        root=private_dir(a.state_dir,create=True)
        capture(root,raw)  # Preserve the actual envelope before deriving inputs.
        a.snapshot=capture(root,canonical(envelope['snapshot'],max_bytes=MAX))['path']
        if a.operation=='verify':
            a.readback=a.snapshot
            if envelope['response'] is not None:
                a.response=capture(root,canonical(envelope['response'],max_bytes=MAX))['path']
        inline_files={'snapshot_file':a.snapshot,'response_file':a.response}
    code=read_private_file(a.join_code_file,256).decode('ascii').strip()
    source=queue.snapshot(read(a.snapshot),a.document_id,a.tab_id)
    ledger=NativeLedger(a.state_dir,source.state,code,a.native_task_id)
    if a.operation=='emit-cell':value=emit_cell(ledger,source,a.save,a.package_root,a.join_code_file,a.cell_operation,a.capacity,a.seconds,a.route_id,
        plan_file=a.plan_file,actual_arguments_file=a.actual_arguments,native_result_file=a.native_result,record_snapshot_file=a.record_snapshot_file or a.snapshot)
    elif a.operation in ('inspect','inspect-heartbeat'):
        value=ledger.inspect(source)
        if a.operation=='inspect-heartbeat':
            value={k:value[k] for k in ('controller','first_heartbeat_required','controller_timing')}
    elif a.operation=='check-cas':value=ledger.check_plan(source,a.plan_file)
    elif a.operation=='verify':
        readback=read(a.readback)
        if a.import_child_admission:
            candidate=read(a.plan_file);queue.validate_plan(candidate,code)
            event=candidate['expected_state']['events'][-1]
            require(event['kind']=='admitted' and event['arguments']['route_id']==a.route_id,
                    'global_inline_import_requires_verified_admission')
        value=ledger.verify_plan(a.plan_file,read(a.response) if a.response else None,readback)
        if a.import_child_admission:
            accepted=queue.snapshot(readback,a.document_id,a.tab_id)
            value['child_import']=ledger.import_child_admission(accepted,a.route_id,a.admission_receipt,a.child_native_task_id)
    elif a.operation=='plan-native':value=ledger.plan_spawn(source,a.route_id,a.save,a.package_root)
    elif a.operation=='check-native':value=ledger.check_spawn(source,a.plan_file)
    elif a.operation=='record-native':value=ledger.record_spawn(a.plan_file,read(a.actual_arguments),read(a.native_result),a.save)
    elif a.operation=='import-child-admission':value=ledger.import_child_admission(source,a.route_id,a.admission_receipt,a.child_native_task_id)
    else:value=ledger.plan_event(source,a.operation.removeprefix('plan-'),a.save,route_id=a.route_id,capacity=a.capacity,seconds=a.seconds)
    if a.check_cas_now and value.get('action')!='heartbeat_deferred_for_ready':
        # Run the same complete dispatch check after reservation, in this helper
        # invocation. Callers must still account for elapsed time before CAS.
        value['dispatch_check']=ledger.check_plan(source,value['plan_file'])
    if a.check_native_now:
        value['dispatch_check']=ledger.check_spawn(source,value['plan_file'])
    if inline_files is not None:value.update(inline_files)
    if a.result_file:
        destination=ledger._new_path(a.result_file);private_write(destination,canonical(value,max_bytes=MAX))
        if a.result_first_chunk:
            from .connector_files import packet_chunk
            print(json.dumps(packet_chunk(destination,max_chars=65536 if a.result_large_chunk else 16384,
                                          large_output=a.result_large_chunk),ensure_ascii=False))
        else:print(json.dumps({'result_file':str(destination)}))
    else:print(json.dumps(value,ensure_ascii=False))

if __name__=='__main__':
    try:main()
    except Exception as exc:
        error={'error':str(exc) if isinstance(exc,ProtocolError) else type(exc).__name__}
        diagnostic=getattr(exc,'controller_diagnostic',None)
        if diagnostic is not None:error['diagnostic']=diagnostic
        print(json.dumps(error));raise SystemExit(1)
