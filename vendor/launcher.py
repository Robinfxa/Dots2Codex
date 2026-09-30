#!/usr/bin/env python3
"""Default: stage an isolated Codex profile after live bridge/broker readiness.
No original configuration, credentials or PATH aliases are read or modified.
"""
import argparse,hashlib,http.client,json,math,os,re,secrets,shlex,stat,subprocess,sys,time
from pathlib import Path
from core import QueueError,protocol,read
MAPPING='codex-0.159.2-hyphen-pair-v1'
MAX_EXECUTABLE_BYTES=512*1024*1024
URL=re.compile(r'^http://127\.0\.0\.1:([1-9][0-9]{0,4})/v1$')

def endpoint(url):
 if not isinstance(url,str) or not (m:=URL.fullmatch(url)) or int(m[1])>65535:raise QueueError('exact_loopback_url_required')
 return int(m[1])
def number(x):return type(x) in (int,float) and math.isfinite(x)
def private_directory(path):
 st=Path(path).lstat()
 if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode&0o077:raise QueueError('private_owned_directory_required')
def bounded_bytes(path,limit):
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 with os.fdopen(fd,'rb') as f:
  if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):raise QueueError('regular_file_required')
  data=f.read(limit+1)
 if len(data)>limit:raise QueueError('file_too_large')
 return data
def binary_fingerprint(path):
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK);h=hashlib.sha256();size=0
 with os.fdopen(fd,'rb') as f:
  st=os.fstat(f.fileno())
  if not stat.S_ISREG(st.st_mode) or st.st_size>MAX_EXECUTABLE_BYTES:raise QueueError('invalid_executable_file')
  while True:
   chunk=f.read(1024*1024)
   if not chunk:break
   size+=len(chunk)
   if size>MAX_EXECUTABLE_BYTES:raise QueueError('invalid_executable_file')
   h.update(chunk)
 return h.hexdigest(),size

def live_ready(root,min_remaining=10):
 root=Path(root).absolute()
 if root.is_symlink() or root.stat().st_uid!=os.getuid() or root.stat().st_mode&0o077:raise QueueError('private_service_root_required')
 if (root/'closed.json').exists() or (root/'stop.requested').exists():raise QueueError('service_closed')
 saved=read(root/'ready.json',8192)
 if not isinstance(saved,dict) or saved.get('version')!=1 or saved.get('session_mapping')!=MAPPING:raise QueueError('session_adapter_required')
 if not isinstance(saved.get('instance'),str) or not re.fullmatch(r'[0-9a-f]{32}',saved['instance']):raise QueueError('invalid_service_instance')
 if not number(saved.get('deadline')) or saved['deadline']<time.time()+min_remaining:raise QueueError('service_expired_or_too_short')
 port=endpoint(saved.get('base_url'));nonce=secrets.token_hex(16);conn=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
 try:
  conn.request('GET','/bridge/ready?nonce='+nonce,headers={'Accept':'application/json'})
  response=conn.getresponse();lengths=response.headers.get_all('Content-Length') or []
  if response.status!=200:raise QueueError('bridge_or_broker_not_ready')
  if len(lengths)!=1 or not re.fullmatch(r'[0-9]{1,5}',lengths[0]) or int(lengths[0])>8192 or response.headers.get_content_type()!='application/json':raise QueueError('invalid_readiness_response')
  data=response.read(8193)
  if len(data)!=int(lengths[0]) or len(data)>8192:raise QueueError('invalid_readiness_response')
  result=protocol.decode(data)
  if not isinstance(result,dict) or result.get('version')!=1 or result.get('status')!='ready' or result.get('nonce')!=nonce or any(result.get(k)!=saved[k] for k in ('instance','base_url','deadline','session_mapping')):raise QueueError('readiness_correlation_failed')
  broker=result.get('broker',{})
  if not isinstance(broker,dict) or not isinstance(broker.get('challenge'),str) or not re.fullmatch(r'[0-9a-f]{32}',broker['challenge']) or any(not number(broker.get(k)) or not -1<=time.time()-broker[k]<=5 for k in ('ack_at','heartbeat_at')):raise QueueError('broker_freshness_failed')
  if saved['deadline']<time.time()+min_remaining or (root/'closed.json').exists():raise QueueError('service_closed')
  return result
 except (OSError,TimeoutError,http.client.HTTPException,ValueError,UnicodeError,KeyError):raise QueueError('readiness_transport_failed') from None
 finally:conn.close()

