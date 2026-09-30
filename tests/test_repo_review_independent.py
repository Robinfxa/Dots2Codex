"""Independent offline fixture checks. These do NOT fetch GitHub or prove desktop execution."""
import copy, hashlib, io, json, os, sys, tempfile, time, unittest, uuid
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from portable import init,Deployment,FileQueue,QueueError,protocol
import probe,broker_bootstrap as broker,repo_review as rr,repo_fetch as rf
from routing import Registry,initialize
from routing_worker import Worker
SHA='a'*40

def request():
 f={'type':'function','name':'exec_command','parameters':{'type':'object','properties':{'cmd':{'type':'string'},'login':{'type':'boolean'},'sandbox_permissions':{'type':'string','enum':['use_default','require_escalated']},'yield_time_ms':{'type':'number'},'max_output_tokens':{'type':'number'}},'required':['cmd'],'additionalProperties':False}}
 return {'model':'native-subagent-bridge','stream':True,'tools':[{'type':'namespace','name':'functions','tools':[f]}],'input':[{'role':'user','content':[{'type':'input_text','text':'Read and summarize public repository.'}]}]}

def intent(action='tree'):
 return {'kind':'function_call','name':'exec_command','namespace':'functions','arguments':dict(cmd=rr.command(action),**rr.FIXED_ARGS)}

def body(action,ok=True):
 b=dict(contract=rf.CONTRACT,repo=rf.REPO,request=action,ok=ok)
 if not ok:b['error']='network_unavailable'
 elif action['action']=='tree':b.update(commit=SHA,files=[{'path':'README.md','mode':'100644','bytes':40},{'path':'source.py','mode':'100644','bytes':40}],truncated=False)
 else:b.update(commit=action['commit'],path=action['path'],file_sha256='b'*64,file_bytes=40,total_lines=2,text='1: public source\n2: function',url='https://github.com/'+rf.REPO+'/blob/'+SHA+'/'+action['path'],truncated=False)
 return b

def output(b,code=None):
 code=int(not b['ok']) if code is None else code
 return 'Chunk ID: fixture\nWall time: 0.01 seconds\nProcess exited with code '+str(code)+'\nOutput:\n'+json.dumps(b)+'\n'

