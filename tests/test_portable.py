import concurrent.futures,hashlib,json,os,sys,tempfile,time,unittest,uuid
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import portable,probe,broker_bootstrap as broker
from portable import Deployment,QueueError,FileQueue,protocol,read
from file_queue import request_for_text

class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'fresh';portable.init(self.root,'owner',seconds=30);self.d=Deployment(self.root)
 def tearDown(self):self.tmp.cleanup()
 def handshake(self):
  probe.observe(self.d,'desktop');probe.offer(self.d);probe.observe(self.d,'broker');probe.answer(self.d);return probe.verify(self.d)
 def queue(self):
  q=FileQueue(self.d.queue_root,create=True,owner='owner',session='session_'+self.d.m['run_id'],max_jobs=3)
  protocol.atomic_json(self.d.queue_root/'ready.json',{'version':1,'instance':uuid.uuid4().hex,'started':time.time(),'deadline':self.d.m['expires'],'session_mapping':'codex-0.159.2-hyphen-pair-v1'})
  return q
 def actor(self,lease=10,worker='broker1'):return self.d.assign('owner','broker',worker,lease,native_attested=True)
 def setup_job(self,lease=10,timeout=20):
  self.handshake();q=self.queue();q.enqueue(request_for_text('SYNTHETIC hello'),owner='owner',session=q.meta['session'],timeout=timeout);a=self.actor(lease);t=broker.claim(self.d,a,0,lease);self.d.heartbeat(a,'busy',lease);return q,a,t
 def err(self,code,fn,*a,**kw):
  with self.assertRaises(QueueError) as cm:fn(*a,**kw)
  self.assertEqual(code,cm.exception.code)
 def test_fresh_root_and_dynamic_paths(self):
  self.err('deployment_already_exists',portable.init,self.root,'owner');r=Path(self.tmp.name)/'second';m=portable.init(r,'owner');self.assertNotEqual(m['deployment_id'],self.d.m['deployment_id']);self.assertNotEqual(m['paths']['queue'],self.d.m['paths']['queue']);self.assertFalse(self.d.queue_root.exists())
 def test_capability_attestation_and_owner(self):
  self.err('native_capability_not_attested',self.d.assign,'owner','broker','b');self.err('owner_mismatch',self.d.assign,'other','desktop','d')
 def test_nonce_handshake(self):
  r=self.handshake();self.assertTrue(r['shared_file_roundtrip']);self.assertFalse(r['distinct_namespaces_observed']);self.assertFalse(r['native_inference_verified'])
 def test_wrong_probe_version_and_run(self):
  self.handshake();p=self.root/'probe/verified.json';r=read(p);r['contract']='other';protocol.atomic_json(p,r);self.err('probe_scope_mismatch',probe.require_verified,self.d)
 def test_concurrent_assign(self):
  def assign(i):
   try:return self.d.assign('owner','broker','b'+str(i),native_attested=True)
   except QueueError:return None
  with concurrent.futures.ThreadPoolExecutor(8) as e:r=list(e.map(assign,range(8)))
  self.assertEqual(1,sum(x is not None for x in r))
 def test_claim_read_complete_idempotent(self):
  q,a,t=self.setup_job()
  try:
   self.assertNotIn('request',t['job']);payload=broker.begin(self.d,a,t);self.assertEqual('SYNTHETIC hello',payload['request']['input'][0]['content'][0]['text']);result={'kind':'message','text':'synthetic'}
   out=broker.complete(self.d,a,t,result);self.assertFalse(out['idempotent']);self.assertEqual('waiting',out['delivery']);self.assertTrue(broker.complete(self.d,a,t,result)['idempotent'])
   self.err('stale_lease_or_correlation',broker.complete,self.d,a,t,{'kind':'message','text':'changed'})
   self.assertEqual('waiting',broker.status(self.d,a,t)['delivery']);q.delivery(t['job']['id'],owner='owner',session=q.meta['session'],value='delivered');self.assertEqual('delivered',broker.status(self.d,a,t)['delivery'])
  finally:q.close()
 def test_tool_intent_rejected(self):
  q,a,t=self.setup_job()
  try:broker.begin(self.d,a,t);self.err('text_only_result_required',broker.complete,self.d,a,t,{'kind':'function_call','name':'anything','arguments':{}})
  finally:q.close()
 def test_crash_before_read_takeover_and_fencing(self):
  q,a,t=self.setup_job(.05)
  try:
   time.sleep(.07);b=self.actor(10,'replacement');u=broker.claim(self.d,b,0,1);self.assertEqual(2,u['job']['lease_epoch']);self.err('stale_assignment',broker.begin,self.d,a,t);broker.begin(self.d,b,u);self.assertEqual('completed',broker.complete(self.d,b,u,{'kind':'message','text':'recovered'})['state'])
  finally:q.close()
 def test_ambiguous_after_read_requires_resolution(self):
  q,a,t=self.setup_job(.05)
  try:
   broker.begin(self.d,a,t);time.sleep(.07);self.err('ambiguous_dispatch',self.actor,10,'replacement');self.assertEqual('ambiguous_dispatch',self.d.recovery('owner')['reason']);self.d.resolve('owner',t['job']['id'],'confirmed_stopped','platform_turn_ended');b=self.actor(10,'replacement');self.assertEqual(2,b['epoch'])
  finally:q.close()
 def test_expired_job_does_not_prove_native_stopped(self):
  q,a,t=self.setup_job(.05,.06)
  try:broker.begin(self.d,a,t);time.sleep(.08);self.err('ambiguous_dispatch',self.actor,10,'replacement')
  finally:q.close()
 def test_wrong_ticket_contract(self):
  q,a,t=self.setup_job()
  try:t['contract']='file-ipc-portable/99';self.err('ticket_deployment_mismatch',broker.begin,self.d,a,t)
  finally:q.close()
 def test_receipt_duplicate_stale_and_ack(self):
  a=self.actor(.05);rid=uuid.uuid4().hex;x=self.d.receipt(a,'ready',receipt_id=rid);self.assertFalse(x['idempotent']);self.assertTrue(self.d.receipt(a,'ready',receipt_id=rid)['idempotent']);self.err('receipt_id_conflict',self.d.receipt,a,'claimed',receipt_id=rid)
  r=read(self.root/'outbox'/(rid+'.json'));self.err('owner_mismatch',self.d.ack,'wrong',rid,r['content_sha256'],'test');self.assertEqual('read_observer',self.d.observe_receipt('owner',rid,'parent')['state']);self.d.ack('owner',rid,r['content_sha256'],'parent_message_1');self.assertTrue(self.d.ack('owner',rid,r['content_sha256'],'parent_message_1')['idempotent']);time.sleep(.07);self.actor(10,'new');self.err('stale_assignment',self.d.receipt,a,'ready')
 def test_ack_detects_tampering(self):
  a=self.actor();rid=self.d.receipt(a,'ready')['receipt_id'];p=self.root/'outbox'/(rid+'.json');r=read(p);sha=r['content_sha256'];r['run_id']=uuid.uuid4().hex;protocol.atomic_json(p,r);self.err('receipt_scope_or_hash_mismatch',self.d.ack,'owner',rid,sha,'parent_msg')
 def test_no_implicit_notification(self):
  a=self.actor();rid=self.d.receipt(a,'ready')['receipt_id'];r=read(self.root/'outbox'/(rid+'.json'));self.assertEqual('written_outbox',r['notification']);self.assertFalse(list((self.root/'acks').iterdir()));self.err('notification_capability_missing',self.d.ack,'owner',rid,r['content_sha256'],'msg','parent_tool')
 def test_stop_survives_outbox_write_failure(self):
  a=self.actor();original=protocol.atomic_json
  def fail(path,*args,**kw):
   if Path(path).parent.name=='outbox':raise OSError('synthetic')
   return original(path,*args,**kw)
  with patch.object(protocol,'atomic_json',fail):
   with self.assertRaises(OSError):self.d.receipt(a,'blocked','permission_denied')
  self.assertEqual('deployment_blocked',Deployment(self.root).recovery('owner')['reason'])
 def test_stop_even_when_outbox_full(self):
  a=self.actor()
  for _ in range(128):self.d.receipt(a,'ready')
  self.err('receipt_limit',self.d.receipt,a,'blocked','user_stop');self.assertEqual('deployment_blocked',self.d.recovery('owner')['reason'])
 def test_budget_persists(self):
  for i in range(3):
   a=self.actor(10,'worker'+str(i));self.d.heartbeat(a,'closed');self.d=Deployment(self.root)
  self.err('restart_budget_exhausted',self.actor,10,'fourth');self.assertEqual('restart_budget_exhausted',self.d.recovery('owner')['reason'])
 def test_desktop_single_use(self):
  a=self.d.assign('owner','desktop','d');self.d.heartbeat(a,'closed');self.err('restart_budget_exhausted',self.d.assign,'owner','desktop','d2')
 def test_fifo_and_symlink_root(self):
  x=Path(self.tmp.name)/'link';x.symlink_to(self.root);self.err('symlink_directory',Deployment,x);(self.root/'control/state.json').unlink();os.mkfifo(self.root/'control/state.json');self.err('not_regular_file',self.actor)
 def test_actual_synthetic_http_roundtrip(self):
  import http.client,threading
  from service_adapter import ReadyService
  self.handshake();q=FileQueue(self.d.queue_root,create=True,owner='owner',session='session_'+self.d.m['run_id'],max_jobs=3);q.close();service=ReadyService(self.d.queue_root,lifetime=5,request_deadline=3).start();a=self.actor();response={}
  def client():
   c=http.client.HTTPConnection('127.0.0.1',service.facade.server.server_port,timeout=4)
   try:
    thread=str(uuid.uuid4());body=protocol.encode(request_for_text('SYNTHETIC HTTP'))
    c.request('POST','/v1/responses',body,{'Content-Type':'application/json','session-id':thread,'thread-id':thread});r=c.getresponse();response.update(status=r.status,body=r.read().decode())
   finally:c.close()
  th=threading.Thread(target=client);th.start()
  try:
   t=broker.claim(self.d,a,2,2);self.assertIsNotNone(t);broker.begin(self.d,a,t);broker.complete(self.d,a,t,{'kind':'message','text':'SYNTHETIC HTTP RESULT'});th.join(4);self.assertFalse(th.is_alive());self.assertEqual(200,response['status']);self.assertIn('SYNTHETIC HTTP RESULT',response['body']);self.assertIn('response.completed',response['body'])
  finally:service.close();th.join(1)
 def test_deterministic_handoff_fixture(self):
  import handoff_fixture as h
  root=Path(self.tmp.name)/'handoff';h.prepare(root,'owner');d=Deployment(root);a=d.assign('owner','broker','fixture',native_attested=True);t=broker.claim(d,a,0,10);inp=broker.begin(d,a,t);ip=root/'evidence/input.json';rp=root/'evidence/result.json';protocol.atomic_json(ip,inp);h.reply(d,ip,rp);broker.complete(d,a,t,read(rp));self.assertTrue(h.verify(d)['passed']);self.assertEqual('waiting',h.verify(d)['delivery'])
 def test_machine_schemas_and_handoff(self):
  try:import jsonschema
  except ImportError:self.skipTest('optional jsonschema not installed')
  import handoff
  base=Path(__file__).resolve().parents[1]
  def check(name,value):
   schema=json.loads((base/'schemas'/(name+'.schema.json')).read_text());jsonschema.Draft202012Validator.check_schema(schema);jsonschema.Draft202012Validator(schema).validate(value)
  for p in (base/'schemas').glob('*.json'):jsonschema.Draft202012Validator.check_schema(json.loads(p.read_text()))
  check('deployment',self.d.m);q,a,t=self.setup_job()
  try:
   check('assignment',a);check('ticket',t);ap=self.root/'evidence/assignment.json';protocol.atomic_json(ap,a);check('handoff',handoff.packet(self.d,'owner',ap,base));self.d.heartbeat(a);check('heartbeat',read(self.root/'control/heartbeat-broker.json'));broker.begin(self.d,a,t);result={'kind':'message','text':'synthetic'};check('result',result);r=broker.complete(self.d,a,t,result);rid=r['receipt']['receipt_id'];receipt=read(self.root/'outbox'/(rid+'.json'));check('receipt',receipt);self.d.ack('owner',rid,receipt['content_sha256'],'parent_msg');check('acknowledgment',read(self.root/'acks'/(rid+'.json')));check('recovery',self.d.recovery('owner'))
  finally:q.close()
 def test_preserve_first_stop_reason(self):
  a=self.actor();self.d.receipt(a,'blocked','user_stop');self.d.receipt(a,'blocked','configuration_error')
  with self.d.locked() as s:self.assertEqual('user_stop',s['blocked'])
 def test_plan_never_launches(self):
  self.handshake();from desktop_bootstrap import run
  with patch('desktop_bootstrap.execute_staged',side_effect=AssertionError('must not run')):out=run(self.d,'owner','desktop',None,'test',False)
  self.assertFalse(out['will_spawn']);self.assertFalse(self.d.queue_root.exists())
if __name__=='__main__':unittest.main()
