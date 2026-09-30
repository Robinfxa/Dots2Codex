"""Bounded native-broker data API. Does not invoke inference or execute payloads."""
import argparse,json,os,time,uuid
from pathlib import Path
from portable import Deployment,QueueError,protocol,read,digest,encode,number
from probe import require_verified
from readiness import broker_tick

def _job_scope(d,t):
 if not isinstance(t,dict) or t.get('contract')!=d.m['contract'] or t.get('deployment_id')!=d.m['deployment_id'] or t.get('run_id')!=d.m['run_id']:raise QueueError('ticket_deployment_mismatch')
 q=d.queue()
 if q.meta['owner']!=d.m['owner_id'] or q.meta['session']!='session_'+d.m['run_id']:q.close();raise QueueError('queue_scope_mismatch')
 return q

def claim(d,a,wait=20,lease=60):
 number(wait,0,20);number(lease,.05,180);require_verified(d);end=time.monotonic()+wait
 while True:
  with d.locked() as s:
   d.live(s);cur=d.current(s,a)
   if cur['role']!='broker':raise QueueError('broker_role_required')
   cur['expires']=min(time.time()+180,d.m['expires']);d.save(s)
   if (d.queue_root/'ready.json').exists():
    ready=read(d.queue_root/'ready.json',8192)
    if time.time()>=ready['deadline']:raise QueueError('service_expired')
    broker_tick(d.queue_root,ready['instance'],'ready')
    q=d.queue()
    try:
     
     with q.locked():
      for old in q.states():
       if s['dispatch'].get(old['id'],{}).get('status')=='started' and not (old['state']=='completed' and old.get('completion_sha256')):raise QueueError('ambiguous_dispatch')
     job=q.claim(owner=d.m['owner_id'],session='session_'+d.m['run_id'],wait=0,lease_seconds=min(lease,max(.05,ready['deadline']-time.time())))
    finally:q.close()
    if job:
     prior=s['dispatch'].get(job['id'])
     # Normally prevented during assignment. Guard same-assignment lease expiry too.
     if prior and prior['status']=='started':raise QueueError('ambiguous_dispatch')
     s['dispatch'][job['id']]={'assignment_id':cur['assignment_id'],'epoch':cur['epoch'],'lease_epoch':job['lease_epoch'],'request_sha256':job['request_sha256'],'status':'claimed','at':time.time()};d.save(s)
     broker_tick(d.queue_root,ready['instance'],'busy')
     # Ticket deliberately has no inference input; begin() is the first input API.
     job.pop('request',None)
     return {'contract':d.m['contract'],'deployment_id':d.m['deployment_id'],'run_id':d.m['run_id'],'assignment_id':cur['assignment_id'],'worker_epoch':cur['epoch'],'job':job}
  if time.monotonic()>=end:return None
  time.sleep(min(.05,max(0,end-time.monotonic())))

def _validate(d,s,a,t):
 cur=d.current(s,a)
 if cur['role']!='broker' or t.get('assignment_id')!=cur['assignment_id'] or t.get('worker_epoch')!=cur['epoch']:raise QueueError('ticket_assignment_mismatch')
 j=t.get('job')
 if not isinstance(j,dict):raise QueueError('invalid_ticket')
 ds=s['dispatch'].get(j.get('id'))
 if not ds or ds['assignment_id']!=cur['assignment_id'] or ds['lease_epoch']!=j.get('lease_epoch') or ds['request_sha256']!=j.get('request_sha256'):raise QueueError('dispatch_scope_mismatch')
 return cur,j,ds

def begin(d,a,t):
 with d.locked() as s:
  d.live(s);cur,j,ds=_validate(d,s,a,t);q=_job_scope(d,t)
  try:
   with q.locked():
    stored=q.load(j['id']);q._lease(stored,j['lease'],j['lease_epoch'],j['request_sha256'])
    if digest(stored['request'])!=j['request_sha256']:raise QueueError('request_hash_mismatch')
   if ds['status']!='claimed':raise QueueError('inference_already_started_or_resolved')
   # Written BEFORE returning request. Any uncertain interruption requires explicit resolution.
   ds.update(status='started',started_at=time.time());d.save(s)
   return {'contract':d.m['contract'],'deployment_id':d.m['deployment_id'],'job_id':j['id'],'request_sha256':j['request_sha256'],'deadline':j['deadline'],'lease_until':j['lease_until'],'scope':'text_only','request':stored['request']}
  finally:q.close()

