"""Desktop-role bootstrap. Default is a plan; --execute explicitly starts own CLI."""
import argparse,json,os,signal,sys,threading,time
from pathlib import Path
from portable import Deployment,QueueError,FileQueue,protocol,encode
from probe import require_verified
from service_adapter import ReadyService
from launcher import stage,execute_staged

def run(d,owner,worker,codex,prompt,execute=False):
 d.scope(owner);require_verified(d)
 if not execute:return {'contract':d.m['contract'],'action':'plan_only','queue_relative':d.m['paths']['queue'],'cli_relative':d.m['paths']['cli'],'same_environment_required':['facade','official_codex'],'broker':'separate active native agent using same shared files','notification':'outbox only unless explicitly acknowledged','will_spawn':False}
 a=d.assign(owner,'desktop',worker,lease=240);service=None;code='none';exitcode=None;stop=threading.Event();heart=None;tty_saved=None;previous={}
 def beat():
  while not stop.wait(10):
   try:d.heartbeat(a,'busy',240)
   except (QueueError,OSError):
    # Stop only this deployment's owned service/CLI; no unrelated process actions.
    if d.queue_root.exists():protocol.atomic_json(d.queue_root/'stop.requested',{'reason':'control_plane_not_live'})
    stop.set();return
 def interrupted(*_):raise KeyboardInterrupt()
 try:
  if sys.stdin.isatty():
   try:
    import termios
    tty_saved=termios.tcgetattr(sys.stdin.fileno())
   except (ImportError,OSError,ValueError):pass
  for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):previous[sig]=signal.signal(sig,interrupted)
  q=FileQueue(d.queue_root,create=True,owner=owner,session='session_'+d.m['run_id'],max_jobs=d.m['policy']['max_requests']);q.close()
  remaining=d.m['expires']-time.time()
  if remaining<=1:raise QueueError('deployment_expired')
  service=ReadyService(d.queue_root,lifetime=min(900,remaining),request_deadline=min(180,remaining)).start()
  d.receipt(a,'ready');heart=threading.Thread(target=beat,daemon=True);heart.start()
  until=time.monotonic()+min(45,max(0,remaining-1))
  while True:
   if stop.is_set():raise QueueError('deployment_blocked')
   try:stage(d.queue_root,d.root/d.m['paths']['cli'],Path(codex),prompt);break
   except QueueError as exc:
    if exc.code not in ('bridge_or_broker_not_ready','readiness_transport_failed') or time.monotonic()>=until:raise
    time.sleep(.1)
  exitcode=execute_staged(d.root/d.m['paths']['cli']);code='normal_exit' if exitcode==0 else 'service_closed' if time.time()>=d.m['expires'] else 'configuration_error'
 except KeyboardInterrupt:code='user_stop'
 except (QueueError,OSError,ValueError,TypeError):code='configuration_error'
 finally:
  stop.set()
  if heart:heart.join(1)
  if service:service.close()
  if tty_saved is not None:
   try:termios.tcsetattr(sys.stdin.fileno(),termios.TCSANOW,tty_saved)
   except (OSError,ValueError):pass
  for sig,handler in previous.items():signal.signal(sig,handler)
  # Preserve the initiating stop cause; cleanup errors do not reclassify it.
  with d.locked() as control:
   if control['blocked']:code=control['blocked']
  # A raw process crash cannot promise this finally/outbox runs.
  outcome={'contract':d.m['contract'],'deployment_id':d.m['deployment_id'],'returncode':exitcode,'code':code,'at':time.time(),'service_stopped':service is None or not service.facade.thread.is_alive(),'notification_delivered':False}
  protocol.atomic_json(d.root/'evidence/desktop-outcome.json',outcome)
  try:d.receipt(a,'stopped' if code in ('normal_exit','service_closed') else 'blocked',code)
  finally:d.heartbeat(a,'closed')
 return outcome

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True);p.add_argument('--owner',required=True);p.add_argument('--worker',default='desktop-process');p.add_argument('--codex');p.add_argument('--prompt',default='Reply with one short text sentence. Do not call tools.');p.add_argument('--execute',action='store_true');args=p.parse_args();os.umask(0o077)
 if args.execute and not args.codex:raise QueueError('explicit_codex_path_required')
 out=run(Deployment(args.root),args.owner,args.worker,args.codex,args.prompt,args.execute);print(encode(out).decode());return 0 if not args.execute or out['returncode']==0 else 1
if __name__=='__main__':
 try:raise SystemExit(main())
 except (QueueError,OSError,ValueError,KeyError,TypeError) as e:print(json.dumps({'error':getattr(e,'code',type(e).__name__)}),file=sys.stderr);raise SystemExit(1)