def stage(root,destination,codex_path,prompt=None):
 """No execution. A partially written new directory has no valid final manifest."""
 ready=live_ready(root);dest=Path(destination).absolute();binary=Path(codex_path).resolve(strict=True)
 if dest.exists() or dest.is_symlink():raise QueueError('destination_must_be_new')
 if not binary.is_file() or not os.access(binary,os.X_OK):raise QueueError('codex_path_not_executable')
 binary_hash,binary_size=binary_fingerprint(binary)
 if prompt is not None and (not isinstance(prompt,str) or len(prompt.encode())>16384):raise QueueError('invalid_prompt')
 dest.mkdir(mode=0o700,parents=True)
 for name in ('codex-home','home','workspace','tmp'):(dest/name).mkdir(mode=0o700)
 config=protocol.config_text(ready['base_url'],240)
 # atomic_json cannot write TOML; use the same fsync + replace + directory fsync pattern.
 path=dest/'codex-home/config.toml';temp=path.with_name('.config-staging')
 fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
 with os.fdopen(fd,'wb') as f:f.write(config.encode());f.flush();os.fsync(f.fileno())
 os.replace(temp,path);dfd=os.open(path.parent,os.O_DIRECTORY)
 try:os.fsync(dfd)
 finally:os.close(dfd)
 env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'LANG':os.environ.get('LANG','C.UTF-8'),'TERM':os.environ.get('TERM','xterm-256color'),'CODEX_HOME':str(dest/'codex-home'),'HOME':str(dest/'home'),'TMPDIR':str(dest/'tmp'),'XDG_RUNTIME_DIR':str(dest/'tmp'),'NO_PROXY':'127.0.0.1,localhost'}
 argv=[str(binary),'--no-daemon','--no-alt-screen','--sandbox','read-only','--ask-for-approval','on-request','-C',str(dest/'workspace')]
 if prompt is not None:argv+=['--',prompt]
 manifest={'version':1,'stage_complete':True,'executed':False,'service_root':str(Path(root).absolute()),'instance':ready['instance'],'base_url':ready['base_url'],'service_deadline':ready['deadline'],'session_mapping':MAPPING,'destination':str(dest),'config_sha256':hashlib.sha256(config.encode()).hexdigest(),'argv':argv,'binary_path':str(binary),'binary_sha256':binary_hash,'binary_bytes':binary_size,'env':env,'sandbox':'read-only','approvals':'on-request','provider':'native_bridge','model':'native-subagent-bridge','original_config_modified':False,'auth_copied':False,'no_fallback':True,'staged_at':time.time()}
 protocol.atomic_json(dest/'launch.json',manifest,exclusive=True)
 return manifest

