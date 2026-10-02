import copy
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from dots_lite.gateway import MacGateway, ResponsesServer
from dots_lite.protocol import canonical, ProtocolError, parse_inbox
from dots_lite.wire import validate_request, validate_response
from lite_tests.gateway_fixtures import KEY, FakeGoogle, grant, request, response


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.provider=FakeGoogle(flattened=True); self.grant=grant()
        self.gw=MacGateway(Path(self.tmp.name)/'state',self.grant,KEY,self.provider,self.provider,create=True,poll_seconds=.01,wait_seconds=.2)
        self.addCleanup(self.gw.close); self.gw.initialize()
        self.identity={'session-id':'project-A/session','thread-id':'thread-A'}
    def submit(self,body=None,key='key-1',identity=None):
        return self.gw.submit(identity or self.identity,canonical(body or request()),key)
    def test_healthy_no_self_readback_and_idempotent(self):
        ticket=self.submit(); duplicate=self.submit(); self.assertEqual(ticket,duplicate)
        self.assertEqual(len([c for c in self.provider.calls if c[0]=='get_document']),1)
        self.assertEqual(len([c for c in self.provider.calls if c[0]=='upload']),1)
        self.assertEqual(len([c for c in self.provider.calls if c[0]=='batch_update']),2)
        body=request(); body['input'][0]['content'][0]['text']='different'
        with self.assertRaisesRegex(ProtocolError,'idempotency_key_payload_conflict'): self.submit(body)
    def test_metadata_hash_bind_and_text_replay(self):
        ticket=self.submit(); output=response(ticket['request_id'])
        self.provider.publish_result(self.gw,ticket,output)
        self.assertEqual(self.gw.poll(ticket),output)
        before=len(self.provider.calls)
        frames=self.gw.delivery(ticket)
        self.assertEqual(self.gw.delivery(ticket),frames)
        self.assertEqual(len(self.provider.calls),before)
        self.assertIn(b'response.completed',frames)
    def test_function_custom_two_project_loop(self):
        a=self.submit(); b=self.submit(identity={'session-id':'/unrelated/project-B','thread-id':'thread-B'})
        self.assertNotEqual(a['route_id'],b['route_id'])
        outputs={}
        for ticket,kind in [(a,'function_call'),(b,'custom_tool_call')]:
            output=response(ticket['request_id'],kind); outputs[ticket['route_id']]=output
            self.provider.publish_result(self.gw,ticket,output); self.gw.poll(ticket); frames=self.gw.delivery(ticket)
            self.assertIn(output['output'][0]['call_id'].encode(),frames)
            with self.assertRaisesRegex(ProtocolError,'tool_delivery_unknown_do_not_replay'): self.gw.delivery(ticket)
        for ticket,identity in [(a,self.identity),(b,{'session-id':'/unrelated/project-B','thread-id':'thread-B'})]:
            item=outputs[ticket['route_id']]['output'][0]; body=request()
            body['input'] += [item, {'type':'function_call_output' if item['type']=='function_call' else 'custom_tool_call_output',
                'call_id':item['call_id'],'output':'success'}]
            next_ticket=self.submit(body,'key-2',identity)
            self.assertEqual(next_ticket['seq'],2)
            self.assertIsNotNone(self.gw.journal.read()['routes'][ticket['route_id']]['slot']['request']['previous_result_ack'])
    def test_cross_route_call_rejected(self):
        a=self.submit(); output=response(a['request_id'],'function_call'); self.provider.publish_result(self.gw,a,output)
        self.gw.poll(a); self.gw.delivery(a)
        body=request(); body['input'] += [output['output'][0],{'type':'function_call_output','call_id':output['output'][0]['call_id'],'output':'ok'}]
        with self.assertRaisesRegex(ProtocolError,'tool_history_does_not_match'): self.submit(body,identity={'session-id':'project-B','thread-id':'thread-B'})
    def test_unknown_write_exact_reconcile_no_rewrite(self):
        self.provider.lose_write_reply=True
        ticket=self.submit()
        self.assertEqual(ticket['state'],'published')
        self.assertEqual(len([c for c in self.provider.calls if c[0]=='batch_update']),2)
        self.assertEqual(len([c for c in self.provider.calls if c[0]=='get_document']),2)
    def test_unknown_absence_keeps_same_operation(self):
        self.provider.lose_write_reply=True; self.provider.hide_write=True
        with self.assertRaisesRegex(ProtocolError,'inbox_write_unknown'): self.submit()
        state=self.gw.journal.read(); op=state['write_intent']['operation_id']
        ticket=self.submit(); self.assertEqual(ticket['state'],'publish_unknown')
        with self.assertRaisesRegex(ProtocolError,'inbox_write_unknown'): self.gw.recover_request(ticket)
        self.assertEqual(self.gw.journal.read()['write_intent']['operation_id'],op)
        self.assertEqual(len([c for c in self.provider.calls if c[0]=='batch_update']),2)
    def test_hash_failure_does_not_invalidate_other_route(self):
        a=self.submit(); b=self.submit(identity={'session-id':'project-B','thread-id':'thread-B'})
        out=self.provider.publish_result(self.gw,a,response(a['request_id']))
        self.provider.blobs[out['result']['file_id']]=(self.grant['folder_id'],b'wrong')
        with self.assertRaises(ProtocolError):self.gw.poll(a)
        self.provider.publish_result(self.gw,b,response(b['request_id']))
        self.assertIsNotNone(self.gw.poll(b))
    def test_stop_blocks_new_keeps_result(self):
        ticket=self.submit(); self.gw.stop()
        with self.assertRaisesRegex(ProtocolError,'gateway_stopped'): self.submit(key='new')
        self.provider.publish_result(self.gw,ticket,response(ticket['request_id']))
        self.assertIsNotNone(self.gw.poll(ticket))
        self.assertFalse(self.gw.status()['native_interruption_confirmed'])
    def test_restart_preserves_tool_delivery_barrier(self):
        ticket=self.submit(); self.provider.publish_result(self.gw,ticket,response(ticket['request_id'],'function_call'))
        self.gw.poll(ticket); self.gw.delivery(ticket); self.gw.close()
        recovered=MacGateway(Path(self.tmp.name)/'state',self.grant,KEY,self.provider,self.provider)
        self.addCleanup(recovered.close); recovered.initialize()
        with self.assertRaisesRegex(ProtocolError,'tool_delivery_unknown_do_not_replay'):recovered.delivery(ticket)
    def test_capacity_and_no_same_route_overlap(self):
        self.submit()
        with self.assertRaisesRegex(ProtocolError,'route_request_inflight'):self.submit(key='second')
        self.submit(identity={'session-id':'B','thread-id':'B'})
        self.submit(identity={'session-id':'C','thread-id':'C'})
        with self.assertRaisesRegex(ProtocolError,'route_capacity_exhausted'):self.submit(identity={'session-id':'D','thread-id':'D'})
    def test_100_turn_payload_cache_is_current_only(self):
        body=request()
        for turn in range(100):
            ticket=self.submit(body,'key-'+str(turn))
            output=response(ticket['request_id']); self.provider.publish_result(self.gw,ticket,output)
            self.gw.poll(ticket); self.gw.delivery(ticket)
            body['input'] += output['output'] + [{'role':'user','content':[{'type':'input_text','text':'Continue'}]}]
            self.assertEqual(len(list(self.gw.journal.directory.glob('*.request.json'))),1)
            self.assertEqual(len(list(self.gw.journal.directory.glob('*.result.json'))),1)
        self.assertLess(len(self.provider.texts[self.grant['inbox_id']].encode()),16*1024)
        self.assertLess((self.gw.journal.directory/'journal.json').stat().st_size,1024*1024)
    def test_previous_assistant_text_must_be_preserved(self):
        first=self.submit(); self.provider.publish_result(self.gw,first,response(first['request_id']))
        self.gw.poll(first); self.gw.delivery(first)
        with self.assertRaisesRegex(ProtocolError,'previous_assistant_message_missing'):self.submit(key='new')
    def test_expired_wait_has_no_new_remote_effect(self):
        before=len(self.provider.calls)
        with self.assertRaisesRegex(ProtocolError,'response_wait_expired'):
            self.gw.submit(self.identity,canonical(request()),'new',deadline=time.monotonic()-1)
        self.assertEqual(len(self.provider.calls),before)
    def test_request_stop_admission_immediate(self):
        self.gw.request_stop()
        with self.assertRaisesRegex(ProtocolError,'gateway_stopped'):self.submit()

    def test_http_wait_timeout_retains_same_request_and_late_result(self):
        server=ResponsesServer(self.gw); worker=threading.Thread(target=server.serve_forever,daemon=True); worker.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
        path='/activations/'+self.grant['activation_id']+'/v1/responses'
        headers={'Content-Type':'application/json','session-id':'http-project','thread-id':'http-thread','Idempotency-Key':'http-key'}
        conn.request('POST',path,canonical(request()),headers); res=conn.getresponse(); data=json.loads(res.read()); conn.close()
        self.assertEqual(res.status,409); self.assertEqual(data['error']['code'],'response_wait_expired_same_request_recoverable')
        ticket=self.gw.submit({'session-id':'http-project','thread-id':'http-thread'},canonical(request()),'http-key')
        self.assertEqual(ticket['request_id'],data['request_id'])
        self.provider.publish_result(self.gw,ticket,response(ticket['request_id']))
        conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
        conn.request('POST',path,canonical(request()),headers); res=conn.getresponse(); body=res.read();conn.close()
        self.assertEqual(res.status,200);self.assertIn(b'response.completed',body)


class WireTests(unittest.TestCase):
    def test_full_history_and_previous_response_rejected(self):
        body=request(); body['previous_response_id']='resp-old'
        with self.assertRaisesRegex(ProtocolError,'previous_response_id_unsupported'):validate_request(body)
    def test_alias_and_unknown_tools_rejected(self):
        body=request(); body['reasoning_effort']='low'
        with self.assertRaisesRegex(ProtocolError,'ambiguous_reasoning_effort'):validate_request(body)
    def test_tool_schema_and_namespace(self):
        body=request(); output=response('abc','function_call'); validate_response(output,body)
        output['output'][0]['arguments']='{"cmd":3}'
        with self.assertRaisesRegex(ProtocolError,'tool_arguments_schema_mismatch'):validate_response(output,body)
        output=response('abc','function_call');output['output'][0].pop('namespace')
        with self.assertRaisesRegex(ProtocolError,'unadvertised_tool'):validate_response(output,body)

if __name__=='__main__': unittest.main()
