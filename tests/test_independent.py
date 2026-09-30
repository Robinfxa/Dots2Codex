#!/usr/bin/env python3
"""Synthetic portability/control-plane tests; never invokes CLI or native inference."""
import hashlib,json,os,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
import portable
from portable import Deployment,QueueError,FileQueue,protocol,init
import probe
import broker_bootstrap as broker

class Independent(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='portable-independent-')
        self.root=Path(self.tmp.name)/'deployment'
        init(self.root,'owner',seconds=60);self.d=Deployment(self.root)
    def tearDown(self):self.tmp.cleanup()
    def broker(self,worker='worker',**kwargs):return self.d.assign('owner','broker',worker,native_attested=True,**kwargs)
    def expire(self):
        with self.d.locked() as s:
            s['roles']['broker']['expires']=time.time()-1;self.d.save(s)
    def queue_fixture(self,timeout=10):
        probe.observe(self.d,'desktop');probe.observe(self.d,'broker');probe.offer(self.d);probe.answer(self.d);probe.verify(self.d)
        q=FileQueue(self.d.queue_root,create=True,owner='owner',session='session_'+self.d.m['run_id'])
        protocol.atomic_json(self.d.queue_root/'ready.json',{'version':1,'instance':'a'*32,'deadline':time.time()+30,'session_mapping':'codex-0.159.2-hyphen-pair-v1','base_url':'http://127.0.0.1:1/v1'})
        request={'model':'native-subagent-bridge','stream':True,'input':[{'role':'user','content':[{'type':'input_text','text':'synthetic'}]}]}
        job=q.enqueue(request,owner='owner',session='session_'+self.d.m['run_id'],timeout=timeout);q.close()
        return job,request
    def test_01_role_and_native_capability_fail_closed(self):
        for call in [lambda:self.d.assign('other','broker','worker',native_attested=True),lambda:self.d.assign('owner','broker','worker'),lambda:self.d.assign('owner','unknown','worker')]:
            with self.assertRaises(QueueError):call()
        self.assertEqual(json.loads((self.root/'control/state.json').read_bytes())['roles'],{})
    def test_02_concurrent_assignment_only_one_epoch(self):
        gate=threading.Barrier(6);accepted=[];rejected=[]
        def race(i):
            gate.wait()
            try:accepted.append(Deployment(self.root).assign('owner','broker','w'+str(i),native_attested=True))
            except QueueError as exc:rejected.append(exc.code)
        workers=[threading.Thread(target=race,args=(i,)) for i in range(6)]
        for t in workers:t.start()
        for t in workers:t.join(3);self.assertFalse(t.is_alive())
        self.assertEqual(len(accepted),1);self.assertEqual(len(rejected),5);self.assertEqual(accepted[0]['epoch'],1)
    def test_03_assignment_fencing_and_budget_survive_reload(self):
        old=self.broker();self.expire();self.d=Deployment(self.root);second=self.broker('replacement')
        self.assertEqual(second['epoch'],2)
        with self.assertRaises(QueueError):self.d.heartbeat(old)
        with self.assertRaises(QueueError):self.d.receipt(old,'ready')
        self.expire();third=Deployment(self.root).assign('owner','broker','third',native_attested=True);self.assertEqual(third['epoch'],3)
        self.expire();self.d=Deployment(self.root)
        self.assertEqual(self.d.recovery('owner')['reason'],'restart_budget_exhausted')
        with self.assertRaises(QueueError):self.broker('fourth')
    def test_04_outbox_pending_is_not_notification_delivery(self):
        a=self.broker();out=self.d.receipt(a,'ready',receipt_id='1'*32)
        self.assertEqual(out['notification'],'written_outbox')
        self.assertEqual(list((self.root/'acks').glob('*.json')),[])
        recovery=self.d.recovery('owner');self.assertFalse(recovery['executed']);self.assertFalse(recovery['notification_delivered'])
    def test_05_receipt_and_ack_dedupe_conflicts(self):
        a=self.broker();rid='2'*32;self.d.receipt(a,'ready',receipt_id=rid)
        self.assertTrue(self.d.receipt(a,'ready',receipt_id=rid)['idempotent'])
        with self.assertRaises(QueueError):self.d.receipt(a,'failed',receipt_id=rid)
        row=json.loads((self.root/'outbox'/f'{rid}.json').read_bytes())
        first=self.d.ack('owner',rid,row['content_sha256'],'manual:reviewed')
        self.assertFalse(first['idempotent']);self.assertTrue(self.d.ack('owner',rid,row['content_sha256'],'manual:reviewed')['idempotent'])
        with self.assertRaises(QueueError):self.d.ack('owner',rid,row['content_sha256'],'manual:changed')
        with self.assertRaises(QueueError):self.d.ack('owner',rid,'0'*64,'manual:reviewed')
        with self.assertRaises(QueueError):self.d.ack('owner',rid,row['content_sha256'],'tool:claimed','parent_tool')
    def test_06_stop_receipt_cannot_be_durable_while_unblocked(self):
        a=self.broker();rid='3'*32
        with patch.object(self.d,'save',side_effect=OSError('synthetic control write failure')):
            with self.assertRaises(OSError):self.d.receipt(a,'blocked','user_stop',receipt_id=rid)
        self.d=Deployment(self.root)
        # A successfully published stop cannot be lost at the subsequent state boundary.
        state=json.loads((self.root/'control/state.json').read_bytes())
        self.assertFalse((self.root/'outbox'/f'{rid}.json').exists() and state['blocked'] is None)
        self.d.receipt(a,'blocked','user_stop',receipt_id=rid)
        self.assertEqual(json.loads((self.root/'control/state.json').read_bytes())['blocked'],'user_stop')
        with self.assertRaises(QueueError):self.d.heartbeat(a)
        self.assertEqual(self.d.recovery('owner')['action'],'await_operator')
    def test_07_stop_survives_outbox_failure(self):
        a=self.broker();real=protocol.atomic_json
        def fail_outbox(path,value,*args,**kwargs):
            if Path(path).parent==self.root/'outbox':raise OSError('synthetic outbox failure')
            return real(path,value,*args,**kwargs)
        with patch.object(protocol,'atomic_json',side_effect=fail_outbox):
            with self.assertRaises(OSError):self.d.receipt(a,'blocked','permission_denied')
        self.d=Deployment(self.root)
        self.assertEqual(json.loads((self.root/'control/state.json').read_bytes())['blocked'],'permission_denied')
        with self.assertRaises(QueueError):self.broker('replacement')
    def test_08_auth_block_persists_and_no_automatic_recovery(self):
        a=self.broker();self.d.receipt(a,'blocked','auth_required');self.expire();self.d=Deployment(self.root)
        self.assertEqual(self.d.recovery('owner')['reason'],'deployment_blocked')
        with self.assertRaises(QueueError):self.broker('replacement')
    def test_09_deployment_schema_paths_and_fifo_fail_closed(self):
        p=self.root/'deployment.json';saved=p.read_bytes();m=json.loads(saved)
        for bad in [{**m,'contract':'file-ipc-portable/99'},{**m,'paths':{**m['paths'],'queue':'../outside'}},{**m,'expires':float('inf')}]:
            p.write_text(json.dumps(bad))
            with self.assertRaises(QueueError):Deployment(self.root)
        p.write_bytes(saved);p.unlink();os.mkfifo(p);start=time.monotonic()
        with self.assertRaises(QueueError):Deployment(self.root)
        self.assertLess(time.monotonic()-start,.2)
    def test_10_resolution_does_not_create_worker_or_notify(self):
        proposal=self.d.recovery('owner')
        self.assertEqual(proposal['action'],'assign_replacement_broker');self.assertFalse(proposal['executed']);self.assertFalse(proposal['notification_delivered'])
        self.assertEqual(json.loads((self.root/'control/state.json').read_bytes())['roles'],{})
    def test_11_full_outbox_still_blocks_user_stop(self):
        a=self.broker()
        for _ in range(128):self.d.receipt(a,'ready')
        try:self.d.receipt(a,'blocked','user_stop')
        except QueueError:pass
        self.assertEqual(json.loads((self.root/'control/state.json').read_bytes())['blocked'],'user_stop')
    def test_12_probe_and_ticket_unknown_version_rejected(self):
        self.queue_fixture();p=self.root/'probe/verified.json';value=json.loads(p.read_bytes());p.write_text(json.dumps({**value,'contract':'file-ipc-portable/99'}))
        with self.assertRaises(QueueError):probe.require_verified(self.d)
        p.write_text(json.dumps(value));a=self.broker();ticket=broker.claim(self.d,a,wait=0)
        ticket['contract']='file-ipc-portable/99'
        with self.assertRaises(QueueError):broker.begin(self.d,a,ticket)
    def test_13_claim_hides_request_and_begin_fences_inference(self):
        job,request=self.queue_fixture();a=self.broker();ticket=broker.claim(self.d,a,wait=0)
        self.assertNotIn('request',ticket['job'])
        shown=broker.begin(self.d,a,ticket);self.assertEqual(shown['request'],request)
        state=json.loads((self.root/'control/state.json').read_bytes());self.assertEqual(state['dispatch'][job['id']]['status'],'started')
        with self.assertRaises(QueueError):broker.begin(self.d,a,ticket)
        answer=broker.complete(self.d,a,ticket,{'kind':'message','text':'synthetic-result'})
        self.assertEqual(answer['state'],'completed');self.assertEqual(answer['receipt']['notification'],'written_outbox')
        replay=broker.complete(self.d,a,ticket,{'kind':'message','text':'synthetic-result'});self.assertTrue(replay['idempotent'])
        with self.assertRaises(QueueError):broker.complete(self.d,a,ticket,{'kind':'message','text':'changed'})
    def test_14_started_expired_job_needs_explicit_resolution(self):
        job,_=self.queue_fixture(timeout=.05);a=self.broker();ticket=broker.claim(self.d,a,wait=0);broker.begin(self.d,a,ticket)
        time.sleep(.06);self.expire()
        self.assertEqual(self.d.recovery('owner')['reason'],'ambiguous_dispatch')
        with self.assertRaises(QueueError):self.broker('replacement')
        self.d.resolve('owner',job['id'],'confirmed_stopped','synthetic-stop-confirmation')
        newer=self.broker('replacement');self.assertEqual(newer['epoch'],2)
        with self.assertRaises(QueueError):broker.complete(self.d,a,ticket,{'kind':'message','text':'late'})
    def test_15_changed_receipt_body_cannot_be_acknowledged(self):
        a=self.broker();rid='4'*32;self.d.receipt(a,'ready',receipt_id=rid)
        p=self.root/'outbox'/f'{rid}.json';record=json.loads(p.read_bytes());record['event']='completed';p.write_text(json.dumps(record))
        with self.assertRaises(QueueError):self.d.ack('owner',rid,record['content_sha256'],'manual:reviewed')
    def test_16_text_only_result_rejects_executable_intent(self):
        self.queue_fixture();a=self.broker();ticket=broker.claim(self.d,a,wait=0);broker.begin(self.d,a,ticket)
        for result in [{'kind':'function_call','name':'shell','arguments':'{}'},{'kind':'message','text':'ok','usage':{'total_tokens':1}}]:
            with self.assertRaises(QueueError):broker.complete(self.d,a,ticket,result)
        self.assertEqual(broker.status(self.d,a,ticket)['state'],'running')
    def test_17_ack_file_existence_is_not_acknowledgment(self):
        a=self.broker();rid='5'*32;self.d.receipt(a,'ready',receipt_id=rid)
        path=self.root/'acks'/f'{rid}.json';path.write_text('{}')
        with self.assertRaises(QueueError):self.d.receipt(a,'ready',receipt_id=rid)
        with self.assertRaises(QueueError):self.d.observe_receipt('owner',rid,'observer')
    def test_18_receipt_replay_revalidates_original_body(self):
        a=self.broker();rid='6'*32;self.d.receipt(a,'ready',receipt_id=rid)
        p=self.root/'outbox'/f'{rid}.json';r=json.loads(p.read_bytes());r['event']='completed';p.write_text(json.dumps(r))
        with self.assertRaises(QueueError):self.d.receipt(a,'ready',receipt_id=rid)
    def test_19_uncertain_begin_write_never_reveals_input_twice(self):
        self.queue_fixture();a=self.broker();ticket=broker.claim(self.d,a,wait=0);real_save=self.d.save
        def write_then_fail(s):real_save(s);raise OSError('synthetic uncertainty after durable marker')
        with patch.object(self.d,'save',side_effect=write_then_fail):
            with self.assertRaises(OSError):broker.begin(self.d,a,ticket)
        with self.assertRaises(QueueError):broker.begin(self.d,a,ticket)
        self.expire();self.assertEqual(self.d.recovery('owner')['reason'],'ambiguous_dispatch')
    def test_20_complete_crash_retries_receipt_without_new_inference(self):
        self.queue_fixture();a=self.broker();ticket=broker.claim(self.d,a,wait=0);broker.begin(self.d,a,ticket);result={'kind':'message','text':'synthetic'}
        with patch.object(self.d,'save',side_effect=OSError('synthetic state write failure after queue completion')):
            with self.assertRaises(OSError):broker.complete(self.d,a,ticket,result)
        self.assertEqual(broker.status(self.d,a,ticket)['state'],'completed')
        retry=broker.complete(self.d,a,ticket,result);self.assertTrue(retry['idempotent'])
        self.assertEqual(retry['receipt']['notification'],'written_outbox')
        with self.assertRaises(QueueError):broker.begin(self.d,a,ticket)
    def test_21_observation_is_separate_from_parent_ack(self):
        a=self.broker();rid=self.d.receipt(a,'ready')['receipt_id'];read=self.d.observe_receipt('owner',rid,'observer')
        self.assertEqual(read['state'],'read_observer');self.assertEqual(list((self.root/'acks').glob('*.json')),[])
        self.d.ack('owner',rid,read['receipt']['content_sha256'],'manual:confirmed')
        self.assertEqual(self.d.observe_receipt('owner',rid,'observer')['state'],'acknowledged_parent')
    def test_22_original_stop_cause_is_preserved(self):
        a=self.broker();self.d.receipt(a,'blocked','user_stop');self.d.receipt(a,'blocked','configuration_error')
        self.assertEqual(json.loads((self.root/'control/state.json').read_bytes())['blocked'],'user_stop')

if __name__=='__main__':
    unittest.main()
