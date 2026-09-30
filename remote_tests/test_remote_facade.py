import concurrent.futures
import http.client
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from remote_transport import *
from remote_transport.facade import RemoteResponsesFacade
from remote_transport.model import canonical
from fakes import FakeDrive


def request(text,previous=None):
    inputs=[] if previous is None else [previous]
    inputs.append({'type':'message','role':'user','content':[{'type':'input_text','text':text}]})
    return {'model':'native-subagent-bridge','stream':True,'input':inputs,'tools':[]}


class RemoteFacadeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.pin=deployment('http-session','synthetic/native-task')
        self.api=FakeDrive();self.backend=GoogleDriveBackend(self.api,'folder')
        self.controller=Controller(Journal.provision(self.root/'controller',self.pin,'controller'),self.backend)
        self.worker=Worker(Journal.provision(self.root/'worker',self.pin,'worker'),self.backend)
        self.controller.publish_deployment()
        self.facade=RemoteResponsesFacade(self.controller,request_deadline=3).start()
        self.headers={'Content-Type':'application/json','session-id':'client-session','thread-id':str(uuid.uuid4())}
    def tearDown(self):self.facade.close();self.tmp.cleanup()

    def post(self,body,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.facade.server.server_port,timeout=6)
        conn.request('POST','/v1/responses',body=canonical(body),headers=headers or self.headers)
        resp=conn.getresponse();data=resp.read();conn.close()
        return resp.status,data

    def run_worker(self,answer):
        permit=self.worker.poll(self.worker.start_next,attempts=8,initial_delay=.02,max_delay=.1)
        self.assertIsNotNone(permit)
        self.assertEqual(permit['request']['scope'],'text_only')
        self.assertEqual(permit['request']['responses_request']['model'],'native-subagent-bridge')
        return self.worker.complete(permit,answer)

    def turn(self,body,answer):
        with concurrent.futures.ThreadPoolExecutor() as pool:
            fut=pool.submit(self.post,body)
            self.run_worker(answer)
            status,data=fut.result()
        self.assertEqual(status,200,data)
        item=json.loads(data.split(b'data: ')[1].split(b'\n')[0])['item']
        self.assertEqual(item['content'][0]['text'],answer)
        return item

    def test_two_http_turns_one_pinned_worker(self):
        first=self.turn(request('first'),'one')
        with self.controller.journal.locked() as state:self.assertFalse(state['deliveries'])
        self.turn(request('second',first),'two')
        with self.controller.journal.locked() as state:self.assertEqual(len(state['deliveries']),1)
        state=self.facade.read_state()
        last=[rid for rid,j in state['jobs'].items() if j['status']!='confirmed'][0]
        self.facade.confirm_delivery(last,'synthetic test client parsed full SSE body')
        with self.controller.journal.locked() as state:self.assertEqual(len(state['deliveries']),2)
        self.assertEqual(len([v for v in self.api.files.values() if Object.parse(v['raw']).body['kind']=='started']),2)

    def test_followup_without_exact_output_blocks(self):
        self.turn(request('first'),'one')
        status,data=self.post(request('second'))
        self.assertEqual(status,400);self.assertIn(b'previous_delivery_unconfirmed',data)
        with self.controller.journal.locked() as state:self.assertEqual(len(state['requests']),1)

    def test_replay_does_not_spawn(self):
        body=request('first');self.turn(body,'one')
        status,data=self.post(body)
        self.assertEqual(status,409);self.assertIn(b'request_replay',data)

    def test_canonical_identity_and_local_headers(self):
        status,_=self.post(request('first'),{'Content-Type':'application/json'})
        self.assertEqual(status,400)
        status,_=self.post(request('first'),dict(self.headers,Origin='https://example.com'))
        self.assertEqual(status,403)

    def test_tool_input_rejected(self):
        body=request('first');body['input'].append({'type':'function_call_output','call_id':'x','output':'y'})
        status,data=self.post(body)
        self.assertEqual(status,400);self.assertIn(b'text_only_input_required',data)
        with self.controller.journal.locked() as state:self.assertFalse(state['requests'])

    def test_single_running_facade(self):
        with self.assertRaisesRegex(ProtocolError,'facade_already_running'):
            RemoteResponsesFacade(self.controller)

    def test_native_task_stays_pinned(self):
        self.turn(request('first'),'one')
        new_headers=dict(self.headers,**{'session-id':'different'})
        status,data=self.post(request('second'),new_headers)
        self.assertEqual(status,409);self.assertIn(b'session_scope_mismatch',data)

if __name__=='__main__':unittest.main()
