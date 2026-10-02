"""Independent actual core+gateway offline integration, two arbitrary projects."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import http.client
import threading
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parent
REPO = Path(os.environ["LITE_REPO"]) if "LITE_REPO" in os.environ else next(parent for parent in ROOT.parents if (parent / "dots_lite").is_dir())
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(ROOT))
from dots_lite import docs, protocol as p
from dots_lite.gateway import MacGateway, ResponsesServer
from dots_lite.storage import private_write
from dots_lite.worker import ParentController, Worker
from test_worker_audit import StrictDocs, KEY
from test_wire_audit import request, response, function_call, custom_call


class Google:
    def __init__(self):
        self.port=StrictDocs(); self.files={}; self.counts={'get_document':0,'batch_update':0,
            'create_document':0,'upload':0,'metadata':0,'raw_get':0}; self.n=0
    def add(self,name): self.port.add(name)
    def get_document(self,document_id,*,deadline=None):
        self.counts['get_document']+=1; return self.port.read(document_id)
    def batch_update_document(self,document_id,requests,write_control,*,deadline=None):
        self.counts['batch_update']+=1
        return self.port.apply({'document_id':document_id,'body':{'requests':requests,'writeControl':write_control}})
    def create_document_once(self,folder,name,*,deadline=None):
        self.counts['create_document']+=1; self.n+=1; fid='doc-'+str(self.n);self.add(fid);return fid
    def create_bytes(self,folder,name,raw,file_id=None,*,deadline=None):
        self.counts['upload']+=1;self.n+=1;fid='blob-'+str(self.n);self.files[fid]=(folder,name,bytes(raw));return fid
    def get_metadata(self,file_id,*,deadline=None):
        self.counts['metadata']+=1;folder,name,raw=self.files[file_id]
        return {'id':file_id,'parents':[folder],'name':name,'mimeType':'application/json','trashed':False,'size':str(len(raw))}
    def get_bytes(self,file_id,limit,*,deadline=None):
        self.counts['raw_get']+=1;raw=self.files[file_id][2];assert len(raw)<=limit;return raw


class GatewayAudit(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        now=int(time.time());self.grant={'protocol':p.PROTOCOL,'activation_id':'activation-a','folder_id':'folder-a',
            'inbox_id':'inbox-a','created_at':now-60,'expires_at':now+600,
            'allowed_pairs':[{'model':'gpt-6.1-sol','reasoning_effort':'xhigh'}],
            'limits':dict(p.DEFAULT_LIMITS),'package_sha256':'a'*64}
        self.google=Google();self.google.add('inbox-a')
        self.gateway=MacGateway(self.root/'gateway',self.grant,KEY,self.google,self.google,create=True,poll_seconds=.01)
        self.addCleanup(self.gateway.close);self.gateway.initialize();self.workers={};self.native_calls=0;self.exposures={}
    def identity(self,project): return {'session-id':'arbitrary-session-'+project,'thread-id':'arbitrary-thread-'+project}
    def submit(self,project,body,key): return self.gateway.submit(self.identity(project),p.canonical(body),key)
    def inbox(self): return p.parse_inbox(docs.snapshot(self.google.get_document('inbox-a'),'inbox-a'),self.grant,KEY)
    def drive_worker(self,ticket,output,*,lose_begin=False):
        inbox=self.inbox();route=next(r for r in inbox['routes'] if r['route_id']==ticket['route_id'])
        rid=route['route_id']
        if rid not in self.workers:
            parent=ParentController.create(self.root/('worker-'+rid),self.grant,KEY,route,'/root',
                docs.snapshot(self.google.get_document(route['outbox_id']),route['outbox_id'],max_bytes=p.OUTBOX_MAX_BYTES))
            args={'task_name':'serve_'+rid,'message':'synthetic admission, no request plaintext',
                  'fork_turns':'none','model':route['model'],'reasoning_effort':route['reasoning_effort']}
            reserved=parent.reserve_spawn(args);parent.accept_spawn_reserved_and_issue(self.google.port.apply(reserved));self.native_calls+=1
            actual={'task_name':'/root/serve_'+rid,'agent_id':'synthetic-'+rid}
            plan=parent.record_actual_admission(args,actual);handoff=parent.accept_admission(self.google.port.apply(plan))
            worker=Worker(self.root/('worker-'+rid),KEY,actual['task_name']);worker.takeover(handoff);self.workers[rid]=worker
        worker=self.workers[rid];desc=route['request']
        # Actual immutable bytes from the synthetic Drive port cross into worker.
        meta=self.google.get_metadata(desc['file_id']);raw=self.google.get_bytes(desc['file_id'],self.grant['limits']['max_request_bytes'])
        local=self.root/(ticket['request_id']+'.input.json');private_write(local,raw)
        plan=worker.prepare_begin(inbox,local,metadata=meta);ack=self.google.port.apply(plan,lost=lose_begin)
        result=worker.accept_begin_and_expose(ack)
        if lose_begin:return result
        self.exposures[rid]=self.exposures.get(rid,0)+1
        self.assertEqual(result,p.strict_json(raw))
        artifact=worker.save_actual_result(ticket['request_id'],output);worker.record_upload_attempt()
        fid=self.google.create_bytes('folder-a','result.json',Path(artifact['path']).read_bytes())
        plan=worker.publish_result({'file_id':fid,'folder_id':'folder-a','byte_length':artifact['byte_length']})
        worker.accept_result(self.google.port.apply(plan));return artifact

    def test_two_arbitrary_projects_have_separate_docs_children_and_histories(self):
        a=self.submit('/project/alpha',request('project alpha'),'a-1');b=self.submit('/elsewhere/beta',request('project beta'),'b-1')
        self.assertNotEqual(a['route_id'],b['route_id']);self.assertEqual(len(self.google.port.documents),3)
        self.drive_worker(a,response(function_call(),'resp-a'))
        self.drive_worker(b,response(custom_call(),'resp-b'))
        self.assertEqual(self.gateway.poll(a)['id'],'resp-a');self.assertEqual(self.gateway.poll(b)['id'],'resp-b')
        self.assertEqual(self.native_calls,2);self.assertEqual(set(self.exposures.values()),{1})
        self.assertEqual(len(self.gateway.journal.read()['routes']),2)

    def test_duplicate_request_key_reuses_request_and_rejects_changed_bytes(self):
        body=request();a=self.submit('a',body,'same-key');counts=copy.deepcopy(self.google.counts)
        self.assertEqual(self.submit('a',body,'same-key'),a);self.assertEqual(self.google.counts,counts)
        with self.assertRaisesRegex(p.ProtocolError,'idempotency_key_payload_conflict'):
            self.submit('a',request('changed'),'same-key')

    def test_full_function_then_custom_loop_preserves_ids_and_one_tool_emission(self):
        body=request();a=self.submit('a',body,'a-1');first=response(function_call(),'resp-a')
        self.drive_worker(a,first);self.assertEqual(self.gateway.poll(a),first)
        frames=self.gateway.delivery(a);self.assertIn(b'call_synthetic_a',frames)
        with self.assertRaisesRegex(p.ProtocolError,'tool_delivery_unknown'):self.gateway.delivery(a)
        continued=copy.deepcopy(body);continued['input'] += [function_call(),{'type':'function_call_output','call_id':'call_synthetic_a','output':'file bytes'}]
        b=self.submit('a',continued,'a-2');second=response(custom_call(),'resp-b')
        self.drive_worker(b,second);self.assertEqual(self.gateway.poll(b),second);self.gateway.delivery(b)
        self.assertEqual(self.native_calls,1);self.assertEqual(self.exposures[a['route_id']],2)
        self.assertEqual(self.workers[a['route_id']].state['history'][0]['request_id'],a['request_id'])

    def test_unknown_route_does_not_block_second_project(self):
        a=self.submit('a',request('a'),'a-1');b=self.submit('b',request('b'),'b-1')
        self.assertEqual(self.drive_worker(a,response(function_call()),lose_begin=True)['status'],'unknown')
        self.drive_worker(b,response(custom_call()));self.assertIsNotNone(self.gateway.poll(b));self.gateway.delivery(b)
        self.assertIsNone(self.gateway.poll(a))

    def test_verified_result_survives_gateway_restart_and_tool_delivery_fence(self):
        a=self.submit('a',request(),'a-1');out=response(function_call());self.drive_worker(a,out)
        self.gateway.poll(a);self.gateway.delivery(a);self.gateway.close()
        restored=MacGateway(self.root/'gateway',self.grant,KEY,self.google,self.google)
        self.addCleanup(restored.close);self.assertEqual(restored.poll(a),out)
        with self.assertRaisesRegex(p.ProtocolError,'tool_delivery_unknown'):restored.delivery(a)
        self.assertEqual(self.native_calls,1)

    def test_route_capacity_is_explicit(self):
        for i in range(3):self.submit(str(i),request(str(i)),'key-'+str(i))
        with self.assertRaisesRegex(p.ProtocolError,'route_capacity'):self.submit('fourth',request('4'),'key-4')
        self.assertEqual(len(self.google.port.documents),4)

    def test_hundred_turn_history_keeps_control_and_active_cache_bounded(self):
        body=request('Bounded synthetic long session')
        for index in range(100):
            ticket=self.submit('long-session',body,'turn-'+str(index))
            item={'type':'message','id':'msg-'+str(index),'role':'assistant',
                  'content':[{'type':'output_text','text':'Synthetic answer '+str(index)}]}
            out=response(item,'resp-'+str(index))
            self.drive_worker(ticket,out);self.assertEqual(self.gateway.poll(ticket),out);self.gateway.delivery(ticket)
            body['input'] += [item,{'role':'user','content':[{'type':'input_text','text':'Next '+str(index)}]}]
            cache=list(self.gateway.journal.directory.glob('*.request.json'))+list(self.gateway.journal.directory.glob('*.result.json'))
            self.assertLessEqual(len(cache),2)
            worker=self.workers[ticket['route_id']]
            self.assertLessEqual(len(list(worker.journal.directory.glob('input-*.json'))),1)
            self.assertLessEqual(len(list(worker.journal.directory.glob('result-*.json'))),1)
            self.assertLessEqual((worker.journal.directory/'journal.json').stat().st_size,1024*1024)
        self.assertEqual(self.native_calls,1);self.assertEqual(self.exposures[ticket['route_id']],100)
        for docid,document in self.google.port.documents.items():
            value=''.join(r['textRun']['content'] for e in document['tabs'][0]['documentTab']['body']['content'][1:] for r in e['paragraph']['elements'])
            self.assertLessEqual(len(value.encode()),(p.INBOX_MAX_BYTES if docid=='inbox-a' else p.OUTBOX_MAX_BYTES)+1)

    def test_real_loopback_http_delivers_exact_function_sse(self):
        server=ResponsesServer(self.gateway);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def send():
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            try:
                connection.request('POST','/activations/activation-a/v1/responses',body=p.canonical(request()),
                    headers={'Content-Type':'application/json','session-id':'http-session','thread-id':'http-thread','Idempotency-Key':'http-key'})
                result=connection.getresponse();return result.status,result.getheader('Content-Type'),result.getheader('X-Request-ID'),result.read()
            finally:connection.close()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(send);deadline=time.monotonic()+3;ticket=None
                while time.monotonic()<deadline:
                    with self.gateway._lock:
                        jobs=self.gateway.journal.read()['requests']
                        if jobs:ticket=self.gateway._ticket(next(iter(jobs.values())))
                    if ticket:break
                    time.sleep(.01)
                self.assertIsNotNone(ticket)
                out=response(function_call(),'http-response');self.drive_worker(ticket,out)
                status,kind,request_id,frames=future.result(timeout=5)
                self.assertEqual((status,kind,request_id),(200,'text/event-stream',ticket['request_id']))
                events=[json.loads(line[6:]) for line in frames.splitlines() if line.startswith(b'data: ')]
                self.assertEqual(events[-1]['type'],'response.completed');self.assertEqual(events[-1]['response'],out)
                self.assertTrue(self.gateway.journal.read()['requests'][ticket['request_id']]['delivery_started'])
        finally:server.shutdown();server.server_close();thread.join(timeout=3)


if __name__=='__main__':unittest.main(verbosity=2)
