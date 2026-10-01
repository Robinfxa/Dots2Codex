"""Synthetic only: no Google calls, native inference, or real client tool effects."""
import concurrent.futures
import copy
import http.client
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
from remote_transport import *
from remote_transport.model import canonical, MAX_WIRE_BYTES, MAX_REQUESTS
from remote_transport.control import *
from remote_transport.controlled import CASController,CASWorker
from remote_transport.connector_worker import ConnectorWorker
from remote_transport.facade import RemoteResponsesFacade
from remote_transport.session import _index
from fakes import FakeDrive
from test_docs_cas import FakeDocs


def wire(text,history=(),tools=()):
    return {'model':'native-subagent-bridge','stream':True,'tools':list(tools),
            'input':list(history)+[{'type':'message','role':'user','content':[{'type':'input_text','text':text}]}]}

def events(raw):
    return [json.loads(line[6:]) for line in raw.splitlines() if line.startswith(b'data: ')]


class Fixture(unittest.TestCase):
    scope='text_only'
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.pin=deployment('long','synthetic/native',seconds=28800,max_requests=128,scope=self.scope)
        self.drive=FakeDrive();self.messages=GoogleDriveBackend(self.drive,'folder',discovery='control_refs')
        self.docs=FakeDocs(initial_state(self.pin,'control'))
        self.store=GoogleDocsCASControlStore(self.docs,'doc','tab','control','long','writer')
        self.coord=SessionCoordinator(self.store,self.messages)
        self.controller=CASController(Journal.provision(self.root/'controller',self.pin,'controller'),self.messages,self.coord)
        self.worker=CASWorker(Journal.provision(self.root/'worker',self.pin,'worker'),self.messages,self.coord)
    def tearDown(self):self.tmp.cleanup()


class LongCoreTests(Fixture):
    def test_boundaries(self):
        with self.assertRaisesRegex(ProtocolError,'invalid_lifetime'):deployment('s','n',seconds=28801)
        with self.assertRaisesRegex(ProtocolError,'unsupported_deployment'):deployment('s','n',max_requests=129)
        self.assertEqual(self.pin.body['payload']['max_requests'],128)

    def test_128_rounds_across_hours_exact_identity_and_ledger(self):
        start=self.pin.body['payload']['created']
        for i in range(128):
            with patch('time.time',return_value=start+i*120):
                request=self.controller.submit('synthetic '+str(i),'key-'+str(i))
                permit=self.worker.start_next();self.assertEqual(permit['native_task_id'],'synthetic/native')
                result=self.worker.complete(permit,'synthetic answer '+str(i))
                self.controller.record_delivery(request,result,'synthetic parsed answer')
        state=self.store.read().state
        self.assertEqual(state['control_epoch'],640);self.assertEqual(len(state['history']),127)
        self.assertLess(len(block_for(state).encode()),MAX_CONTROL_BYTES)
        with self.controller.journal.locked() as journal:
            _index(dict(reversed(list(journal['objects'].items()))),self.pin)
        with self.assertRaisesRegex(ProtocolError,'request_budget_exceeded'):
            self.controller.submit('excess','key-129')
        self.assertEqual(len([x for x in self.drive.files.values() if Object.parse(x['raw']).body['kind']=='started']),128)

    def test_close_before_begin_fences_stale_plan(self):
        self.controller.submit('one','key')
        self.coord.transition('claim',{'binding':binding_for(self.pin),'claim_id':'claim'},'claim')
        old=self.store.read();state=old.state
        self.controller.close_session()
        dispatch=hash_bytes(canonical({'request':state['request']['object_id'],'incarnation':self.pin.body['identity']['worker_journal_id']}))
        with self.assertRaises(CASConflict):self.coord.transition('begin',{'binding':binding_for(self.pin),
            'claim_id':'claim','dispatch_id':dispatch},'begin',snapshot=old)
        self.assertIsNone(self.worker.start_next())
        with self.assertRaisesRegex(ProtocolError,'control_session_closed'):self.controller.submit('two','key2')

    def test_late_known_result_after_close_and_expiry(self):
        request=self.controller.submit('one','key');permit=self.worker.start_next();self.controller.close_session()
        with patch('time.time',return_value=self.pin.body['payload']['expires']+10):
            result=self.worker.complete(permit,'late actual answer')
            self.assertEqual(self.controller.result(request).oid,result)
            self.controller.record_delivery(request,result,'observed after expiry')
        self.assertTrue(self.store.read().state['closed'])

    def test_aggregate_cap_before_publication(self):
        calls=len(self.drive.create_calls)
        with patch('remote_transport.session.MAX_TRANSCRIPT_BYTES',1):
            with self.assertRaisesRegex(ProtocolError,'session_transcript_budget_exceeded'):
                self.controller.submit('one','key')
        self.assertEqual(len(self.drive.create_calls),calls);self.assertEqual(self.store.read().state['control_epoch'],0)


class ConnectorWorkerTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config={'document_id':'doc','tab_id':'tab','control_id':'control','writer_identity':'worker','folder_id':'folder'}
        self.cw=ConnectorWorker.provision(self.root/'connector',self.pin,self.config,'synthetic/native')
        self.entries=[]
    def resource(self):return self.docs.get_document('doc')
    def entries_for_all(self):
        self.entries=[]
        for fid,data in self.drive.files.items():
            obj=Object.parse(data['raw']);raw=self.root/(fid+'.json');meta=self.root/(fid+'-meta.json')
            raw.write_bytes(obj.raw);raw.chmod(0o600)
            meta.write_bytes(canonical({'id':fid,'title':data['name'],'mime_type':'application/json','parent_ids':['folder']}));meta.chmod(0o600)
            self.entries.append({'reference':{'object_id':obj.oid,'locator':{'backend':'drive','folder_id':'folder','file_id':fid}},
                                 'file':str(raw),'metadata':str(meta)})
        return self.entries
    def execute(self,packet):
        args=packet['tool_arguments']
        response=self.docs.batch_update_document(args['document_id'],args['requests'],args['write_control'])
        return response,self.resource()
    def begin(self):
        packet=self.cw.tick(self.resource(),self.entries_for_all());self.assertEqual(packet['kind'],'claim')
        self.cw.accept(*self.execute(packet))
        packet=self.cw.tick(self.resource(),self.entries_for_all());self.assertEqual(packet['kind'],'begin')
        return packet
    def finish(self,seq):
        self.cw.input(self.entries_for_all(),seq)
        packets=self.cw.result(seq,'synthetic answer '+str(seq))
        for item in packets['objects']:
            self.messages.publish(Object.parse(Path(item['path']).read_bytes()),self.messages.reserve())
        packet=self.cw.tick(self.resource(),self.entries_for_all());self.assertEqual(packet['kind'],'result')
        self.cw.accept(*self.execute(packet))
    def test_five_turn_connector_loop_crosses_old_expiry(self):
        start=self.pin.body['payload']['created']
        for seq in range(1,6):
            with patch('time.time',return_value=start+seq*1800):
                request=self.controller.submit('synthetic '+str(seq),'k'+str(seq))
                self.cw.accept(*self.execute(self.begin()))
                self.finish(seq)
                result=self.controller.result(request)
                self.controller.record_delivery(request,result.oid,'synthetic observed')
                self.cw.tick(self.resource(),self.entries_for_all())
        self.assertEqual(len(self.cw.status()['records']),5)
        with patch('time.time',return_value=self.pin.body['payload']['expires']+1):
            self.assertEqual(self.cw.poll()['action'],'expired')
    def test_unknown_begin_never_reissues_input(self):
        self.controller.submit('one','key');packet=self.begin();self.execute(packet)
        self.assertEqual(self.cw.tick(self.resource(),[])['action'],'await_exact_cas_response')
        with self.assertRaisesRegex(ProtocolError,'one_use_input_unavailable'):self.cw.input(self.entries_for_all(),1)
    def test_input_is_one_use_after_restart(self):
        self.controller.submit('one','key');self.cw.accept(*self.execute(self.begin()))
        self.cw.input(self.entries_for_all(),1)
        restarted=ConnectorWorker(self.root/'connector','synthetic/native')
        with self.assertRaisesRegex(ProtocolError,'one_use_input_unavailable'):restarted.input(self.entries,1)
    def test_wrong_native_identity_and_rollback(self):
        with self.assertRaisesRegex(ProtocolError,'actual_native_identity_mismatch'):ConnectorWorker(self.root/'connector','other')
        old=self.resource();self.controller.submit('one','key');self.cw.tick(self.resource(),[])
        with self.assertRaisesRegex(ProtocolError,'control_epoch_rollback'):self.cw.tick(old,[])
    def test_poll_quota_and_stop(self):
        self.assertEqual(self.cw.poll()['action'],'read_control')
        self.assertEqual(self.cw.poll()['action'],'wait')
        self.cw.status(stop=True);self.assertEqual(self.cw.poll()['action'],'stopped')