def execute_staged(destination):
 """Explicit deployment operation. Re-check readiness and config immediately; never fallback."""
 dest=Path(destination).absolute()
 for path in [dest,*[dest/n for n in ('home','codex-home','workspace','tmp')]]:private_directory(path)
 m=read(dest/'launch.json',32768)
 if not isinstance(m,dict) or not isinstance(m.get('argv'),list) or not all(isinstance(x,str) for x in m['argv']) or not isinstance(m.get('env'),dict):raise QueueError('invalid_manifest')
 if (dest/'execution-claim.json').exists():raise QueueError('launch_already_attempted')
 if m.get('version')!=1 or m.get('stage_complete') is not True or m.get('destination')!=str(dest):raise QueueError('incomplete_stage')
 # The local manifest is user-controlled, but reject edits rather than accepting arbitrary overrides.
 expected=protocol.config_text(m['base_url'],240).encode();actual=bounded_bytes(dest/'codex-home/config.toml',16384)
 if actual!=expected or hashlib.sha256(actual).hexdigest()!=m['config_sha256']:raise QueueError('config_changed')
 ready=live_ready(m['service_root'])
 if any(ready[k]!=m[k] for k in ('instance','base_url','session_mapping')):raise QueueError('service_instance_changed')
 required={'CODEX_HOME':str(dest/'codex-home'),'HOME':str(dest/'home'),'TMPDIR':str(dest/'tmp'),'XDG_RUNTIME_DIR':str(dest/'tmp'),'NO_PROXY':'127.0.0.1,localhost'}
 if set(m['env'])!={'PATH','LANG','TERM',*required} or any(m['env'][k]!=v for k,v in required.items()):raise QueueError('environment_changed')
 prefix=['--no-daemon','--no-alt-screen','--sandbox','read-only','--ask-for-approval','on-request','-C',str(dest/'workspace')]
 if m['argv'][1:9]!=prefix or (len(m['argv'])!=9 and not(len(m['argv'])==11 and m['argv'][9]=='--')):raise QueueError('argv_changed')
 if not m['argv'] or m['argv'][0]!=m['binary_path'] or binary_fingerprint(m['binary_path'])!=(m['binary_sha256'],m['binary_bytes']):raise QueueError('executable_changed')
 if time.time()>=ready['deadline']:raise QueueError('service_expired_or_too_short')
 # One dedicated launcher per service instance, across different staging destinations.
 try:protocol.atomic_json(Path(m['service_root'])/'launcher-admission.json',{'instance':m['instance'],'destination':str(dest),'at':time.time()},exclusive=True)
 except FileExistsError:raise QueueError('service_launch_already_attempted') from None
 # Atomic single-use admission: even ambiguous start failures cannot be blindly retried.
 try:protocol.atomic_json(dest/'execution-claim.json',{'state':'starting','at':time.time(),'instance':m['instance'],'pid':os.getpid()},exclusive=True)
 except FileExistsError:raise QueueError('launch_already_attempted') from None
 outcome={'state':'start_failed','at':time.time()};process=None
 try:
  process=subprocess.Popen(m['argv'],env=m['env'],cwd=dest/'workspace')
  m['executed']=True;m['child_pid']=process.pid;protocol.atomic_json(dest/'launch.json',m)
  while process.poll() is None:
   if time.time()>=ready['deadline'] or (Path(m['service_root'])/'closed.json').exists() or (Path(m['service_root'])/'stop.requested').exists():
    process.terminate()
    try:process.wait(timeout=5)
    except subprocess.TimeoutExpired:process.kill();process.wait()
    outcome['reason']='service_closed';break
   time.sleep(.05)
  outcome.update(state='exited',returncode=process.returncode,finished_at=time.time());return process.returncode
 except BaseException:
  if process is not None and process.poll() is None:
   process.terminate()
   try:process.wait(timeout=5)
   except subprocess.TimeoutExpired:process.kill();process.wait()
  outcome.update(state='failed_or_interrupted',finished_at=time.time());raise
 finally:protocol.atomic_json(dest/'execution-result.json',outcome)


def main():
 p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='operation')
 s=sub.add_parser('stage');s.add_argument('--service-root',required=True);s.add_argument('--dest',required=True);s.add_argument('--codex',required=True);s.add_argument('--prompt')
 r=sub.add_parser('run');r.add_argument('--dest',required=True)
 a=p.parse_args();os.umask(0o077)
 if a.operation=='run':return execute_staged(a.dest)
 if a.operation!='stage':p.error('Choose stage (default intended workflow) or explicit run')
 m=stage(a.service_root,a.dest,a.codex,a.prompt);print(json.dumps({'staged':m['destination'],'executed':False,'provider':m['provider'],'base_url':m['base_url'],'session_mapping':m['session_mapping']}));return 0
if __name__=='__main__':
 try:raise SystemExit(main())
 except (QueueError,OSError,ValueError,KeyError,TypeError) as e:print(json.dumps({'error':getattr(e,'code',type(e).__name__)}),file=sys.stderr);raise SystemExit(1)
