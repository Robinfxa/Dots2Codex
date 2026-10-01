"""Authenticated queue → one native plan/receipt → unchanged child JOIN, offline."""
import concurrent.futures
import copy
import json
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from remote_transport import global_control as q, router_bootstrap as child
from remote_transport.global_native import NativeLedger
from remote_transport.global_google import GoogleQueueBridge, mirror_verified_queue
from remote_transport.global_gateway import Store, Gateway, private_dir, private_write, strict_json
from remote_transport.global_fixture import unused_fixture_port, identity, request, post, events
from remote_transport.model import ProtocolError, Object, canonical, hash_bytes
from remote_transport.selection import select, load_catalog
from remote_transport.router_join import _source_hashes
from remote_transport.backend import Capabilities, GoogleDriveBackend
from remote_transport.control import GoogleDocsCASControlStore, SessionCoordinator, CASConflict
from remote_transport.controlled import CASWorker
from remote_transport.session import Journal


class FakeGoogle:
    capabilities=Capabilities(create_by_id=True,complete_listing=True,direct_metadata_read=True)
    def __init__(self):self.docs={};self.files={};self.counter=0;self.lock=threading.RLock();self.lose=False;self.fail_create=False;self.calls=[]
    def generate_id(self):
        with self.lock:self.counter+=1;return 'file'+str(self.counter)
    def create_document_once(self,folder,title):
        with self.lock:
            self.calls.append(('create_doc',folder,title))
            if self.fail_create:raise TimeoutError('unknown create')
            did=self.generate_id();self.docs[did]=['\n',1];return did
    def get_document(self,document_id):
        with self.lock:
            text,revision=self.docs[document_id]
            return {'documentId':document_id,'revisionId':'r'+str(revision),'suggestionsViewMode':'SUGGESTIONS_INLINE',
                    'tabs':[{'tabProperties':{'tabId':'t.0'},'documentTab':{'body':{'content':[
                        {'sectionBreak':{}},{'paragraph':{'elements':[{'textRun':{'content':text}}]}}]}}}]}
    def batch_update_document(self,document_id,requests,write_control):
        with self.lock:
            current=self.docs[document_id]
            if write_control!={'requiredRevisionId':'r'+str(current[1])}:raise CASConflict('revision conflict')
            self.calls.append(('cas',document_id,copy.deepcopy(requests)))
            action=requests[0]
            if 'insertText' in action:
                assert current[0]=='\n';current[0]=action['insertText']['text']+'\n';reply={}
            else:
                r=action['replaceAllText'];old=r['containsText']['text'];count=current[0].count(old)
                current[0]=current[0].replace(old,r['replaceText']);reply={'replaceAllText':{'occurrencesChanged':count}}
            current[1]+=1
            if self.lose:self.lose=False;raise TimeoutError('successful write response lost')
            return {'documentId':document_id,'replies':[reply],'writeControl':{'requiredRevisionId':'r'+str(current[1])}}
    def create_bytes(self,folder,name,raw,file_id):
        with self.lock:
            if file_id in self.files:
                from remote_transport.backend import AlreadyExists
                raise AlreadyExists()
            self.files[file_id]={'id':file_id,'name':name,'parents':[folder],'trashed':False,'raw':raw};return file_id
    def get_bytes(self,file_id,limit):
        with self.lock:
            value=self.files[file_id]['raw'];assert len(value)<=limit;return value
    def get_metadata(self,file_id):
        with self.lock:return {k:v for k,v in self.files[file_id].items() if k!='raw'}


class GlobalControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.package=Path(__file__).resolve().parents[1]
        self.store,self.gen=Store.initialize(self.root/'gateway',select(load_catalog(),'gpt-6.1-sol','high'),port=unused_fixture_port())
        self.gateway=Gateway(self.store).start();self.google=FakeGoogle();self.code=secrets.token_hex(32)
        did=self.google.create_document_once('folder','authorized control');now=int(time.time())-2
        self.initial=q.initial(activation_id=self.gen,queue_id=secrets.token_hex(16),folder_id='folder',document_id=did,
            tab_id='t.0',join_code=self.code,created=now,expires=now+1800,runtime_source_hashes=_source_hashes())
        self.bridge=GoogleQueueBridge(self.store,self.root/'bridge',self.google,self.google,self.initial,self.code)
        self.bridge.initialize_blank_queue()
        self.ledger=NativeLedger(self.root/'native',self.initial,self.code,'/root/fixture_global_router');self.serial=0
        self.execute('join',capacity=2,seconds=1200,now=now+1)
        self.execute('heartbeat',now=now+2);self.bridge.sync_heartbeat()
    def tearDown(self):self.bridge.close();self.gateway.close();self.tmp.cleanup()
    def error(self,pattern,fn,*args,**kwargs):
        with self.assertRaisesRegex(ProtocolError,pattern):fn(*args,**kwargs)
    def path(self,suffix):
        self.serial+=1;return self.root/'native'/f'{self.serial}-{suffix}'
    def execute(self,kind,route_id=None,**kwargs):
        plan=self.path('cas.json');value=self.ledger.plan_event(self.bridge.read(),kind,plan,route_id=route_id,**kwargs)
        response=self.google.batch_update_document(**value['tool_arguments'])
        self.ledger.verify_plan(plan,response,self.google.get_document(self.initial['document_id']));return value
    def demand(self,body=None,client=None):
        body=body or request('fixture');client=client or identity()
        status,raw=post(self.store,self.gen,client,body);self.assertEqual(status,409,raw)
        from remote_transport.global_gateway import route_key
        rid=route_key(self.gen,client);self.bridge.prepare_child(rid);return rid,body,client
    def native(self,rid):
        self.execute('claim',rid);self.execute('begin',rid)
        plan=self.path('spawn.json');out=self.ledger.plan_spawn(self.bridge.read(),rid,plan,self.package)
        args=out['arguments'];result={'task_name':'/root/offline/'+args['task_name']}
        receipt_path=self.path('receipt.json');self.ledger.record_spawn(plan,args,result,receipt_path)
        self.execute('admitted',rid);return strict_json(receipt_path.read_bytes())
    def child_admit(self,rid,receipt):
        d=self.bridge.read().state['logical']['demands'][rid];state=d['child_bootstrap'];cc=q.child_code(self.code,self.gen,rid)
        raw=canonical({'contract':'dots-router-probe/1','bootstrap_id':state['bootstrap_id'],'native_task_id':receipt['native_task_id']})
        fid=self.google.generate_id();name='dots2codex-router-probe-'+state['bootstrap_id']+'.json';self.google.create_bytes('folder',name,raw,fid)
        admitted=child.worker_admitted(state,join_code=cc,native_task_id=receipt['native_task_id'],
                                       probe={'file_id':fid,'name':name,'sha256':hash_bytes(raw)},admission=receipt)
        source=child.snapshot_from_document(self.google.get_document(state['bootstrap_document_id']),state['bootstrap_document_id'],'t.0')
        packet=child.plan(source,admitted,join_code=cc)
        self.google.batch_update_document(**packet['tool_arguments'])
        self.assertEqual(self.bridge.advance_child(rid)['state'],'await_child_polling')
        bundle=child.snapshot_from_document(self.google.get_document(state['bootstrap_document_id']),state['bootstrap_document_id'],'t.0').state
        extracted=child.extract_bundle(bundle,join_code=cc,native_task_id=receipt['native_task_id'])
        # Child consumes the exact existing v3 bundle and runs the actual CAS worker,
        # with a synthetic model result only. No real native tool is called.
        pin=Object.parse(extracted[0]);config=json.loads(extracted[1])
        backend=GoogleDriveBackend(self.google,'folder',discovery='control_refs')
        control=GoogleDocsCASControlStore(self.google,config['document_id'],config['tab_id'],config['control_id'],rid,config['writer_identity'])
        worker=CASWorker(Journal.provision(self.root/('worker-'+rid),pin,'worker'),backend,SessionCoordinator(control,backend))
        ready=child.worker_polling(bundle,join_code=cc,native_task_id=receipt['native_task_id'],runtime_hash='a'*64)
        source=child.snapshot_from_document(self.google.get_document(state['bootstrap_document_id']),state['bootstrap_document_id'],'t.0')
        packet=child.plan(source,ready,join_code=cc);self.google.batch_update_document(**packet['tool_arguments'])
        self.assertEqual(self.bridge.advance_child(rid)['state'],'ready')
        return worker,pin
    def turn(self,rid,body,client,worker,result):
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future=pool.submit(post,self.store,self.gen,client,body)
            permit=worker.poll(worker.start_next,attempts=8,initial_delay=.02,max_delay=.1);self.assertIsNotNone(permit)
            worker.complete(permit,result);status,raw=future.result()
        self.assertEqual(status,200,raw);self.assertEqual(events(raw)[-1]['type'],'response.completed');return permit

    def test_join_heartbeat_claim_and_signed_projection(self):
        c=self.bridge.read().state['logical']['controller'];now=max(c['heartbeat_at']+1,int(time.time())+1)
        with patch('time.time',return_value=now):
            self.execute('heartbeat',now=now);self.bridge.sync_heartbeat()
            rid,_,_=self.demand();self.execute('claim',rid)
            state=self.bridge.read().state
        self.assertEqual(state['logical']['demands'][rid]['state'],'claimed')
        tampered=copy.deepcopy(state);tampered['logical']['controller']['capacity']=6
        self.error('projection',q.verify,tampered,self.code)
        self.error('join_code',q.verify,state,'0'*64)

    def test_two_queue_children_to_real_facades_isolated_end_to_end(self):
        first=self.demand(request('A'));second=self.demand(request('B','gpt-6-astra','max'))
        workers=[]
        for rid,_,_ in (first,second):workers.append(self.child_admit(rid,self.native(rid)))
        self.assertNotEqual(workers[0][1].oid,workers[1][1].oid)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            fs=[pool.submit(self.turn,*row,worker,'synthetic '+row[0]) for row,(worker,_) in zip((first,second),workers)]
            permits=[f.result() for f in fs]
        self.assertNotEqual(permits[0]['native_task_id'],permits[1]['native_task_id'])
        self.assertEqual(permits[0]['request']['responses_request']['input'][-1]['content'][0]['text'],'A')
        self.assertEqual(permits[1]['request']['responses_request']['input'][-1]['content'][0]['text'],'B')
        self.assertEqual(self.store.status()['controller_mode'],'native_google_v2');self.assertFalse(self.store.status()['production_ready'])

    def test_ready_route_transport_restart_keeps_native_pin_and_delivery_history(self):
        rid,body,client=self.demand();worker,pin=self.child_admit(rid,self.native(rid))
        self.turn(rid,body,client,worker,'first response')
        status,raw=post(self.store,self.gen,client,body);self.assertEqual(status,200)
        item=next(e['item'] for e in events(raw) if e['type']=='response.output_item.done')
        before=self.store.route(rid);queue_epoch=self.bridge.read().state['epoch']
        self.bridge.close()
        self.bridge=GoogleQueueBridge(self.store,self.root/'bridge',self.google,self.google,self.initial,self.code)
        self.assertEqual(self.bridge.advance_child(rid)['state'],'ready')
        after=self.store.route(rid)
        self.assertEqual((after['pin'],after['native_task'],after['used']),(before['pin'],before['native_task'],before['used']))
        self.assertEqual(self.bridge.read().state['epoch'],queue_epoch)
        self.turn(rid,request('second',history=[item]),client,worker,'second response')
        with worker.journal.locked() as state:self.assertEqual(len(state['executions']),2)

    def test_explicit_stop_is_durable_and_closes_control_without_facade(self):
        rid,body,client=self.demand();worker,pin=self.child_admit(rid,self.native(rid))
        self.bridge.close_local_facades()
        result=self.bridge.stop();self.assertTrue(result['queue_closed']);self.assertTrue(result['controls'][rid]['authoritative'])
        self.assertFalse(result['native_children_stopped']);self.assertEqual(self.bridge.step()['state'],'closed')
        self.error('session_stopped',self.bridge.sync_heartbeat)
        self.assertIsNone(worker.start_next())

    def test_new_generation_requires_new_native_join_without_stalling_old_queue(self):
        new=self.store.activate(select(load_catalog(),'gpt-6-astra','max'))
        status,raw=post(self.store,new,identity(),request('new','gpt-6-astra','max'))
        self.assertEqual(status,409);self.assertIn(b'activation_requires_new_native_join',raw)
        self.assertEqual(self.bridge.step()['state'],'controller_active')

    def test_controller_mode_status_and_bridge_lease(self):
        self.assertEqual(self.store.status()['controller_mode'],'native_google_v2')
        self.error('already_running',GoogleQueueBridge,self.store,self.root/'bridge',self.google,self.google,self.initial,self.code)

    def test_native_plan_is_one_attempt_across_helper_restart(self):
        rid,_,_=self.demand();self.execute('claim',rid);self.execute('begin',rid)
        self.ledger.plan_spawn(self.bridge.read(),rid,self.path('spawn.json'),self.package)
        restart=NativeLedger(self.root/'native',self.bridge.read().state,self.code,'/root/fixture_global_router')
        self.error('already_reserved_no_replay',restart.plan_spawn,self.bridge.read(),rid,self.path('again.json'),self.package)
        self.execute('unknown',rid);self.assertEqual(self.bridge.read().state['logical']['demands'][rid]['state'],'unknown')

    def test_stale_controller_and_closed_queue_reject_spawn_exposure(self):
        rid,_,_=self.demand();self.execute('claim',rid);self.execute('begin',rid);source=self.bridge.read()
        c=source.state['logical']['controller']
        with patch('time.time',return_value=c['heartbeat_at']+181):
            self.error('not_active',self.ledger.plan_spawn,source,rid,self.path('stale.json'),self.package)
        self.bridge.event('close',{'confirm':True})
        self.error('queue_closed',self.ledger.plan_spawn,self.bridge.read(),rid,self.path('closed.json'),self.package)

    def test_expired_child_bootstrap_rejects_spawn_even_with_fresh_heartbeat(self):
        self.bridge.bootstrap_seconds=60;rid,_,_=self.demand();self.execute('claim',rid);self.execute('begin',rid)
        source=self.bridge.read();expires=source.state['logical']['demands'][rid]['child_bootstrap']['expires']
        with patch('time.time',return_value=expires-31):self.execute('heartbeat',now=expires-31)
        with patch('time.time',return_value=expires-1):self.execute('heartbeat',now=expires-1)
        with patch('time.time',return_value=expires+1):
            self.error('bootstrap_expired',self.ledger.plan_spawn,self.bridge.read(),rid,self.path('expired.json'),self.package)

    def test_unknown_cas_reconciles_exact_event_without_rewrite(self):
        rid,_,_=self.demand();path=self.path('claim.json');out=self.ledger.plan_event(self.bridge.read(),'claim',path,route_id=rid)
        self.google.lose=True
        with self.assertRaises(TimeoutError):self.google.batch_update_document(**out['tool_arguments'])
        calls=len(self.google.calls);value=self.ledger.verify_plan(path,None,self.google.get_document(self.initial['document_id']))
        self.assertTrue(value['reconciled_from_event']);self.assertEqual(len(self.google.calls),calls)
        self.error('already_issued',self.ledger.plan_event,self.bridge.read(),'claim',self.path('retry.json'),route_id=rid)

    def test_actual_argument_mismatch_cannot_publish_admission(self):
        rid,_,_=self.demand();self.execute('claim',rid);self.execute('begin',rid)
        path=self.path('spawn.json');out=self.ledger.plan_spawn(self.bridge.read(),rid,path,self.package)
        changed={**out['arguments'],'model':'gpt-6-astra'}
        self.error('arguments_mismatch',self.ledger.record_spawn,path,changed,{'task_name':'/root/x'},self.path('receipt.json'))
        self.error('recorded_native_result_required',self.ledger.plan_event,self.bridge.read(),'admitted',self.path('admit.json'),route_id=rid)

    def test_unknown_document_create_not_retried(self):
        body=request('x');client=identity();post(self.store,self.gen,client,body)
        from remote_transport.global_gateway import route_key
        rid=route_key(self.gen,client);self.google.fail_create=True
        with self.assertRaisesRegex(RuntimeError,'create_unknown'):self.bridge.prepare_child(rid)
        before=len(self.google.calls);self.error('runtime_exists',self.bridge.prepare_child,rid)
        self.assertEqual(len(self.google.calls),before)

    def test_queue_replay_and_source_tampering_rejected(self):
        old=self.bridge.read();self.demand();self.ledger.inspect(self.bridge.read())
        self.error('rollback_or_fork',self.ledger.inspect,old)
        state=self.bridge.read().state;changed=copy.deepcopy(state);changed['runtime_source_hashes']['native_connector/runner.js']='0'*64
        self.error('source_binding',q.verify,changed,self.code)

if __name__=='__main__':unittest.main()