class LongHTTPTests(Fixture):
    scope='responses_tools'
    def setUp(self):
        super().setUp()
        self.facade=RemoteResponsesFacade(self.controller,long_session=True,request_deadline=3,
                                           poll_interval=.05,heartbeat_interval=.05).start()
        self.headers={'Content-Type':'application/json','session-id':'client','thread-id':str(uuid.uuid4())}
    def tearDown(self):self.facade.close();super().tearDown()
    def post(self,body,path='/v1/responses'):
        conn=http.client.HTTPConnection('127.0.0.1',self.facade.server.server_port,timeout=10)
        conn.request('POST',path,body=canonical(body),headers=self.headers)
        response=conn.getresponse();raw=response.read();status=response.status;rid=response.getheader('X-Request-ID');conn.close()
        return status,raw,rid
    def get(self,path):
        conn=http.client.HTTPConnection('127.0.0.1',self.facade.server.server_port,timeout=10)
        conn.request('GET',path,headers=self.headers);response=conn.getresponse();raw=response.read();conn.close()
        return response.status,json.loads(raw)
    def turn(self,body,result,delay=0):
        with concurrent.futures.ThreadPoolExecutor() as pool:
            pending=pool.submit(self.post,body)
            permit=self.worker.poll(self.worker.start_next,attempts=8,initial_delay=.02,max_delay=.1)
            self.assertIsNotNone(permit)
            if delay:time.sleep(delay)
            self.worker.complete(permit,result);status,raw,rid=pending.result()
        self.assertEqual(status,200,raw)
        return events(raw),rid
    def test_heartbeat_same_text_retry_and_recovery(self):
        body=wire('one');ev,rid=self.turn(body,'answer',delay=.18)
        self.assertEqual(ev[0]['type'],'response.created')
        self.assertTrue(any(e['type']=='response.in_progress' for e in ev))
        self.assertEqual(ev[-1]['type'],'response.completed')
        status,raw,again=self.post(body);self.assertEqual((status,again),(200,rid))
        status,data=self.get('/v1/bridge/requests/'+rid+'/result');self.assertEqual(data['item']['content'][0]['text'],'answer')
        with self.worker.journal.locked() as state:self.assertEqual(len(state['executions']),1)
    def test_wait_expiry_then_late_result_recovery(self):
        self.facade.server.deadline=.1
        status,raw,rid=self.post(wire('slow'))
        self.assertEqual(status,200);self.assertEqual(events(raw)[-1]['type'],'response.failed')
        permit=self.worker.start_next();result=self.worker.complete(permit,'late answer')
        status,data=self.get('/v1/bridge/requests/'+rid+'/result');self.assertEqual(data['result_id'],result)
        with self.controller.journal.locked() as s:self.assertFalse(s['deliveries'])
        status,raw,_=self.post({'result_id':result,'evidence':'synthetic observer read exact answer'},'/v1/bridge/requests/'+rid+'/ack')
        self.assertEqual(status,200,raw)
    def test_tool_loop_and_no_uncertain_tool_replay(self):
        tool={'type':'function','name':'fixture_read','parameters':{'type':'object','properties':{'path':{'type':'string'}},'required':['path'],'additionalProperties':False}}
        body=wire('read fixture',tools=[tool])
        result={'kind':'function_call','name':'fixture_read','arguments':{'path':'fixture.txt'}}
        ev,rid=self.turn(body,result)
        item=next(x['item'] for x in ev if x['type']=='response.output_item.done')
        self.assertFalse(ev[-1]['response']['end_turn'])
        status,raw,_=self.post(body);self.assertEqual(status,400);self.assertIn(b'tool_emission_outcome_unknown',raw)
        output={'type':'function_call_output','call_id':item['call_id'],'output':'synthetic fixture bytes'}
        # Dynamic advertisements disappear; previous item is validated with its ORIGINAL schema.
        ev2,_=self.turn(wire('finish',[item,output]),'done')
        self.assertEqual(ev2[-1]['type'],'response.completed')
        with self.worker.journal.locked() as s:self.assertEqual(len(s['executions']),2)
    def test_forged_history_fails_before_receipt(self):
        ev,rid=self.turn(wire('one'),'answer')
        item=next(x['item'] for x in ev if x['type']=='response.output_item.done')
        forged={'type':'function_call','id':'fc_fake','name':'fake','call_id':'call_fake','arguments':'{}'}
        output={'type':'function_call_output','call_id':'call_fake','output':'fake'}
        status,raw,_=self.post(wire('next',[item,forged,output]))
        self.assertEqual(status,400);self.assertIn(b'unissued_tool_history',raw)
        with self.controller.journal.locked() as s:self.assertFalse(s['deliveries']);self.assertEqual(len(s['requests']),1)
    def test_large_wire_and_oversize_before_publication(self):
        body=wire('x'*900000);self.turn(body,'okay')
        calls=len(self.drive.create_calls)
        status,raw,_=self.post(wire('x'*MAX_WIRE_BYTES))
        self.assertEqual(status,400);self.assertEqual(len(self.drive.create_calls),calls)
    def test_status_cannot_observe_half_published_facade_request_index(self):
        import threading
        staged=threading.Event();release=threading.Event();reader_started=threading.Event()
        original=self.controller.submit_request
        def paused_submit(*args,**kwargs):
            rid=original(*args,**kwargs);staged.set()
            if not release.wait(3):raise RuntimeError('synthetic_barrier_timeout')
            return rid
        def read():
            reader_started.set();return self.facade.read_state()
        with patch.object(self.controller,'submit_request',side_effect=paused_submit), \
             concurrent.futures.ThreadPoolExecutor() as pool:
            pending=pool.submit(self.post,wire('one'))
            self.assertTrue(staged.wait(3))
            reading=pool.submit(read);self.assertTrue(reader_started.wait(3))
            try:
                # The request journal is durable, but its facade job is not yet
                # saved. A concurrent read must wait, not report corruption.
                time.sleep(.03);self.assertFalse(reading.done())
            finally:release.set()
            self.assertEqual(len(reading.result(timeout=3)['jobs']),1)
            status,raw,_=self.post({'confirm':True},'/v1/bridge/close')
            self.assertEqual(status,200,raw);self.facade.close();pending.result(timeout=3)

    def test_close_while_request_inflight(self):
        with concurrent.futures.ThreadPoolExecutor() as pool:
            pending=pool.submit(self.post,wire('one'))
            deadline=time.time()+2
            while not self.facade.read_state()['jobs'] and time.time()<deadline:time.sleep(.01)
            status,raw,_=self.post({'confirm':True},'/v1/bridge/close')
            self.assertEqual(status,200,raw);self.assertTrue(json.loads(raw)['closed'])
            self.assertIsNone(self.worker.start_next())
            self.facade.close();pending.result()

    def test_wait_disconnect_before_first_tool_can_reattach(self):
        tool={'type':'function','name':'fixture','parameters':{'type':'object','properties':{}}}
        body=wire('fixture',tools=[tool]);self.facade.server.deadline=.1
        status,raw,rid=self.post(body);self.assertEqual(events(raw)[-1]['type'],'response.failed')
        permit=self.worker.start_next();self.worker.complete(permit,{'kind':'function_call','name':'fixture','arguments':{}})
        status,raw,same=self.post(body)
        self.assertEqual((status,same),(200,rid));self.assertEqual(events(raw)[-1]['type'],'response.completed')
        status,raw,_=self.post(body);self.assertEqual(status,400);self.assertIn(b'tool_emission_outcome_unknown',raw)

    def test_immediate_next_turn_waits_for_delivery_flush_ledger(self):
        original=self.facade.server.store.delivery
        def slow_delivery(*args):
            time.sleep(.25);return original(*args)
        self.facade.server.store.delivery=slow_delivery
        with concurrent.futures.ThreadPoolExecutor() as pool:
            first=pool.submit(self.post,wire('first'))
            permit=self.worker.poll(self.worker.start_next,attempts=8,initial_delay=.02,max_delay=.1)
            self.worker.complete(permit,'one')
            status,raw,rid=first.result();self.assertEqual(status,200)
            item=next(e['item'] for e in events(raw) if e['type']=='response.output_item.done')
            # HTTP client consumes until EOF, whereas official Codex returns on
            # response.completed. Exercise the race directly with delivery gate held.
        self.facade.server.inflight.acquire();self.facade.server.delivery_started.set()
        import threading
        timer=threading.Timer(.25,self.facade.server.inflight.release);timer.start()
        try:self.turn(wire('second',[item]),'two')
        finally:timer.join()

    def test_custom_tool_round_and_wrong_call_output(self):
        tool={'type':'custom','name':'fixture_patch','format':{'type':'text'}}
        ev,_=self.turn(wire('patch fixture',tools=[tool]),{'kind':'custom_tool_call','name':'fixture_patch','input':'synthetic patch'})
        item=next(e['item'] for e in ev if e['type']=='response.output_item.done')
        wrong={'type':'custom_tool_call_output','call_id':'wrong','output':'okay'}
        status,raw,_=self.post(wire('finish',[item,wrong]));self.assertEqual(status,400);self.assertIn(b'uncorrelated_tool_output',raw)
        right={'type':'custom_tool_call_output','call_id':item['call_id'],'output':'synthetic applied'}
        self.turn(wire('finish',[item,right]),'done')

    def test_invalid_function_schema_cannot_publish_result(self):
        tool={'type':'function','name':'fixture','parameters':{'type':'object','properties':{'n':{'type':'integer'}},'required':['n']}}
        with concurrent.futures.ThreadPoolExecutor() as pool:
            pending=pool.submit(self.post,wire('fixture',tools=[tool]))
            permit=self.worker.poll(self.worker.start_next,attempts=8,initial_delay=.02,max_delay=.1)
            before=len(self.drive.create_calls)
            with self.assertRaisesRegex(ProtocolError,'tool_arguments_schema_mismatch'):
                self.worker.complete(permit,{'kind':'function_call','name':'fixture','arguments':{'n':'invalid'}})
            self.assertEqual(len(self.drive.create_calls),before)
            self.facade.close();pending.result()

    def test_operator_flags_are_bounded_and_do_not_execute(self):
        from remote_transport.operator import codex_command
        command=codex_command('http://127.0.0.1:8765/v1','/tmp/project')
        joined=' '.join(command)
        self.assertIn('on-request',joined);self.assertIn('workspace-write',joined)
        self.assertIn('features.multi_agent=false',joined)
        self.assertIn('features.unbounded_connection_retries=false',joined)
        self.assertIn('requires_openai_auth=false',joined)
        self.assertIn('request_max_retries=0',joined)
        with self.assertRaisesRegex(ProtocolError,'loopback_ready_url_required'):
            codex_command('https://external.example/v1','/tmp/project')

    def test_plain_controller_cannot_claim_long_session_support(self):
        fresh=Controller(Journal.provision(self.root/'plain',self.pin,'controller'),self.messages)
        with self.assertRaisesRegex(ProtocolError,'long_session_requires_docs_cas'):
            RemoteResponsesFacade(fresh,long_session=True)

    def test_missing_tool_emission_marker_fails_closed(self):
        self.turn(wire('one'),'answer')
        state=self.facade.read_state()
        next(iter(state['jobs'].values())).pop('tool_emission_started')
        self.facade.save_state(state)
        with self.assertRaisesRegex(ProtocolError,'tool_emission_marker_missing'):
            self.facade.read_state()
