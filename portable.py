"""Versioned portable control plane. Data only; no model/task/notification API."""
import argparse,contextlib,fcntl,hashlib,json,math,os,re,stat,sys,time,uuid
from pathlib import Path
VENDOR=Path(__file__).resolve().parent/'vendor'
sys.path.insert(0,str(VENDOR))
from core import FileQueue,QueueError,protocol,read,digest
from file_queue import checked,encode
VERSION='file-ipc-portable/1'
ROLES=('desktop','broker')
STOP_CODES={'user_stop','auth_required','permission_denied','configuration_error','ambiguous_dispatch'}
EVENTS={'ready','claimed','inference_started','completed','failed','stopped','blocked','worker_expired','broker_crash','recovery_resolved'}
CODES={'none','user_stop','auth_required','permission_denied','configuration_error','ambiguous_dispatch','worker_expired','broker_crash','deadline','service_closed','request_limit','normal_exit'}

def label(v):
 if not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',v):raise QueueError('invalid_label')
 return v

def number(v,lo,hi):
 if type(v) not in (int,float) or not math.isfinite(v) or not lo<=v<=hi:raise QueueError('invalid_limit')
 return v

def private_dir(p):
 p=Path(p).absolute()
 if p.is_symlink():raise QueueError('symlink_directory')
 st=p.stat()
 if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode&0o077:raise QueueError('directory_not_private_owned')
 return p

def init(root,owner,seconds=600,max_requests=3,broker_starts=3,notification='outbox_only'):
 label(owner);number(seconds,5,900);number(max_requests,1,3);number(broker_starts,1,3)
 if type(max_requests) is not int or type(broker_starts) is not int:raise QueueError('invalid_limit')
 if notification not in ('outbox_only','parent_tool_attested'):raise QueueError('invalid_notification_mode')
 root=Path(root).absolute()
 if root.exists() or root.is_symlink():raise QueueError('deployment_already_exists')
 root.mkdir(mode=0o700,parents=True,exist_ok=False)
 for name in ('control','outbox','acks','observations','probe','runs','evidence'):(root/name).mkdir(mode=0o700)
 fd=os.open(root/'control/lock',os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600);os.close(fd)
 now=time.time();run=uuid.uuid4().hex
 manifest={'contract':VERSION,'deployment_id':uuid.uuid4().hex,'run_id':run,'owner_id':owner,'created':now,'expires':now+seconds,'scope':'text_only','notification':notification,'policy':{'max_requests':max_requests,'max_broker_starts':broker_starts,'max_desktop_starts':1,'request_seconds':180,'lease_seconds':60,'claim_wait_seconds':20},'paths':{'queue':'runs/'+run+'/queue','cli':'runs/'+run+'/isolated-cli','evidence':'evidence'}}
 protocol.atomic_json(root/'deployment.json',manifest,exclusive=True)
 protocol.atomic_json(root/'control/state.json',{'contract':VERSION,'deployment_id':manifest['deployment_id'],'run_id':run,'blocked':None,'roles':{},'dispatch':{}},exclusive=True)
 return manifest

