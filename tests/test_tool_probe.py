import copy,json,sys,tempfile,time,unittest,uuid
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from portable import init,Deployment,FileQueue,QueueError,protocol
import probe,broker_bootstrap as broker,tool_probe as tp

def request(text='probe',namespace=None):
 f={'type':'function','name':'exec_command','parameters':{'type':'object','properties':{'cmd':{'type':'string'},'login':{'type':'boolean'},'sandbox_permissions':{'type':'string','enum':['use_default','require_escalated']},'yield_time_ms':{'type':'number'},'max_output_tokens':{'type':'number'}},'required':['cmd'],'additionalProperties':False}}
 return {'model':'native-subagent-bridge','stream':True,'tools':[f] if namespace is None else [{'type':'namespace','name':namespace,'tools':[f]}],'input':[{'role':'user','content':[{'type':'input_text','text':text}]}]}
def intent(namespace=None):
 x={'kind':'function_call','name':'exec_command','arguments':dict(tp.ARGUMENTS)}
 if namespace is not None:x['namespace']=namespace
 return x
class ToolProbe(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'root';init(self.root,'owner',seconds=60,scope='tool_probe');self.d=Deployment(self.root)
  probe.observe(self.d,'desktop');probe.offer(self.d);probe.observe(self.d,'broker');probe.answer(self.d);probe.verify(self.d)
  self.q=FileQueue(self.d.queue_root,create=True,owner='owner',session='session_'+self.d.m['run_id'],max_jobs=3);protocol.atomic_json(self.d.queue_root/'ready.json',{'version':1,'instance':uuid.uuid4().hex,'deadline':time.time()+60});self.a=self.d.assign('owner','broker','b',native_attested=True)
 def tearDown(self):self.q.close();self.tmp.cleanup()
 def err(self,fn,*a):
  with self.assertRaises(QueueError):fn(*a)
 def start(self,req=None):
  self.q.enqueue(req or request(),owner='owner',session=self.q.meta['session'],timeout=30);t=broker.claim(self.d,self.a,0,10);broker.begin(self.d,self.a,t);return t
 def call(self):
  t=self.start();r=broker.complete(self.d,self.a,t,intent());self.q.delivery(t['job']['id'],owner='owner',session=self.q.meta['session'],value='delivered');return t,r
 def followup(self,first,output=None):
  req=request('followup');call=protocol.validate_result(intent(),request(),first['job']['id']);req['input'] += [call,{'type':'function_call_output','call_id':call['call_id'],'output':output or 'Chunk ID: abc123\nWall time: 0.020 seconds\nProcess exited with code 0\nOutput:\nTOOL_NONCE=0123456789abcdef01234567\n'}];return req
 def test_scope_optin_and_default(self):
  m=init(Path(self.tmp.name)/'default','owner');self.assertEqual('text_only',m['scope']);self.assertEqual('tool_probe',self.d.m['scope'])
 def test_exact_current_advertisement(self):
  self.assertEqual('function_call',tp.validate_function(request(),intent(),'a'*32)['type']);self.assertEqual('functions',tp.validate_function(request(namespace='functions'),intent('functions'),'a'*32)['namespace']);self.err(tp.validate_function,request(namespace='functions'),intent(),'a'*32)
 def test_schema_required_types_and_bounds(self):
  for change in [lambda p:p['required'].append('extra'),lambda p:p['properties']['login'].update(type='string'),lambda p:p['properties']['max_output_tokens'].update(maximum=1)]:
   req=request();change(req['tools'][0]['parameters']);self.err(tp.validate_function,req,intent(),'a'*32)
 def test_schema_references_fail_closed(self):
  for key in ['$ref','$dynamicRef','$recursiveRef']:
   for val in ['https://example.invalid/schema','#/local']:
    req=request();req['tools'][0]['parameters'][key]=val;self.err(tp.validate_function,req,intent(),'a'*32)
 def test_no_command_override_or_escalation(self):
  for key,value in [('cmd','echo unsafe'),('login',True),('sandbox_permissions','require_escalated'),('workdir','/tmp'),('shell','/bin/sh'),('justification','test')]:
   v=intent();v['arguments'][key]=value;self.err(tp.validate_function,request(),v,'a'*32)
 def test_unknown_or_custom_tool_rejected(self):
  v=intent();v['name']='different';self.err(tp.validate_function,request(),v,'a'*32);self.err(tp.validate_function,request(),{'kind':'custom_tool_call','name':'exec_command','input':'x'},'a'*32)
 def test_call_projection_and_exact_retry(self):
  t=self.start();self.assertEqual('completed',broker.complete(self.d,self.a,t,intent())['state']);self.assertTrue(broker.complete(self.d,self.a,t,intent())['idempotent'])
 def test_one_invocation_only(self):
  t,_=self.call();u=self.start(self.followup(t));self.err(broker.complete,self.d,self.a,u,intent())
 def test_followup_requires_matching_actual_output(self):
  t,_=self.call();u=self.start(self.followup(t));self.assertEqual('completed',broker.complete(self.d,self.a,u,{'kind':'message','text':'TOOL_OK:76543210fedcba9876543210'})['state'])
 def test_wrong_and_early_final_rejected(self):
  t=self.start();self.err(broker.complete,self.d,self.a,t,{'kind':'message','text':'fake'});broker.complete(self.d,self.a,t,intent());self.q.delivery(t['job']['id'],owner='owner',session=self.q.meta['session'],value='delivered');u=self.start(self.followup(t));self.err(broker.complete,self.d,self.a,u,{'kind':'message','text':'TOOL_OK:'+'0'*24})
 def test_wrong_call_duplicate_failed_running_output(self):
  t,_=self.call()
  with self.d.locked() as s:reservation=s['tool_probe']
  req=self.followup(t);req['input'][-1]['call_id']='call_wrong';self.err(tp.expected_final,req,reservation)
  req=self.followup(t);req['input'].append(copy.deepcopy(req['input'][-1]));self.err(tp.expected_final,req,reservation)
  for output in ['Wall time: 1.0 seconds\nProcess running with session ID 3\nOutput:\nTOOL_NONCE='+'0'*24,'Wall time: 0.1 seconds\nProcess exited with code 1\nOutput:\nTOOL_NONCE='+'0'*24,'Wall time: 0.1 seconds\nProcess exited with code 1\nOutput:\nProcess exited with code 0\nTOOL_NONCE='+'0'*24]:self.err(tp.expected_final,self.followup(t,output),reservation)
 def test_order_and_extra_stdout_rejected(self):
  t,_=self.call()
  with self.d.locked() as s:r=s['tool_probe']
  req=self.followup(t);req['input'][-2:]=reversed(req['input'][-2:]);self.err(tp.expected_final,req,r)
  req=self.followup(t);req['input'][-1]['output']+='extra\n';self.err(tp.expected_final,req,r)
 def test_reservation_survives_queue_failure(self):
  t=self.start()
  with patch.object(FileQueue,'complete',side_effect=OSError('synthetic crash before queue commit')):
   with self.assertRaises(OSError):broker.complete(self.d,self.a,t,intent())
  with self.d.locked() as s:self.assertEqual(t['job']['id'],s['tool_probe']['job_id'])
  self.assertEqual('completed',broker.complete(self.d,self.a,t,intent())['state'])
if __name__=='__main__':unittest.main()
