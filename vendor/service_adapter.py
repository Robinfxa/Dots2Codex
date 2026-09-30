#!/usr/bin/env python3
"""Opt-in single Codex runtime session mapping + nonce readiness. No Codex launcher."""
import argparse,json,os,re,threading,time,urllib.parse,uuid
from pathlib import Path
from core import FileQueue,LocalFacade,ResponsesHandler,QueueError,protocol
from readiness import challenge
from facade import StoreAdapter

class SessionStore(StoreAdapter):
 def __init__(self,queue,owner):super().__init__(queue);self.owner=owner
 def enqueue(self,request,session,deadline):
  if self.owner.closed or time.time()>=self.owner.deadline or self.owner.facade.server.stop_event.is_set():raise QueueError('service_expired')
  identity=getattr(self.owner.context,'identity',None)
  if identity is None:raise QueueError('runtime_identity_missing')
  with self.owner.mapping_lock:
   if self.owner.client_identity is not None and self.owner.client_identity!=identity:raise QueueError('session_scope_mismatch')
   admitted=super().enqueue(request,session,deadline)
   if self.owner.client_identity is None:
    try:protocol.atomic_json(self.owner.root/'client-session-map.json',{'version':1,'instance':self.owner.instance,'client':identity,'queue_session':self.owner.queue.meta['session'],'first_job':admitted['job_id'],'mapped_at':time.time()},exclusive=True)
    except Exception:
     self.disconnect(admitted['job_id']);raise
    self.owner.client_identity=identity
   return admitted


class AdapterHandler(ResponsesHandler):
 def local_headers(self):
  return self.headers.get_all('Host')==[f'127.0.0.1:{self.server.server_port}'] and not self.headers.get_all('Origin') and not self.headers.get_all('Authorization')
 def do_GET(self):
  if not self.local_headers():self.send_json(403,'invalid_local_headers');return
  try:
   parts=urllib.parse.urlsplit(self.path);q=urllib.parse.parse_qs(parts.query,strict_parsing=True) if parts.query else {}
  except ValueError:self.send_json(400,'invalid_readiness_request');return
  if parts.path!='/bridge/ready' or set(q)!={'nonce'} or len(q['nonce'])!=1 or not re.fullmatch(r'[0-9a-f]{32}',q['nonce'][0]):self.send_json(400,'invalid_readiness_request');return
  owner=self.server.adapter
  if not owner.readiness_lock.acquire(blocking=False):self.send_json(409,'readiness_busy');return
  try:
   if self.server.stop_event.is_set() or time.time()>=owner.deadline:raise QueueError('service_not_live')
   if owner.client_identity is not None:raise QueueError('service_already_bound')
   with owner.queue.locked():states=owner.queue.states()
   if len(states)>=owner.queue.meta['max_jobs']:raise QueueError('request_capacity_exhausted')
   if any(s['state'] not in ('completed','failed','expired','cancelled') or (s['state']=='completed' and s['delivery']=='waiting') for s in states):raise QueueError('service_busy')
   broker=challenge(owner.root,owner.instance,wait=owner.readiness_wait)
   if self.server.stop_event.is_set() or time.time()>=owner.deadline:raise QueueError('service_not_live')
   body=protocol.encode({'version':1,'status':'ready','nonce':q['nonce'][0],'instance':owner.instance,'base_url':owner.base_url,'deadline':owner.deadline,'session_mapping':'codex-0.159.2-hyphen-pair-v1','broker':broker})
   self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.send_header('Connection','close');self.end_headers();self.wfile.write(body)
  except QueueError as e:self.send_json(503,e.code)
  except (OSError,ValueError,TypeError):self.send_json(503,'readiness_unavailable')
  finally:owner.readiness_lock.release();self.close_connection=True
 def do_POST(self):
  if not self.local_headers():self.send_json(403,'invalid_local_headers');return
  if self.path!='/v1/responses':self.send_json(404,'unsupported_endpoint');return
  if self.headers.get_all('session_id') or self.headers.get_all('thread_id'):self.send_json(400,'ambiguous_legacy_session_header');return
  identities={}
  for name in ('session-id','thread-id'):
   values=self.headers.get_all(name) or []
   pattern=r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}' if name=='thread-id' else r'[!-~]{1,256}'
   if len(values)!=1 or not re.fullmatch(pattern,values[0]):self.send_json(400,'canonical_runtime_session_required');return
   identities[name]=values[0]
  trace=self.headers.get_all('x-client-request-id') or []
  if trace and (len(trace)!=1 or trace[0]!=identities['thread-id']):self.send_json(400,'conflicting_client_request_id');return
  owner=self.server.adapter
  # Validation/admission in the frozen handler occurs before SessionStore commits mapping.
  self.headers['session_id']=owner.queue.meta['session']
  owner.context.identity=identities
  try:super().do_POST()
  finally:del owner.context.identity


class ReadyService:
 def __init__(self,root,lifetime=900,request_deadline=240,readiness_wait=3):
  if not .1<=lifetime<=900 or not .05<=request_deadline<=240 or not .05<=readiness_wait<=5:raise QueueError('invalid_limit')
  self.root=Path(root).absolute();self.instance=uuid.uuid4().hex;self.started=time.time();self.deadline=self.started+lifetime;self.readiness_wait=readiness_wait
  self.readiness_lock=threading.Lock();self.mapping_lock=threading.Lock();self.client_identity=None;self.context=threading.local();self.closed=False;self.facade=None
  if self.root.is_symlink():raise QueueError('symlink_root')
  if (self.root/'ready.json').exists() or (self.root/'client-session-map.json').exists():raise QueueError('runtime_already_used')
  protocol.atomic_json(self.root/'adapter-launch.json',{'instance':self.instance,'at':self.started},exclusive=True)
  try:
   self.facade=LocalFacade(self.root,deadline=request_deadline);self.queue=self.facade.queue
   self.facade.server.adapter=self;self.facade.server.RequestHandlerClass=AdapterHandler;self.facade.server.store=SessionStore(self.queue,self)
  except Exception:self.close();raise
 @property
 def base_url(self):return self.facade.base_url
 def start(self):
  try:
   self.facade.start()
   protocol.atomic_json(self.root/'ready.json',{'version':1,'instance':self.instance,'base_url':self.base_url,'started':self.started,'deadline':self.deadline,'session_mapping':'codex-0.159.2-hyphen-pair-v1','pid':os.getpid()},exclusive=True)
   return self
  except Exception:self.close();raise
 def close(self):
  if self.closed:return
  self.closed=True
  if self.facade:self.facade.close()
  protocol.atomic_json(self.root/'closed.json',{'instance':self.instance,'closed':time.time(),'services_stopped':self.facade is None or self.facade.thread is None or not self.facade.thread.is_alive()})
 def run(self):
  try:
   while time.time()<self.deadline and not (self.root/'stop.requested').exists():
    with self.queue.locked():s=self.queue.states()
    if len(s)>=self.queue.meta['max_jobs'] and all(x['state'] in ('completed','failed','expired','cancelled') and (x['state']!='completed' or x['delivery']!='waiting') for x in s) and not self.facade.server.inflight.locked():break
    time.sleep(.1)
  except KeyboardInterrupt:pass
  finally:self.close()
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True,type=Path);a=p.parse_args();os.umask(0o077);q=FileQueue(a.root,create=True,max_jobs=3);q.close();s=ReadyService(a.root).start();print(json.dumps({'base_url':s.base_url,'instance':s.instance}),flush=True);s.run()