def renew(d,a,t,seconds=60):
 with d.locked() as s:
  d.live(s);cur,j,ds=_validate(d,s,a,t);q=_job_scope(d,t)
  try:new=q.renew(j['id'],j['lease'],j['lease_epoch'],j['request_sha256'],owner=d.m['owner_id'],session='session_'+d.m['run_id'],seconds=seconds)
  finally:q.close()
  cur['expires']=min(time.time()+180,d.m['expires']);d.save(s)
  return {**t,'job':{**j,**new}}

def complete(d,a,t,result):
 if not isinstance(result,dict) or set(result)!={'kind','text'} or result['kind']!='message' or not isinstance(result['text'],str):raise QueueError('text_only_result_required')
 with d.locked() as s:
  d.live(s);cur,j,ds=_validate(d,s,a,t)
  if ds['status'] not in ('started','completed'):raise QueueError('inference_not_started')
  q=_job_scope(d,t)
  try:
   out=q.complete(j['id'],j['lease'],j['lease_epoch'],j['request_sha256'],owner=d.m['owner_id'],session='session_'+d.m['run_id'],result=result)
   stored=q.get(j['id'],owner=d.m['owner_id'],session='session_'+d.m['run_id'])
  finally:q.close()
  ds['status']='completed';d.save(s)
  receipt_job={k:stored.get(k) for k in ('id','request_sha256','lease_epoch','state','completion_sha256')}
  rid=uuid.uuid5(uuid.NAMESPACE_URL,d.m['deployment_id']+cur['assignment_id']+j['id']+str(j['lease_epoch'])).hex
  receipt=d._receipt(s,a,'completed',job=receipt_job,receipt_id=rid)
  return {**out,'delivery':stored['delivery'],'receipt':receipt}

def status(d,a,t):
 with d.locked() as s:
  _validate(d,s,a,t);q=_job_scope(d,t)
  try:r=q.get(t['job']['id'],owner=d.m['owner_id'],session='session_'+d.m['run_id'])
  finally:q.close()
  return {k:r.get(k) for k in ('id','request_sha256','state','delivery','lease_epoch','completion_sha256')}

def stop(d,a,code='normal_exit'):
 result=d.receipt(a,'stopped',code)
 try:
  if (d.queue_root/'ready.json').exists():
   r=read(d.queue_root/'ready.json',8192);broker_tick(d.queue_root,r['instance'],'closed')
 finally:d.heartbeat(a,'closed')
 return result

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True);p.add_argument('--assignment',required=True);p.add_argument('operation',choices=['claim','read','renew','complete','status','stop']);p.add_argument('--ticket');p.add_argument('--save');p.add_argument('--result');p.add_argument('--wait',type=float,default=20);p.add_argument('--lease',type=float,default=60);p.add_argument('--code',default='normal_exit');args=p.parse_args();os.umask(0o077);d=Deployment(args.root);a=read(Path(args.assignment),8192)
 if args.operation=='claim':
  if not args.save:raise QueueError('save_path_required')
  out=claim(d,a,args.wait,args.lease)
  if out is not None:
   protocol.atomic_json(Path(args.save),out,exclusive=True)
   out={'job_id':out['job']['id'],'ticket_saved':True,'read_required':True}
 elif args.operation=='stop':out=stop(d,a,args.code)
 else:
  if not args.ticket:raise QueueError('ticket_required')
  t=read(Path(args.ticket),1500000)
  if args.operation=='read':out=begin(d,a,t)
  elif args.operation=='renew':
   if not args.save:raise QueueError('save_path_required')
   out=renew(d,a,t,args.lease);protocol.atomic_json(Path(args.save),out,exclusive=True);out={'renewed':True,'lease_until':out['job']['lease_until']}
  elif args.operation=='complete':out=complete(d,a,t,read(Path(args.result),131072))
  else:out=status(d,a,t)
 print(encode(out).decode());return 0 if out is not None else 2
if __name__=='__main__':
 try:raise SystemExit(main())
 except (QueueError,OSError,ValueError,KeyError,TypeError) as e:print(json.dumps({'error':getattr(e,'code',type(e).__name__)}),file=sys.stderr);raise SystemExit(1)
