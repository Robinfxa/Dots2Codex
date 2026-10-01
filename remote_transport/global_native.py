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

MAX=8*1024*1024


class NativeLedger:
    def __init__(self,root,initial_state,join_code,actual_native_task_id):
        queue.verify(initial_state,join_code)
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
            else:saved={'contract':'dots-global-native-ledger/1','root':self.context,'actual_native_task_id':self.identity,
                        'event_hashes':[],'operations':{},'spawns':{}};self.save(saved)
            require(saved.get('contract')=='dots-global-native-ledger/1' and saved.get('root')==self.context
                    and saved.get('actual_native_task_id')==self.identity,'global_ledger_identity_mismatch')
            yield saved
        finally:os.close(fd)
    def save(self,saved):private_write(self.path,canonical(saved,max_bytes=MAX))
    def observe(self,saved,source):
        state=queue.verify(source.state,self.code,expected_root=self.context)
        hashes=[hash_bytes(canonical(e)) for e in state['events']]
        require(hashes[:len(saved['event_hashes'])]==saved['event_hashes'],'global_observation_rollback_or_fork')
        saved['event_hashes']=hashes;self.save(saved)
        c=state['logical']['controller']
        require(c is None or c['native_task_id']==self.identity,'global_actual_controller_identity_mismatch')
        return state
    def _new_path(self,path):
        path=Path(path).absolute();private_dir(path.parent)
        require(not path.exists() and not path.is_symlink(),'new_private_global_plan_path_required');return path
    def _issue(self,saved,source,kind,args,destination,now=None):
        state=self.observe(saved,source);now=int(time.time()) if now is None else now
        semantic=kind+':'+str(args.get('route_id',now if kind=='heartbeat' else 'controller'))
        require(semantic not in saved['operations'],'global_operation_already_issued_no_replay')
        destination=self._new_path(destination)
        new=queue.transition(state,self.code,kind,'native',args,now=now)
        packet=queue.plan(source,new,self.code);digest=hash_bytes(canonical(packet))
        saved['operations'][semantic]={'status':'issued_outcome_unknown','path':str(destination),'sha256':digest,
                                      'operation_id':packet['operation_id'],'kind':kind,'route_id':args.get('route_id')}
        self.save(saved);private_write(destination,canonical(packet))
        return {'plan_file':str(destination),'operation_id':packet['operation_id'],'tool_arguments':packet['tool_arguments'],
                'one_attempt_only':True,'retry_on_unknown':False,'native_invoked_by_python':False}
    def inspect(self,source):
        with self.locked() as saved:
            state=self.observe(saved,source);c=state['logical']['controller']
            return {'contract':queue.CONTRACT,'activation_id':state['activation_id'],'expires':state['expires'],
                    'controller':c,'closed':state['logical']['closed'],'pending':[
                        {'route_id':d['route_id'],'selection':d['selection'],'expires':d['expires']}
                        for d in state['logical']['demands'].values() if d['state']=='pending'],
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
    def verify_plan(self,plan_path,response,readback):
        packet=strict_json(read_private_file(plan_path,MAX));path=str(Path(plan_path).absolute())
        with self.locked() as saved:
            records=[r for r in saved['operations'].values() if r['path']==path]
            require(len(records)==1 and records[0]['sha256']==hash_bytes(canonical(packet)),'global_plan_not_reserved')
            record=records[0]
            try:
                fresh=queue.verify_update(packet,response,readback,self.code)
                self.observe(saved,fresh)
            except Exception:
                record['status']='outcome_unknown_no_replay';self.save(saved);raise
            record.update(status='verified',readback_hash=hash_bytes(canonical(readback)),revision=fresh.revision_id)
            self.save(saved)
            return {'verified':True,'epoch':fresh.state['epoch'],'operation_id':packet['operation_id'],
                    'reconciled_from_event':response is None,'native_retry_allowed':False}
    def plan_spawn(self,source,route_id,destination,package_root):
        with self.locked() as saved:
            state=self.observe(saved,source)
            begin=saved['operations'].get('begin:'+route_id)
            require(begin and begin['status']=='verified','verified_global_begin_required')
            require(route_id not in saved['spawns'],'global_native_attempt_already_reserved_no_replay')
            d=state['logical']['demands'].get(route_id);require(d and d['state']=='spawn_intent','global_spawn_intent_required')
            packet0=strict_json(read_private_file(begin['path'],MAX))
            require(d['dispatch_id']==packet0['expected_state']['logical']['demands'][route_id]['dispatch_id'],
                    'global_native_dispatch_mismatch')
            args=queue.native_arguments(state,self.code,route_id,package_root);destination=self._new_path(destination)
            packet={'contract':'dots-global-native-plan/1','queue_root_hash':self.key,'route_id':route_id,
                    'dispatch_id':d['dispatch_id'],'selection':d['selection'],'tool':'collaboration.spawn_agent','arguments':args}
            saved['spawns'][route_id]={'status':'reserved_outcome_unknown','plan_path':str(destination),
                                       'plan_sha256':hash_bytes(canonical(packet)),'receipt':None}
            self.save(saved);private_write(destination,canonical(packet))
            return {'plan_file':str(destination),'tool':'collaboration.spawn_agent','arguments':args,
                    'one_attempt_only':True,'native_invoked_by_python':False,
                    'next':'Active trusted Router calls the real native tool once. Record exact arguments and returned task_name. Never retry unknown admission.'}
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
                    'underlying_model_verified':False,'next':'Publish the admitted queue event, then send this receipt path to the same child for its existing v3 JOIN. No new spawn.'}


def main():
    p=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    p.add_argument('operation',choices=['inspect','plan-join','plan-heartbeat','plan-claim','plan-begin','plan-unknown',
                                       'plan-admitted','verify','plan-native','record-native'])
    for arg in ('snapshot','document-id','tab-id','join-code-file','state-dir','native-task-id'):
        p.add_argument('--'+arg,required=True)
    for arg in ('route-id','save','plan-file','response','readback','package-root','actual-arguments','native-result'):
        p.add_argument('--'+arg)
    p.add_argument('--capacity',type=int,default=2);p.add_argument('--seconds',type=int,default=3600)
    a=p.parse_args();os.umask(0o077)
    def read(path):return strict_json(read_private_file(path,MAX))
    code=read_private_file(a.join_code_file,256).decode('ascii').strip()
    source=queue.snapshot(read(a.snapshot),a.document_id,a.tab_id)
    ledger=NativeLedger(a.state_dir,source.state,code,a.native_task_id)
    if a.operation=='inspect':value=ledger.inspect(source)
    elif a.operation=='verify':value=ledger.verify_plan(a.plan_file,read(a.response) if a.response else None,read(a.readback))
    elif a.operation=='plan-native':value=ledger.plan_spawn(source,a.route_id,a.save,a.package_root)
    elif a.operation=='record-native':value=ledger.record_spawn(a.plan_file,read(a.actual_arguments),read(a.native_result),a.save)
    else:value=ledger.plan_event(source,a.operation.removeprefix('plan-'),a.save,route_id=a.route_id,capacity=a.capacity,seconds=a.seconds)
    print(json.dumps(value,ensure_ascii=False))

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'error':str(exc) if isinstance(exc,ProtocolError) else type(exc).__name__}));raise SystemExit(1)
