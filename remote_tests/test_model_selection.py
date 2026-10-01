"""Offline selected-native routing tests. All admissions/inference are fixtures."""
import concurrent.futures
import copy
import http.client
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from remote_transport import deployment, Object, ProtocolError, Journal, GoogleDriveBackend
from remote_transport.model import canonical, hash_bytes
from remote_transport.selection import (load_catalog, select, validate_selection, spawn_arguments,
    admission_receipt, validate_admission, validate_request_selection, pin_selection)
from remote_transport.wire import validate_remote_request
from remote_transport.session import Controller, Worker, _index
from remote_transport.control import initial_state as control_state, GoogleDocsCASControlStore, SessionCoordinator, binding_for
from remote_transport.controlled import CASController, CASWorker
from remote_transport.facade import RemoteResponsesFacade
from remote_transport.router_bootstrap import (initial_state, worker_admitted, bundle_ready,
    worker_polling, consume_bundle, verify_context, block_for, decode_block, root_context,
    verify_join_code)
from remote_transport.router_join import main as join_main, JoinLedger
from test_router_bootstrap import RouterBootstrapFixtures, doc, private_json
from fakes import FakeDrive
from test_docs_cas import FakeDocs


def selected(model='gpt-6-astra', effort='xhigh'):
    return select(load_catalog(), model, effort)


def receipt(selection=None, native='/root/test_worker'):
    selection = selection or selected()
    args = spawn_arguments(selection, native.rsplit('/', 1)[-1], 'Offline fixture worker')
    return admission_receipt(selection, args, native)


def wire(text='hello', selection=None, history=(), tools=()):
    selection = selection or selected()
    return {'model': selection['model'], 'reasoning': {'effort': selection['reasoning_effort']},
            'stream': True, 'tools': list(tools), 'input': list(history) + [
                {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}]}


class SelectionTests(unittest.TestCase):
    def test_every_advertised_pair_has_exact_spawn_and_receipt(self):
        catalog = load_catalog()
        for model, entry in catalog['models'].items():
            for effort in entry['bridge_efforts']:
                with self.subTest(model=model, effort=effort):
                    s = select(catalog, model, effort)
                    args = spawn_arguments(s, 'fixture', 'Full native worker task')
                    self.assertEqual((args['model'], args['reasoning_effort'], args['fork_turns']), (model, effort, 'none'))
                    r = admission_receipt(s, args, '/root/fixture')
                    self.assertFalse(r['underlying_model_verified'])
                    self.assertEqual(validate_admission(r, s, '/root/fixture'), r)
                    validate_remote_request(wire(selection=s), 'responses_tools')

    def test_unsupported_pairs_no_aliases_or_ultra(self):
        for model, effort in [('gpt-6-astra', 'ultra'), ('gpt-6-luna', 'ultra'), ('default', 'xhigh'),
                              ('native-subagent-bridge', 'high'), ('gpt-6-sol', None), ('gpt-6-sol', 'disabled')]:
            with self.subTest(model=model, effort=effort), self.assertRaises(ProtocolError):
                select(load_catalog(), model, effort)

    def test_selection_hash_and_schema_are_exact(self):
        for key, value in [('catalog_sha256', '0' * 64), ('catalog_version', 'future'), ('contract', 'other')]:
            with self.assertRaises(ProtocolError): validate_selection({**selected(), key: value})
        with self.assertRaises(ProtocolError): validate_selection({**selected(), 'fallback': 'gpt-6-sol'})
        catalog = load_catalog(); catalog['models']['fake'] = catalog['models']['gpt-6-sol']
        with self.assertRaises(ProtocolError): select(catalog, 'fake', 'low')

    def test_admission_cannot_omit_override_or_inherit(self):
        s = selected(); args = spawn_arguments(s, 'fixture', 'task')
        for changed in [{k:v for k,v in args.items() if k != 'model'}, {**args, 'fork_turns':'all'},
                        {**args, 'reasoning_effort':'low'}, {**args, 'model':'gpt-6-sol'}]:
            with self.assertRaises(ProtocolError): admission_receipt(s, changed, '/root/fixture')
        with self.assertRaises(ProtocolError): admission_receipt(s, args, '/root/unrelated')

    def test_receipt_mismatch_and_telemetry_claim_are_rejected(self):
        s = selected(); r = receipt(s)
        for key, value in [('native_task_id', '/root/other'), ('submitted_model', 'gpt-6-sol'),
                           ('submitted_reasoning_effort', 'low'), ('underlying_model_verified', True),
                           ('underlying_model_verified', 0), ('catalog_sha256', '0' * 64)]:
            with self.assertRaises(ProtocolError): validate_admission({**r, key:value}, s, '/root/test_worker')

    def test_wire_validation_does_not_rewrite_original(self):
        request = wire(); original = canonical(request)
        validate_remote_request(request, 'responses_tools')
        self.assertEqual(canonical(request), original)
        self.assertEqual(request['model'], 'gpt-6-astra')

    def test_request_must_match_bound_pair_and_explicit_effort(self):
        for change, code in [({'model':'gpt-6-sol'}, 'model_change'),
                             ({'reasoning':{'effort':'low'}}, 'effort_change'),
                             ({'reasoning':{}}, 'explicit_reasoning'),
                             ({'reasoning_effort':'xhigh'}, 'ambiguous_reasoning')]:
            with self.assertRaisesRegex(ProtocolError, code): validate_request_selection({**wire(), **change}, selected())
        with self.assertRaises(ProtocolError): validate_request_selection(wire())
        validate_request_selection({'model':'native-subagent-bridge'})

    def test_pin_receipt_hash_is_part_of_deployment(self):
        inference = {'selection':selected(), 'admission':receipt()}
        p = deployment('selected', '/root/test_worker', inference=inference)
        self.assertEqual(pin_selection(Object.parse(p.raw)), selected())
        changed = p.body['payload']; changed['inference']['admission']['submitted_reasoning_effort'] = 'low'
        with self.assertRaises(ProtocolError): Object.make(p.body['identity'],'deployment',0,None,changed)
        with self.assertRaises(ProtocolError): deployment('s', '/root/other', inference=inference)


