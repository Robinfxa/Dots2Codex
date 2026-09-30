"""Independent loopback tests: synthetic worker, no external/native calls."""
import concurrent.futures
import http.client
import json
import socket
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve()
REPO = next(p for p in HERE.parents if (p / 'remote_transport' / 'session.py').is_file())
sys.path.insert(0, str(REPO))
from remote_transport import deployment, Journal, Controller, Worker, LocalFSBackend, ProtocolError
from remote_transport.facade import RemoteResponsesFacade
from remote_transport.model import canonical


def request(text, prior=None):
    items=[] if prior is None else [prior]
    items.append({'type':'message','role':'user','content':[{'type':'input_text','text':text}]})
    return {'model':'native-subagent-bridge','stream':True,'input':items,'tools':[]}


class IndependentFacadeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.pin=deployment('facade_audit','synthetic/native/task')
        self.backend=LocalFSBackend(self.root/'objects',create=True)
        self.controller=Controller(Journal.provision(self.root/'controller',self.pin,'controller'),self.backend)
        self.worker=Worker(Journal.provision(self.root/'worker',self.pin,'worker'),self.backend)
        self.controller.publish_deployment()
        self.facade=RemoteResponsesFacade(self.controller,request_deadline=2).start()
        self.headers={'Content-Type':'application/json','session-id':'client-one','thread-id':str(uuid.uuid4())}
    def tearDown(self):
        self.facade.close()
        # HTTP handlers are daemon threads; wait for a disconnected request's
        # final local bookkeeping before deleting its test journal directory.
        self.assertTrue(self.facade.server.inflight.acquire(timeout=3))
        self.facade.server.inflight.release()
        self.tmp.cleanup()
    def post(self, body, headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.facade.server.server_port,timeout=5)
        conn.request('POST','/v1/responses',body=canonical(body),headers=headers or self.headers)
        response=conn.getresponse();out=(response.status,response.read());conn.close();return out
    def turn(self, body, answer):
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future=pool.submit(self.post,body)
            permit=self.worker.poll(self.worker.start_next,attempts=8,initial_delay=.02,max_delay=.1)
            self.assertIsNotNone(permit)
            self.worker.complete(permit,answer)
            status,data=future.result()
        self.assertEqual(status,200,data)
        # Client bytes may arrive before server-side delivery bookkeeping finishes.
        self.assertTrue(self.facade.server.inflight.acquire(timeout=2))
        self.facade.server.inflight.release()
        return json.loads(data.split(b'data: ')[1].split(b'\n')[0])['item']
    def test_socket_flush_does_not_create_receipt(self):
        self.turn(request('one'),'answer')
        with self.controller.journal.locked() as s:
            self.assertFalse(s['deliveries'])
        self.assertEqual(next(iter(self.facade.read_state()['jobs'].values()))['status'],'socket_flushed')
    def test_identical_http_retry_has_no_second_native_permit(self):
        body=request('one');self.turn(body,'answer')
        status,data=self.post(body)
        self.assertEqual(status,409);self.assertIn(b'request_replay',data)
        self.assertIsNone(self.worker.start_next())
    def test_changed_client_cannot_confirm_with_known_prior_output(self):
        item=self.turn(request('one'),'answer')
        headers=dict(self.headers,**{'thread-id':str(uuid.uuid4())})
        status,data=self.post(request('two',item),headers)
        self.assertEqual(status,409);self.assertIn(b'session_scope_mismatch',data)
        with self.controller.journal.locked() as s:
            self.assertFalse(s['deliveries']);self.assertEqual(len(s['requests']),1)
    def test_binding_loss_does_not_allow_another_client(self):
        self.turn(request('one'),'answer')
        path=self.root/'controller'/'remote-facade-state.json'
        state=json.loads(path.read_text());state['binding']=None;path.write_text(json.dumps(state))
        with self.assertRaises(ProtocolError):
            self.facade.read_state()
    def test_failed_bind_does_not_leak_lifetime_lock(self):
        self.facade.close()
        occupied=socket.socket();occupied.bind(('127.0.0.1',0));occupied.listen()
        try:
            with self.assertRaises(OSError):
                RemoteResponsesFacade(self.controller,port=occupied.getsockname()[1])
            replacement=RemoteResponsesFacade(self.controller)
            replacement.close()
        finally:
            occupied.close()
    def test_missing_facade_state_after_request_cannot_reinitialize(self):
        self.turn(request('one'),'answer');self.facade.close()
        (self.root/'controller'/'remote-facade-state.json').unlink()
        with self.assertRaisesRegex(ProtocolError,'facade_state_missing_after_requests'):
            RemoteResponsesFacade(self.controller)
    def test_unknown_wire_input_cannot_dispatch(self):
        body=request('one');body['input'].append({'type':'function_call_output','call_id':'unrequested','output':'x'})
        status,data=self.post(body)
        self.assertEqual(status,400);self.assertIn(b'text_only_input_required',data)
        self.assertIsNone(self.worker.start_next())
    def test_client_disconnect_never_confirms_or_replays(self):
        body=request('one');raw=canonical(body)
        sock=socket.create_connection(('127.0.0.1',self.facade.server.server_port),timeout=5)
        headers=dict(self.headers,Host='127.0.0.1:'+str(self.facade.server.server_port),**{'Content-Length':str(len(raw))})
        head='POST /v1/responses HTTP/1.1\r\n'+''.join(k+': '+v+'\r\n' for k,v in headers.items())+'\r\n'
        sock.sendall(head.encode()+raw)
        permit=self.worker.poll(self.worker.start_next,attempts=8,initial_delay=.02,max_delay=.1)
        self.assertIsNotNone(permit)
        sock.shutdown(socket.SHUT_RDWR);sock.close()
        self.worker.complete(permit,'answer after disconnect')
        # A follow-up must not manufacture delivery evidence for the lost response.
        status,data=self.post(request('two'))
        self.assertIn(status,(400,409))
        with self.controller.journal.locked() as state:
            self.assertFalse(state['deliveries']);self.assertEqual(len(state['requests']),1)
        self.assertIsNone(self.worker.start_next())
    def test_explicit_ack_holds_request_exclusion(self):
        self.turn(request('one'),'answer')
        rid=next(iter(self.facade.read_state()['jobs']))
        original=self.controller.record_delivery
        def checked(*args):
            self.assertTrue(self.facade.server.inflight.locked())
            return original(*args)
        with patch.object(self.controller,'record_delivery',side_effect=checked):
            self.facade.confirm_delivery(rid,'synthetic client parsed response')
        self.assertFalse(self.facade.server.inflight.locked())
    def test_exact_full_history_can_confirm_only_prior_turn(self):
        first=self.turn(request('one'),'answer1')
        self.turn(request('two',first),'answer2')
        with self.controller.journal.locked() as s:
            self.assertEqual(len(s['deliveries']),1)
            self.assertEqual(len(s['requests']),2)


if __name__=='__main__':unittest.main(verbosity=2)