class Deployment:
 def __init__(self,root):
  self.root=private_dir(root)
  for n in ('control','outbox','acks','observations','probe','runs','evidence'):private_dir(self.root/n)
  m=read(self.root/'deployment.json',8192)
  if not isinstance(m,dict) or m.get('contract')!=VERSION:raise QueueError('unsupported_contract')
  try:
   checked(m['deployment_id']);checked(m['run_id']);label(m['owner_id']);number(m['created'],0,1e12);number(m['expires']-m['created'],5,900)
   if m['scope']!='text_only' or m['notification'] not in ('outbox_only','parent_tool_attested'):raise ValueError()
   p=m['policy']
   if p!={'max_requests':p['max_requests'],'max_broker_starts':p['max_broker_starts'],'max_desktop_starts':1,'request_seconds':180,'lease_seconds':60,'claim_wait_seconds':20}:raise ValueError()
   if type(p['max_requests']) is not int or not 1<=p['max_requests']<=3 or type(p['max_broker_starts']) is not int or not 1<=p['max_broker_starts']<=3:raise ValueError()
   if m['paths']!={'queue':'runs/'+m['run_id']+'/queue','cli':'runs/'+m['run_id']+'/isolated-cli','evidence':'evidence'}:raise ValueError()
  except (KeyError,TypeError,ValueError):raise QueueError('invalid_deployment') from None
  self.m=m
 @property
 def queue_root(self):return self.root/self.m['paths']['queue']
 def scope(self,owner):
  if owner!=self.m['owner_id']:raise QueueError('owner_mismatch')
 @contextlib.contextmanager
 def locked(self):
  fd=os.open(self.root/'control/lock',os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK)
  try:
   if not stat.S_ISREG(os.fstat(fd).st_mode):raise QueueError('invalid_lock')
   end=time.monotonic()+2
   while True:
    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
    except BlockingIOError:
     if time.monotonic()>=end:raise QueueError('control_lock_timeout')
     time.sleep(.005)
   s=read(self.root/'control/state.json',65536)
   if s.get('contract')!=VERSION or s.get('deployment_id')!=self.m['deployment_id'] or s.get('run_id')!=self.m['run_id']:raise QueueError('state_scope_mismatch')
   yield s
  finally:os.close(fd)
 def save(self,s):
  if len(encode(s))>65536:raise QueueError('state_too_large')
  protocol.atomic_json(self.root/'control/state.json',s)
 def live(self,s):
  if s['blocked']:raise QueueError('deployment_blocked')
  if time.time()>=self.m['expires']:raise QueueError('deployment_expired')
  if (self.queue_root/'closed.json').exists() or (self.queue_root/'stop.requested').exists():raise QueueError('service_closed')
 def current(self,s,a,allow_expired=False):
  if not isinstance(a,dict):raise QueueError('invalid_assignment')
  role=a.get('role');current=s['roles'].get(role)
  if current is None or any(a.get(k)!=current.get(k) for k in ('contract','deployment_id','run_id','owner_id','role','worker_id','assignment_id','epoch')):raise QueueError('stale_assignment')
  if current['status']!='active' or (not allow_expired and current['expires']<=time.time()):raise QueueError('assignment_expired')
  return current
 def queue(self):return FileQueue(self.queue_root)
 def jobs(self):
  if not self.queue_root.exists():return []
  q=self.queue()
  try:
   with q.locked():return q.states()
  finally:q.close()
 def assign(self,owner,role,worker,lease=180,native_attested=False):
  self.scope(owner);label(worker);number(lease,.05,240)
  if role not in ROLES:raise QueueError('invalid_role')
  if role=='broker' and native_attested is not True:raise QueueError('native_capability_not_attested')
  with self.locked() as s:
   self.live(s);old=s['roles'].get(role)
   if old and old['status']=='active' and old['expires']>time.time():raise QueueError('role_already_assigned')
   epoch=1 if old is None else old['epoch']+1
   if epoch>self.m['policy']['max_'+role+'_starts']:raise QueueError('restart_budget_exhausted')
   if role=='broker':
    for j in self.jobs():
     d=s['dispatch'].get(j['id'])
     if d and d['status']=='started' and not (j['state']=='completed' and j.get('completion_sha256')):raise QueueError('ambiguous_dispatch')
     if j['state']=='running':raise QueueError('prior_job_lease_active')
   now=time.time();a={'contract':VERSION,'deployment_id':self.m['deployment_id'],'run_id':self.m['run_id'],'owner_id':owner,'role':role,'worker_id':worker,'assignment_id':uuid.uuid4().hex,'epoch':epoch,'issued':now,'expires':min(now+lease,self.m['expires']),'status':'active','native_capability':'operator_attested' if native_attested else 'not_required'}
   s['roles'][role]=a;self.save(s);return dict(a)
 def heartbeat(self,a,state='ready',seconds=180):
  number(seconds,.05,240)
  if state not in ('ready','busy','closed'):raise QueueError('invalid_heartbeat')
  with self.locked() as s:
   
   if state!='closed':self.live(s)
   cur=self.current(s,a,allow_expired=state=='closed');cur['expires']=min(time.time()+seconds,self.m['expires'])
   if state=='closed':cur['status']='closed'
   self.save(s)
   h={k:cur[k] for k in ('contract','deployment_id','run_id','owner_id','role','worker_id','assignment_id','epoch','expires')};h.update(state=state,at=time.time())
   protocol.atomic_json(self.root/'control'/('heartbeat-'+cur['role']+'.json'),h)
   return dict(cur)
 def _valid_receipt(self,r,rid):
  if not isinstance(r,dict) or any(r.get(k)!=self.m[k] for k in ('contract','deployment_id','run_id','owner_id')) or r.get('receipt_id')!=rid:raise QueueError('receipt_scope_or_hash_mismatch')
  body={k:v for k,v in r.items() if k not in ('created','content_sha256','notification')}
  if digest(body)!=r.get('content_sha256'):raise QueueError('receipt_scope_or_hash_mismatch')
  return r
 def _acknowledged(self,rid,sha):
  path=self.root/'acks'/(rid+'.json')
  if not path.exists():return False
  a=read(path,8192)
  if not isinstance(a,dict) or any(a.get(k)!=self.m[k] for k in ('contract','deployment_id','run_id','owner_id')) or a.get('receipt_id')!=rid or a.get('content_sha256')!=sha or a.get('via') not in ('manual_parent','parent_tool') or not isinstance(a.get('evidence'),str) or not re.fullmatch(r'[A-Za-z0-9_:/.-]{1,256}',a['evidence']):raise QueueError('invalid_acknowledgment')
  if a['via']=='parent_tool' and self.m['notification']!='parent_tool_attested':raise QueueError('invalid_acknowledgment')
  return True
 def _receipt(self,s,a,event,code='none',job=None,receipt_id=None):
  if event not in EVENTS or code not in CODES:raise QueueError('invalid_receipt_event')
  cur=self.current(s,a,allow_expired=True);rid=checked(receipt_id or uuid.uuid4().hex)
  if job is not None:
   if not isinstance(job,dict) or set(job)!={'id','request_sha256','lease_epoch','state','completion_sha256'}:raise QueueError('invalid_receipt_job')
   checked(job['id'])
   if not re.fullmatch('[0-9a-f]{64}',job['request_sha256']) or job['lease_epoch'] not in (1,2,3):raise QueueError('invalid_receipt_job')
  body={k:cur[k] for k in ('contract','deployment_id','run_id','owner_id','role','worker_id','assignment_id','epoch')};body.update(receipt_id=rid,event=event,code=code,job=job)
  path=self.root/'outbox'/(rid+'.json')
  if code in STOP_CODES and code!='ambiguous_dispatch':
   if s['blocked'] is None:s['blocked']=code
   self.save(s)
  if path.exists():
   old=self._valid_receipt(read(path,8192),rid)
   if old.get('content_sha256')!=digest(body):raise QueueError('receipt_id_conflict')
   return {'receipt_id':rid,'idempotent':True,'notification':'acknowledged_parent' if self._acknowledged(rid,old['content_sha256']) else 'written_outbox'}
  if len(list((self.root/'outbox').glob('*.json')))>=128:raise QueueError('receipt_limit')
  record={**body,'created':time.time(),'content_sha256':digest(body),'notification':'written_outbox'}
  # Stop state is already durable even if capacity/publication fails.
  protocol.atomic_json(path,record,exclusive=True)
  return {'receipt_id':rid,'idempotent':False,'notification':'written_outbox'}
 def receipt(self,a,event,code='none',receipt_id=None):
  with self.locked() as s:return self._receipt(s,a,event,code,receipt_id=receipt_id)
 def ack(self,owner,rid,content_sha,evidence,via='manual_parent'):
  self.scope(owner);checked(rid)
  if not isinstance(evidence,str) or not re.fullmatch(r'[A-Za-z0-9_:/.-]{1,256}',evidence):raise QueueError('invalid_ack_evidence')
  if via not in ('manual_parent','parent_tool') or (via=='parent_tool' and self.m['notification']!='parent_tool_attested'):raise QueueError('notification_capability_missing')
  with self.locked():
   r=read(self.root/'outbox'/(rid+'.json'),8192)
   if any(r.get(k)!=self.m[k] for k in ('contract','deployment_id','run_id','owner_id')) or r.get('receipt_id')!=rid or r.get('content_sha256')!=content_sha:raise QueueError('receipt_scope_or_hash_mismatch')
   body_check={k:v for k,v in r.items() if k not in ('created','content_sha256','notification')}
   if digest(body_check)!=content_sha:raise QueueError('receipt_scope_or_hash_mismatch')
   path=self.root/'acks'/(rid+'.json');body={'contract':VERSION,'deployment_id':self.m['deployment_id'],'run_id':self.m['run_id'],'owner_id':owner,'receipt_id':rid,'content_sha256':content_sha,'via':via,'evidence':evidence,'meaning':'parent acknowledgment recorded; this helper sent no notification'}
   if path.exists():
    if read(path,8192)!=body:raise QueueError('ack_conflict')
    return {'idempotent':True,**body}
   protocol.atomic_json(path,body,exclusive=True);return {'idempotent':False,**body}
 def observe_receipt(self,owner,rid,observer):
  self.scope(owner);checked(rid);label(observer)
  with self.locked():
   r=read(self.root/'outbox'/(rid+'.json'),8192)
   if any(r.get(k)!=self.m[k] for k in ('contract','deployment_id','run_id','owner_id')) or r.get('receipt_id')!=rid:raise QueueError('receipt_scope_or_hash_mismatch')
   if digest({k:v for k,v in r.items() if k not in ('created','content_sha256','notification')})!=r.get('content_sha256'):raise QueueError('receipt_scope_or_hash_mismatch')
   body={'contract':VERSION,'deployment_id':self.m['deployment_id'],'run_id':self.m['run_id'],'owner_id':owner,'receipt_id':rid,'content_sha256':r['content_sha256'],'observer':observer,'state':'read_observer','at':time.time()}
   protocol.atomic_json(self.root/'observations'/(rid+'.json'),body)
   return {'receipt':r,'observation':body,'state':'acknowledged_parent' if self._acknowledged(rid,r['content_sha256']) else 'read_observer'}
 def resolve(self,owner,jid,outcome,evidence):
  self.scope(owner);checked(jid);label(evidence)
  if outcome not in ('confirmed_not_started','confirmed_stopped'):raise QueueError('invalid_resolution')
  with self.locked() as s:
   self.live(s);d=s['dispatch'].get(jid)
   if not d or d['status']!='started':raise QueueError('no_ambiguous_dispatch')
   a=s['roles'].get('broker')
   if a and a['status']=='active' and a['expires']>time.time():raise QueueError('worker_still_live')
   d.update(status=outcome,evidence=evidence,resolved_at=time.time());self.save(s);return dict(d)
 def recovery(self,owner):
  self.scope(owner)
  with self.locked() as s:
   reason=None
   try:self.live(s)
   except QueueError as e:reason=e.code
   old=s['roles'].get('broker');jobs=self.jobs()
   if not reason and old and old['status']=='active' and old['expires']>time.time():reason='worker_still_live'
   if not reason and old and old['epoch']>=self.m['policy']['max_broker_starts']:reason='restart_budget_exhausted'
   if not reason and any(s['dispatch'].get(j['id'],{}).get('status')=='started' and not (j['state']=='completed' and j.get('completion_sha256')) for j in jobs):reason='ambiguous_dispatch'
   if not reason and any(j['state']=='running' for j in jobs):reason='prior_job_lease_active'
   return {'contract':VERSION,'deployment_id':self.m['deployment_id'],'owner_id':owner,'action':'await_operator' if reason else 'assign_replacement_broker','reason':reason,'executed':False,'notification_delivered':False,'desktop_restart':'new deployment directory only; no replay of prior jobs'}

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True);s=p.add_subparsers(dest='cmd',required=True)
 i=s.add_parser('init');i.add_argument('--owner',required=True);i.add_argument('--seconds',type=float,default=600);i.add_argument('--max-requests',type=int,default=3);i.add_argument('--notification',default='outbox_only',choices=['outbox_only','parent_tool_attested'])
 a=s.add_parser('assign');a.add_argument('--owner',required=True);a.add_argument('--role',required=True,choices=ROLES);a.add_argument('--worker',required=True);a.add_argument('--native-capability-attested',action='store_true');a.add_argument('--save',required=True)
 a=s.add_parser('recovery');a.add_argument('--owner',required=True)
 a=s.add_parser('observe-receipt');a.add_argument('--owner',required=True);a.add_argument('--receipt',required=True);a.add_argument('--observer',required=True)
 a=s.add_parser('resolve');a.add_argument('--owner',required=True);a.add_argument('--job',required=True);a.add_argument('--outcome',required=True,choices=['confirmed_not_started','confirmed_stopped']);a.add_argument('--evidence',required=True)
 a=s.add_parser('ack');a.add_argument('--owner',required=True);a.add_argument('--receipt',required=True);a.add_argument('--sha256',required=True);a.add_argument('--evidence',required=True);a.add_argument('--via',default='manual_parent')
 args=p.parse_args();os.umask(0o077)
 if args.cmd=='init':out=init(args.root,args.owner,args.seconds,args.max_requests,notification=args.notification)
 else:
  d=Deployment(args.root)
  if args.cmd=='assign':out=d.assign(args.owner,args.role,args.worker,native_attested=args.native_capability_attested);protocol.atomic_json(Path(args.save),out,exclusive=True)
  elif args.cmd=='recovery':out=d.recovery(args.owner)
  elif args.cmd=='observe-receipt':out=d.observe_receipt(args.owner,args.receipt,args.observer)
  elif args.cmd=='resolve':out=d.resolve(args.owner,args.job,args.outcome,args.evidence)
  else:out=d.ack(args.owner,args.receipt,args.sha256,args.evidence,args.via)
 print(encode(out).decode())
if __name__=='__main__':
 try:main()
 except (QueueError,OSError,ValueError,KeyError,TypeError) as e:print(json.dumps({'error':getattr(e,'code',type(e).__name__)}),file=sys.stderr);raise SystemExit(1)
