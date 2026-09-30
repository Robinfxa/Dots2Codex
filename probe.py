"""Data-only per-environment observations and a shared-file nonce handshake."""
import argparse,json,os,platform,sys,time,uuid
from pathlib import Path
from portable import Deployment,QueueError,protocol,read,digest,encode,ROLES

def scoped(d,v):
 if not isinstance(v,dict) or any(v.get(k)!=d.m[k] for k in ('contract','deployment_id','run_id')):raise QueueError('probe_scope_mismatch')
 return v

def observe(d,role):
 if role not in ROLES:raise QueueError('invalid_role')
 ns={}
 for name in ('pid','net','mnt'):
  try:ns[name]=os.readlink('/proc/self/ns/'+name)
  except OSError:pass
 value={'contract':d.m['contract'],'deployment_id':d.m['deployment_id'],'run_id':d.m['run_id'],'probe_id':uuid.uuid4().hex,'role':role,'at':time.time(),'platform':platform.system(),'python':platform.python_version(),'pid':os.getpid(),'observed_uid':os.getuid(),'namespaces':ns,'shared_directory_private_owned':True,'capabilities':{'posix_file_api':os.name=='posix','loopback_connectivity':'not_tested','native_inference':'not_detectable_by_python','parent_notifications':'not_detectable_by_python'}}
 with d.locked() as s:
  d.live(s);protocol.atomic_json(d.root/'probe'/(role+'.json'),value,exclusive=True)
 return value

def offer(d):
 with d.locked() as s:
  d.live(s);p=scoped(d,read(d.root/'probe/desktop.json',8192))
  if p['deployment_id']!=d.m['deployment_id'] or p['role']!='desktop':raise QueueError('probe_scope_mismatch')
  now=time.time();c={'contract':d.m['contract'],'deployment_id':d.m['deployment_id'],'run_id':d.m['run_id'],'desktop_probe_id':p['probe_id'],'nonce':uuid.uuid4().hex,'created':now,'deadline':min(now+120,d.m['expires'])}
  protocol.atomic_json(d.root/'probe/challenge.json',c,exclusive=True);return c

def answer(d):
 with d.locked() as s:
  d.live(s);c=scoped(d,read(d.root/'probe/challenge.json',8192));p=scoped(d,read(d.root/'probe/broker.json',8192))
  if c['deployment_id']!=d.m['deployment_id'] or c['run_id']!=d.m['run_id'] or p['deployment_id']!=d.m['deployment_id'] or p['role']!='broker' or time.time()>c['deadline']:raise QueueError('handshake_stale_or_mismatch')
  a={'contract':d.m['contract'],'deployment_id':d.m['deployment_id'],'run_id':d.m['run_id'],'broker_probe_id':p['probe_id'],'challenge_sha256':digest(c),'transformed_nonce':c['nonce'].upper(),'at':time.time()}
  protocol.atomic_json(d.root/'probe/answer.json',a,exclusive=True);return a

def verify(d):
 with d.locked() as s:
  d.live(s);c=scoped(d,read(d.root/'probe/challenge.json',8192));a=scoped(d,read(d.root/'probe/answer.json',8192));p=scoped(d,read(d.root/'probe/desktop.json',8192));b=scoped(d,read(d.root/'probe/broker.json',8192))
  if any(x.get('deployment_id')!=d.m['deployment_id'] for x in (c,a,p,b)) or a['challenge_sha256']!=digest(c) or a['transformed_nonce']!=c['nonce'].upper() or p['probe_id']!=c['desktop_probe_id'] or b['probe_id']!=a['broker_probe_id'] or not c['created']<=a['at']<=c['deadline'] or time.time()>c['deadline']:raise QueueError('handshake_stale_or_mismatch')
  r={'contract':d.m['contract'],'deployment_id':d.m['deployment_id'],'run_id':d.m['run_id'],'verified_at':time.time(),'challenge_sha256':digest(c),'shared_file_roundtrip':True,'distinct_namespaces_observed':bool(p['namespaces'] and b['namespaces'] and p['namespaces']!=b['namespaces']),'network_reachability_verified':False,'native_inference_verified':False}
  protocol.atomic_json(d.root/'probe/verified.json',r,exclusive=True);return r

def require_verified(d):
 r=scoped(d,read(d.root/'probe/verified.json',8192))
 if r.get('deployment_id')!=d.m['deployment_id'] or r.get('run_id')!=d.m['run_id'] or r.get('shared_file_roundtrip') is not True:raise QueueError('handshake_required')
 return r

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True);p.add_argument('operation',choices=['observe','offer','answer','verify']);p.add_argument('--role',choices=ROLES);a=p.parse_args();os.umask(0o077);d=Deployment(a.root)
 out=observe(d,a.role) if a.operation=='observe' else {'offer':offer,'answer':answer,'verify':verify}[a.operation](d)
 print(encode(out).decode())
if __name__=='__main__':
 try:main()
 except (QueueError,OSError,ValueError,KeyError,TypeError) as e:print(json.dumps({'error':getattr(e,'code',type(e).__name__)}),file=sys.stderr);raise SystemExit(1)