class Loop(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'root';init(self.root,'owner',seconds=120,scope='repo_review',max_requests=16);self.d=Deployment(self.root)
  probe.observe(self.d,'desktop');probe.offer(self.d);probe.observe(self.d,'broker');probe.answer(self.d);probe.verify(self.d)
  self.q=FileQueue(self.d.queue_root,create=True,owner='owner',session='session_'+self.d.m['run_id'],max_jobs=16);protocol.atomic_json(self.d.queue_root/'ready.json',{'version':1,'instance':uuid.uuid4().hex,'deadline':time.time()+120});self.a=self.d.assign('owner','broker','b',native_attested=True);self.req=request()
 def tearDown(self):self.q.close();self.tmp.cleanup()
 def start(self,req=None):
  r=copy.deepcopy(self.req if req is None else req);self.q.enqueue(r,owner='owner',session=self.q.meta['session'],timeout=60);t=broker.claim(self.d,self.a,0,60);broker.begin(self.d,self.a,t);return t
 def emit(self,act='tree',ok=True,deliver=True):
  t=self.start();r=intent(act);broker.complete(self.d,self.a,t,r)
  if deliver:self.q.delivery(t['job']['id'],owner='owner',session=self.q.meta['session'],value='delivered')
  call=protocol.validate_result(r,self.req,t['job']['id']);a=rr.parse_command(r['arguments']);self.req['input'] += [call,{'type':'function_call_output','call_id':call['call_id'],'output':output(body(a,ok))}];return t,r
 def test_multiple_calls_and_final(self):
  self.emit();self.emit('read '+SHA+' README.md 1 20');self.emit('search '+SHA+' source.py function');self.emit('read '+SHA+' source.py 1 20');t=self.start();r={'kind':'message','text':'Fixture summary based on four actual fixture receipts.'};self.assertEqual('completed',broker.complete(self.d,self.a,t,r)['state']);self.assertTrue(broker.complete(self.d,self.a,t,r)['idempotent'])
 def test_reject_shell_and_argument_overrides(self):
  for act in ['tree;id','tree\nwhoami','tree && id','tree > /tmp/x','tree $(id)','read '+SHA+' ../secret 1 2','read '+SHA+' /etc/passwd 1 2','read '+SHA+' a%2fb 1 2','read '+SHA+' .git/config 1 2','read '+SHA+' README.md 0 2','read '+SHA+' README.md 1 151','search '+SHA+' README.md x;id']:
   with self.subTest(act=act),self.assertRaises(QueueError):rr.parse_command(intent(act)['arguments'])
  for key,value in [('login',True),('sandbox_permissions','require_escalated'),('shell','/bin/sh'),('workdir','/tmp'),('yield_time_ms',True),('max_output_tokens',16384.0)]:
   a=intent()['arguments'];a[key]=value
   with self.subTest(key=key),self.assertRaises(QueueError):rr.parse_command(a)
 def test_missing_duplicate_wrong_order_and_call_id(self):
  self.emit();saved=copy.deepcopy(self.req)
  mutations=[lambda r:r['input'].pop(),lambda r:r['input'].append(copy.deepcopy(r['input'][-1])),lambda r:r['input'][-1].update(call_id='call_other'),lambda r:r['input'].__setitem__(slice(-2,None),list(reversed(r['input'][-2:]))),lambda r:r['input'].append({'type':'custom_tool_call','name':'other','input':'x'})]
  with self.d.locked() as s:reservations=s['repo_review']['calls']
  for mutate in mutations:
   req=copy.deepcopy(saved);mutate(req)
   with self.assertRaises(QueueError):rr.history(self.d,req,reservations)
 def test_changed_prior_receipt_rejected(self):
  self.emit();self.emit('read '+SHA+' README.md 1 20')
  changed=json.loads(self.req['input'][2]['output'].split('Output:\n',1)[1]);changed['description']='MUTATED PRIOR RECEIPT';self.req['input'][2]['output']=output(changed)
  t=self.start()
  with self.assertRaises(QueueError):broker.complete(self.d,self.a,t,intent('search '+SHA+' source.py function'))
 def test_source_must_have_delivered(self):
  self.emit(deliver=False)
  with self.d.locked() as s:reservations=s['repo_review']['calls']
  with self.assertRaises(QueueError):rr.history(self.d,self.req,reservations)
 def test_commit_and_path_pinned(self):
  self.emit();t=self.start()
  for act in ['tree','read '+'c'*40+' README.md 1 2','read '+SHA+' unknown.py 1 2']:
   with self.assertRaises(QueueError):broker.complete(self.d,self.a,t,intent(act))
 def test_terminal_error_allows_final_but_no_retry(self):
  self.emit(ok=False);t=self.start()
  with self.assertRaises(QueueError):broker.complete(self.d,self.a,t,intent())
  self.assertEqual('completed',broker.complete(self.d,self.a,t,{'kind':'message','text':'The fetch failed; repository content was not available.'})['state'])
 def test_running_receipt_remains_unresolved(self):
  self.emit();self.req['input'][-1]['output']='Wall time: 30 seconds\nProcess running with session ID 33\nOutput:\n';t=self.start()
  with self.assertRaises(QueueError):broker.complete(self.d,self.a,t,{'kind':'message','text':'The read failed.'})
 def test_exact_retry_after_reservation_commit_fault(self):
  t=self.start();r=intent()
  with patch.object(FileQueue,'complete',side_effect=OSError('injected fixture crash')):
   with self.assertRaises(OSError):broker.complete(self.d,self.a,t,r)
  with self.d.locked() as s:self.assertEqual(1,len(s['repo_review']['calls']))
  with self.assertRaises(QueueError):broker.complete(self.d,self.a,t,intent('read '+SHA+' README.md 1 2'))
  self.assertEqual('completed',broker.complete(self.d,self.a,t,r)['state']);self.assertTrue(broker.complete(self.d,self.a,t,r)['idempotent'])
 def test_tool_budget_and_final_after_budget(self):
  self.emit()
  for i in range(1,12):self.emit('read '+SHA+' README.md '+str(i)+' 1')
  t=self.start()
  with self.assertRaises(QueueError):broker.complete(self.d,self.a,t,intent('read '+SHA+' README.md 12 1'))
  self.assertEqual('completed',broker.complete(self.d,self.a,t,{'kind':'message','text':'Bounded fixture research complete.'})['state'])
 def test_no_final_before_fetch(self):
  t=self.start()
  with self.assertRaises(QueueError):broker.complete(self.d,self.a,t,{'kind':'message','text':'No evidence.'})

class RoutingBounds(unittest.TestCase):
 def test_exposed_failover_and_unfenced_assignment_denied(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)/'registry';initialize(root,'owner',seconds=120,max_sessions=2);reg=Registry(root);r=reg.create_session('owner','research','repo_review',seconds=100,max_requests=16)
   d=Deployment(root/'sessions/research');probe.observe(d,'desktop');probe.offer(d);probe.observe(d,'broker');probe.answer(d);probe.verify(d)
   q=FileQueue(d.queue_root,create=True,owner='owner',session='session_'+d.m['run_id'],max_jobs=16)
   try:
    protocol.atomic_json(d.queue_root/'ready.json',dict(version=1,instance=uuid.uuid4().hex,deadline=time.time()+100));reg.dispatch_started('owner',r['admission_id'],'independent-fixture');c=reg.confirm('owner',r['admission_id'],'independent-fixture/research/1');w=Worker(root,c)
    q.enqueue(request(),owner='owner',session=q.meta['session'],timeout=60);t=w.claim(0,60);w.read(t);w.complete(t,intent());q.delivery(t['broker_ticket']['job']['id'],owner='owner',session=q.meta['session'],value='delivered');w.close()
    with self.assertRaises(QueueError) as failure:reg.failover('owner','research')
    self.assertEqual('repo_review_replacement_not_supported',failure.exception.code)
    self.assertEqual(1,reg.snapshot()['sessions']['research']['generation'])
    with self.assertRaises(QueueError):d.assign('owner','broker','replacement',native_attested=True)
   finally:q.close()

class Helper(unittest.TestCase):
 def test_fixed_read_url_and_bounds(self):
  a=rf.parse(['read',SHA,'README.md','1','2'])
  with patch.object(rf,'fetch',return_value=b'first\nsecond\nthird\n') as fetch:
   result=rf.run(a);fetch.assert_called_once_with(rf.RAW+'/'+SHA+'/README.md');self.assertEqual('1: first\n2: second',result['text'])
 def test_search_literal_and_binary_rejected(self):
  a=rf.parse(['search',SHA,'source.py','f.n'])
  with patch.object(rf,'fetch',return_value=b'fun\nf.n\nF.N\n'):
   result=rf.run(a);self.assertEqual(2,result['matched_lines'])
  with patch.object(rf,'fetch',return_value=b'x\x00y'):
   with self.assertRaises(rf.FetchError):rf.run(a)
 def test_tree_regular_files_only(self):
  responses=[{'default_branch':'main','full_name':rf.REPO,'private':False},{'sha':SHA},{'truncated':False,'tree':[{'path':'README.md','type':'blob','mode':'100644','size':40},{'path':'link','type':'blob','mode':'120000','size':5},{'path':'sub','type':'commit','mode':'160000','size':5},{'path':'large','type':'blob','mode':'100644','size':rf.MAX_BODY+1},{'path':'../escape','type':'blob','mode':'100644','size':5}]}]
  with patch.object(rf,'fetch',side_effect=[json.dumps(r).encode() for r in responses]):
   result=rf.run({'action':'tree'});self.assertEqual(['README.md'],[e['path'] for e in result['files']])
 def test_duplicate_json_rejected(self):
  with self.assertRaises(rf.FetchError):rf.strict_json('{"a":1,"a":2}')
 def test_redirect_handler_refuses(self):
  with self.assertRaises(rf.FetchError):rf.NoRedirect().redirect_request(None,None,302,'redirect',{},'https://evil.invalid/')

if __name__=='__main__':unittest.main(verbosity=2)