class SelectedBootstrapTests(RouterBootstrapFixtures, unittest.TestCase):
    def setUp(self):
        super().setUp()
        root = root_context(self.state)
        self.state = initial_state(bootstrap_id=root['bootstrap_id'],session_id=root['session_id'],
            created=root['created'],expires=root['expires'],join_code=self.code,folder_id=root['folder_id'],
            control_document_id=root['control']['document_id'],control_tab_id=root['control']['tab_id'],
            control_id=root['control']['control_id'],mac_writer_identity=root['control']['mac_writer_identity'],
            worker_writer_identity=root['control']['worker_writer_identity'],bootstrap_document_id='bootDoc',
            bootstrap_tab_id='t.0',forward_probe=self.forward,required_selection=selected())
        self.admission = receipt(selected(), self.native)

    def admit(self):
        return worker_admitted(self.state,join_code=self.code,native_task_id=self.native,
            probe=self.probe,admission=self.admission,now=self.now+1)

    def test_signed_selected_bootstrap_full_lifecycle(self):
        admitted = self.admit()
        pin = deployment('router-session',self.native,seconds=600,now=self.now+2,
            inference={'selection':selected(),'admission':self.admission})
        ready = bundle_ready(admitted,join_code=self.code,pin_raw=pin.raw,config_raw=canonical(self.config),
            deployment_hash=pin.oid,now=self.now+3)
        polling = worker_polling(ready,join_code=self.code,native_task_id=self.native,
            runtime_hash='a'*64,now=self.now+4)
        consumed = consume_bundle(polling,join_code=self.code,now=self.now+5)
        self.assertEqual(decode_block(block_for(consumed)),consumed)
        self.assertTrue(block_for(consumed).startswith('DOTS2CODEX_ROUTER_BOOTSTRAP_BEGIN_V3'))
        verify_join_code(consumed,self.code)

    def test_selection_tamper_and_missing_admission_fail(self):
        changed = copy.deepcopy(self.state);changed['required_selection'] = selected(effort='low')
        with self.assertRaises(ProtocolError): verify_context(changed,self.code,'bootDoc','t.0')
        with self.assertRaises(ProtocolError): worker_admitted(self.state,join_code=self.code,
            native_task_id=self.native,probe=self.probe)
        admitted = self.admit();admitted['worker']['admission']['underlying_model_verified'] = True
        with self.assertRaises(ProtocolError): verify_join_code(admitted,self.code)

    def test_selected_bootstrap_rejects_legacy_or_different_pin(self):
        admitted = self.admit()
        for inference in (None, {'selection':selected(effort='low'),'admission':receipt(selected(effort='low'),self.native)}):
            pin=deployment('router-session',self.native,seconds=600,now=self.now+2,inference=inference)
            with self.assertRaisesRegex(ProtocolError,'bootstrap_pin_inference_mismatch'):
                bundle_ready(admitted,join_code=self.code,pin_raw=pin.raw,config_raw=canonical(self.config),
                    deployment_hash=pin.oid,now=self.now+3)

    def test_parent_plan_record_uses_exact_fake_native_call_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);code=root/'code';code.write_text(self.code);code.chmod(0o600)
            snap=root/'snapshot';private_json(snap,doc('bootDoc','t.0','r1',block_for(self.state)))
            message=root/'message';message.write_text('Offline fake admission task');message.chmod(0o600)
            common=['--snapshot',str(snap),'--document-id','bootDoc','--tab-id','t.0','--join-code-file',str(code),
                    '--state-dir',str(root/'ledger')]
            plan=root/'plan';out=join_main(['plan-native',*common,'--task-name','serve_router',
                 '--message-file',str(message),'--save',str(plan)])
            self.assertEqual(out['arguments']['fork_turns'],'none')
            with self.assertRaisesRegex(ProtocolError,'already_reserved'):
                join_main(['plan-native',*common,'--task-name','another','--message-file',str(message),'--save',str(root/'plan2')])
            args=root/'args';private_json(args,out['arguments'])
            # A fixture callback receives exactly the emitted native arguments.
            def fake_native(**kwargs):
                self.assertEqual(kwargs['model'],'gpt-6-astra');self.assertEqual(kwargs['reasoning_effort'],'xhigh')
                return {'task_name':'/root/'+kwargs['task_name']}
            native=root/'native';private_json(native,fake_native(**out['arguments']))
            evidence=root/'receipt';record=join_main(['record-native',*common,'--plan-file',str(plan),
                 '--actual-arguments',str(args),'--native-result',str(native),'--save',str(evidence)])
            self.assertFalse(record['underlying_model_verified'])
            self.assertEqual(json.loads(evidence.read_bytes())['native_task_id'],self.native)
            with self.assertRaises(ProtocolError):
                join_main(['record-native',*common,'--plan-file',str(plan),'--actual-arguments',str(args),
                           '--native-result',str(native),'--save',str(root/'second-receipt')])


class SelectedRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.pin=deployment('selected','/root/test_worker',seconds=600,max_requests=10,scope='responses_tools',
            inference={'selection':selected(),'admission':receipt()})
        self.drive=FakeDrive();self.messages=GoogleDriveBackend(self.drive,'folder',discovery='control_refs')
        self.docs=FakeDocs(control_state(self.pin,'control'))
        self.store=GoogleDocsCASControlStore(self.docs,'doc','tab','control','selected','writer')
        self.coord=SessionCoordinator(self.store,self.messages)
        self.controller=CASController(Journal.provision(self.root/'controller',self.pin,'controller'),self.messages,self.coord)
        self.worker=CASWorker(Journal.provision(self.root/'worker',self.pin,'worker'),self.messages,self.coord)
        self.facade=RemoteResponsesFacade(self.controller,long_session=True,request_deadline=2,poll_interval=.05,heartbeat_interval=.05).start()
        self.headers={'Content-Type':'application/json','session-id':'client','thread-id':str(uuid.uuid4())}
    def tearDown(self):self.facade.close();self.tmp.cleanup()
    def post(self,body):
        conn=http.client.HTTPConnection('127.0.0.1',self.facade.server.server_port,timeout=4)
        conn.request('POST','/v1/responses',body=canonical(body),headers=self.headers)
        res=conn.getresponse();status,raw=res.status,res.read();conn.close();return status,raw
    def turn(self,body,answer):
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future=pool.submit(self.post,body)
            permit=self.worker.poll(self.worker.start_next,attempts=8,initial_delay=.02,max_delay=.1)
            self.assertIsNotNone(permit)
            self.assertEqual(permit['request']['responses_request'],body)
            self.assertEqual(permit['native_task_id'],'/root/test_worker')
            self.worker.complete(permit,answer);status,raw=future.result()
        self.assertEqual(status,200,raw)
        return next(json.loads(line[6:])['item'] for line in raw.splitlines() if line.startswith(b'data: ') and json.loads(line[6:])['type']=='response.output_item.done')
    def test_actual_pair_preserved_through_tool_round_trip(self):
        tool={'type':'function','name':'fixture_read','parameters':{'type':'object','properties':{}}}
        body=wire(tools=[tool]);item=self.turn(body,{'kind':'function_call','name':'fixture_read','arguments':{}})
        output={'type':'function_call_output','call_id':item['call_id'],'output':'fixture data'}
        full=wire('finish',history=body['input']+[item,output],tools=[tool])
        self.turn(full,'finished')
        self.assertEqual(self.store.read().state['admissions'],2)
    def test_switch_before_first_request_has_no_publication_or_cas(self):
        for body in (wire(selection=selected(effort='low')),wire(selection=selected('gpt-6-sol'))):
            before=len(self.drive.create_calls);status,raw=self.post(body)
            self.assertEqual(status,400,raw);self.assertIn(b'new_paired_session',raw)
            self.assertEqual(len(self.drive.create_calls),before);self.assertEqual(self.store.read().state['control_epoch'],0)
    def test_switch_after_answer_cannot_ack_previous_delivery(self):
        first=wire();item=self.turn(first,'answer');before=self.store.read().state['control_epoch']
        status,raw=self.post(wire('next',selection=selected(effort='low'),history=first['input']+[item]))
        self.assertEqual(status,400,raw);self.assertEqual(self.store.read().state['control_epoch'],before)
        with self.controller.journal.locked() as journal:self.assertFalse(journal['deliveries'])
    def test_direct_submit_and_forged_index_fail_before_write(self):
        before=len(self.drive.create_calls)
        with self.assertRaises(ProtocolError):self.controller.submit_request(wire(selection=selected(effort='low')),'bad')
        with self.assertRaises(ProtocolError):self.controller.submit('no explicit pair','bad2')
        self.assertEqual(len(self.drive.create_calls),before)
        obj=Object.make(self.pin.body['identity'],'request',1,self.pin.oid,
            {'responses_request':wire(selection=selected(effort='low')),'scope':'responses_tools'})
        with self.assertRaises(ProtocolError):_index({self.pin.oid:self.pin.value,obj.oid:obj.value},self.pin)
    def test_selected_session_cannot_generic_rebind_even_when_idle(self):
        new=deployment('selected','/root/other',generation=2,seconds=600,max_requests=10)
        with self.assertRaisesRegex(ProtocolError,'selected_session_rebind_requires_new_session'):
            self.coord.transition('rebind',{'deployment':new.value},'rebind')
        self.assertEqual(self.store.read().state['control_epoch'],0)
    def test_changed_permit_selection_cannot_complete(self):
        self.controller.submit_request(wire(),'one')
        permit=self.worker.start_next();before=len(self.drive.create_calls)
        wrong=copy.deepcopy(permit);wrong['inference_binding']['selection']['reasoning_effort']='low'
        with self.assertRaisesRegex(ProtocolError,'permit_inference_binding_mismatch'):
            self.worker.complete(wrong,'wrong')
        self.assertEqual(len(self.drive.create_calls),before)
        self.worker.complete(permit,'correct')
    def test_unknown_tool_outcome_still_blocks_and_replay_is_not_reinference(self):
        tool={'type':'function','name':'fixture','parameters':{'type':'object','properties':{}}}
        body=wire(tools=[tool]);item=self.turn(body,{'kind':'function_call','name':'fixture','arguments':{}})
        status,raw=self.post(body);self.assertEqual(status,400);self.assertIn(b'tool_emission_outcome_unknown',raw)
        status,raw=self.post(wire('again',history=body['input']+[item],tools=[tool]))
        self.assertEqual(status,400);self.assertIn(b'tool_outcome_unknown',raw)
        self.assertEqual(self.store.read().state['admissions'],1)
