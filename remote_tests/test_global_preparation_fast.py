"""Offline RPC counts and bounded preparation overlap/failure barriers.

Ports, provider responses and time are synthetic. No credential access, external
network, native admission or live activation is performed.
"""
import copy
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from examples.remote_setup import initialize_blank
from remote_tests import test_global_read_recovery as recovery_fixture
from remote_transport import global_control as queue, router_bootstrap as child
from remote_transport.global_fixture import identity
from remote_transport.global_gateway import private_write
from remote_transport.model import ProtocolError, canonical, deployment, hash_bytes
from remote_transport.control import initial_state


class PreparationFastTests(unittest.TestCase):
    # Reuse only the fixture methods, not the existing test cases.
    setUp = recovery_fixture.GlobalReadRecoveryFaultTests.setUp
    advance = recovery_fixture.GlobalReadRecoveryFaultTests.advance
    plan = recovery_fixture.GlobalReadRecoveryFaultTests.plan
    event = recovery_fixture.GlobalReadRecoveryFaultTests.event
    join = recovery_fixture.GlobalReadRecoveryFaultTests.join
    recovering_docs = recovery_fixture.GlobalReadRecoveryFaultTests.recovering_docs

    def route(self, parallel=True):
        self.join()
        self.bridge.parallel_preparation = parallel
        row = self.store.admission(self.generation, identity(), self.selection)
        self.route_id = row['id']
        self.runtime = self.bridge.root/'routes'/self.route_id
        return self.route_id

    def active(self):
        return json.loads((self.runtime/'active.json').read_text())

    def reservation(self, name):
        return json.loads((self.runtime/(name+'.json')).read_text())

    def admitted_child(self):
        rid = self.route(parallel=False)
        self.bridge.prepare_child(rid)
        self.event('claim', route_id=rid); self.event('begin', route_id=rid)
        plan = self.root/'native'/'spawn.json'
        package = Path(__file__).resolve().parents[1]
        out = self.ledger.plan_spawn(self.bridge.read(), rid, plan, package)
        receipt_path = self.root/'native'/'receipt.json'
        self.ledger.record_spawn(plan, out['arguments'],
            {'task_name': '/root/offline/'+out['arguments']['task_name']}, receipt_path)
        receipt = json.loads(receipt_path.read_text())
        self.event('admitted', route_id=rid)
        state = self.bridge.read().state['logical']['demands'][rid]['child_bootstrap']
        cc = queue.child_code(self.code, self.generation, rid)
        raw = canonical({'contract': 'dots-router-probe/1', 'bootstrap_id': state['bootstrap_id'],
                         'native_task_id': receipt['native_task_id']})
        fid = self.google.generate_id(); name = 'dots2codex-router-probe-'+state['bootstrap_id']+'.json'
        self.google.create_bytes('folder', name, raw, fid)
        admitted = child.worker_admitted(state, join_code=cc, native_task_id=receipt['native_task_id'],
            probe={'file_id': fid, 'name': name, 'sha256': hash_bytes(raw)}, admission=receipt)
        source = child.snapshot_from_document(self.google.get_document(state['bootstrap_document_id']),
            state['bootstrap_document_id'], state['bootstrap_tab_id'])
        self.google.batch_update_document(**child.plan(source, admitted, join_code=cc)['tool_arguments'])
        return rid, state

    def test_event_observes_exact_readback_with_two_gets_and_one_write(self):
        before_gets = self.service.count('get'); before_writes = self.service.count('batch')
        fresh = self.bridge.event('close', {'confirm': True})
        self.assertEqual(self.service.count('get')-before_gets, 2)
        self.assertEqual(self.service.count('batch')-before_writes, 1)
        saved = json.loads((self.bridge.root/'queue-observation.json').read_text())
        self.assertEqual(saved['event_hashes'], [hash_bytes(canonical(e)) for e in fresh.state['events']])
        # Exact readback observation retains rollback rejection on the next call.
        did = self.initial['document_id']
        self.google.docs[did] = [queue.block(self.initial), 1]
        with self.assertRaisesRegex(ProtocolError, 'rollback_or_fork'):
            self.bridge.read()

    def test_lost_write_response_is_reconciled_without_extra_get_or_replay(self):
        self.google.lose = True
        before_gets = self.service.count('get'); before_writes = self.service.count('batch')
        self.assertTrue(self.bridge.event('close', {'confirm': True}).state['logical']['closed'])
        self.assertEqual(self.service.count('get')-before_gets, 2)
        self.assertEqual(self.service.count('batch')-before_writes, 1)
        self.assertEqual(json.loads((self.bridge.root/'queue-close-controller.json').read_text())['status'], 'verified')

    def test_step_mirrors_its_one_fresh_get_without_caching_across_steps(self):
        controller = self.join()
        before = self.service.count('get')
        self.assertEqual(self.bridge.step()['state'], 'controller_active')
        self.assertEqual(self.service.count('get')-before, 1)
        self.now = controller['heartbeat_at']+900
        before = self.service.count('get')
        self.assertTrue(self.bridge.step()['restart_required'])
        self.assertEqual(self.service.count('get')-before, 1)
        self.assertFalse(self.store.status()['controller_active'])

    def test_initializer_returns_verified_snapshot_without_changing_default_result(self):
        did = self.google.create_document_once('folder', 'control')
        pin = deployment('test-session', 'test-native')
        before = self.service.count('get')
        result = initialize_blank(self.docs, pin, did, 't.0', 'control', 'writer', return_snapshot=True)
        self.assertEqual(result.state, initial_state(pin, 'control'))
        self.assertEqual(result.revision_id, 'r2')
        self.assertEqual(self.service.count('get')-before, 2)
        did = self.google.create_document_once('folder', 'control-default')
        self.assertEqual(initialize_blank(self.docs, pin, did, 't.0', 'control', 'writer'),
                         {'initialized': True, 'phase': 'IDLE', 'native_execution_authorized': False})

    def test_advance_child_reuses_control_initialization_readback(self):
        rid, state = self.admitted_child()
        with patch.object(self.docs, 'get_document', wraps=self.docs.get_document) as reads:
            result = self.bridge.advance_child(rid)
        self.assertEqual(result['state'], 'await_child_polling')
        control_reads = [c for c in reads.call_args_list if c.args[0] == state['control']['document_id']]
        self.assertEqual(len(control_reads), 2)
        self.assertEqual(self.reservation('initialize-control')['status'], 'verified')

    def test_restart_after_control_initialization_uses_fresh_read_and_no_rewrite(self):
        rid, state = self.admitted_child(); original = self.docs.get_document
        # Interrupt after verified control initialization but before bootstrap
        # publication. No assumption about an unnecessary intervening GET.
        with patch('remote_transport.global_google.mac._bootstrap_cas',
                   side_effect=RuntimeError('synthetic before bundle write')):
            with self.assertRaisesRegex(RuntimeError, 'before bundle'):
                self.bridge.advance_child(rid)
        control_id = state['control']['document_id']
        writes = len([c for c in self.google.calls if c[:2] == ('cas', control_id)])
        with patch.object(self.docs, 'get_document', wraps=original) as reads:
            self.assertEqual(self.bridge.advance_child(rid)['state'], 'await_child_polling')
        self.assertEqual(len([c for c in reads.call_args_list if c.args[0] == control_id]), 1)
        self.assertEqual(len([c for c in self.google.calls if c[:2] == ('cas', control_id)]), writes)

    def test_parallel_ports_overlap_but_all_docs_stay_on_owner_and_fields_survive(self):
        rid = self.route(); owner = threading.get_ident()
        entered_read = threading.Event(); entered_create = threading.Event()
        original_create = self.google.create_document_once
        original_get = self.google.get_document
        calls = []; doc_threads = []
        def create(folder, title):
            self.assertNotEqual(threading.get_ident(), owner)
            for label in ('create-control', 'create-bootstrap', 'create-forward-probe'):
                self.assertIn(self.reservation(label)['status'], ('dispatched', 'returned'))
                if ' Control ' in title:
                    self.assertEqual(self.reservation(label)['status'], 'dispatched')
            if ' Bootstrap ' in title:
                entered_create.set()
                self.assertTrue(entered_read.wait(3), 'Docs GET never overlapped Drive create')
            result = original_create(folder, title); calls.append(result)
            return result
        def get(did):
            doc_threads.append(threading.get_ident())
            self.assertEqual(threading.get_ident(), owner)
            if did != self.initial['document_id'] and not entered_read.is_set():
                entered_read.set()
                self.assertTrue(entered_create.wait(3), 'Drive create never overlapped Docs GET')
            return original_get(did)
        with patch.object(self.google, 'create_document_once', side_effect=create), patch.object(self.google, 'get_document', side_effect=get):
            result = self.bridge.prepare_child(rid)
        active = self.active()
        self.assertEqual([active['control_document_id'], active['bootstrap_document_id']], calls)
        self.assertEqual(active['control_tab_id'], 't.0'); self.assertEqual(active['bootstrap_tab_id'], 't.0')
        self.assertEqual(active['stage'], 'WAITING_FOR_WORKER')
        self.assertEqual(result.state['logical']['demands'][rid]['child_bootstrap']['control']['document_id'], calls[0])
        self.assertTrue(all(value == owner for value in doc_threads))
        for label in ('create-control', 'create-bootstrap'):
            self.assertEqual(self.reservation(label)['status'], 'returned')
        self.assertEqual(self.reservation('create-forward-probe')['status'], 'verified')

    def test_unknown_first_create_fences_worker_before_owner_handles_failure(self):
        rid = self.route(); calls = []
        def create(folder, title):
            calls.append(title)
            raise TimeoutError('synthetic ambiguous creation')
        with patch.object(self.google, 'create_document_once', side_effect=create):
            with self.assertRaisesRegex(RuntimeError, 'create_unknown'):
                self.bridge.prepare_child(rid)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.reservation('create-control')['status'], 'unknown')
        self.assertEqual(self.reservation('create-bootstrap')['status'], 'dispatched')
        self.assertFalse(self.google.files)
        with self.assertRaisesRegex(ProtocolError, 'runtime_exists'):
            self.bridge.prepare_child(rid)
        self.assertEqual(len(calls), 1)

    def test_partial_create_failure_retains_first_id_and_never_publishes_demand(self):
        rid = self.route(); original = self.google.create_document_once; calls = []
        def create(folder, title):
            calls.append(title)
            if ' Bootstrap ' in title:
                raise TimeoutError('synthetic bootstrap create unknown')
            return original(folder, title)
        with patch.object(self.google, 'create_document_once', side_effect=create):
            with self.assertRaises((RuntimeError, ProtocolError)):
                self.bridge.prepare_child(rid)
        active = self.active()
        self.assertEqual(active['control_document_id'], self.reservation('create-control')['document_id'])
        self.assertIsNone(active['bootstrap_document_id'])
        self.assertEqual(self.reservation('create-bootstrap')['status'], 'unknown')
        self.assertEqual(active['stage'], 'PREPARING')
        self.assertNotIn(rid, self.bridge.read().state['logical']['demands'])
        self.assertEqual(len(calls), 2); self.assertFalse(self.google.files)

    def test_docs_failure_drains_inflight_create_and_preserves_ids(self):
        rid = self.route(); owner = threading.get_ident()
        bootstrap_inflight = threading.Event(); release_bootstrap = threading.Event()
        original_create = self.google.create_document_once
        original_get = self.google.get_document; completed = []
        def create(folder, title):
            if ' Bootstrap ' in title:
                bootstrap_inflight.set()
                self.assertTrue(release_bootstrap.wait(3))
            result = original_create(folder, title); completed.append(result)
            return result
        def get(did):
            if did != self.initial['document_id']:
                self.assertEqual(threading.get_ident(), owner)
                self.assertTrue(bootstrap_inflight.wait(3))
                release_bootstrap.set()
                raise ValueError('synthetic permanent Docs failure')
            return original_get(did)
        with patch.object(self.google, 'create_document_once', side_effect=create), patch.object(self.google, 'get_document', side_effect=get):
            with self.assertRaises(ProtocolError):
                self.bridge.prepare_child(rid)
        active = self.active()
        self.assertEqual(len(completed), 2)
        self.assertEqual([active['control_document_id'], active['bootstrap_document_id']], completed)
        self.assertIsNone(active['control_tab_id']); self.assertIsNone(active['bootstrap_tab_id'])
        self.assertEqual(active['stage'], 'PREPARING')
        self.assertNotIn(rid, self.bridge.read().state['logical']['demands'])
        self.assertFalse(any(t.name.startswith('global-prepare-drive') for t in threading.enumerate()))

    def test_stop_during_inflight_creation_records_returned_id_and_stops_next_effect(self):
        rid = self.route(); original = self.google.create_document_once; calls = []
        def create(folder, title):
            result = original(folder, title); calls.append(result)
            private_write(self.bridge.root/'stop-requested.json', canonical({'stopped': True}))
            return result
        with patch.object(self.google, 'create_document_once', side_effect=create):
            with self.assertRaisesRegex(ProtocolError, 'stopped|cancelled'):
                self.bridge.prepare_child(rid)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.active()['control_document_id'], calls[0])
        self.assertEqual(self.reservation('create-control')['document_id'], calls[0])
        self.assertFalse(self.google.files)

    def test_paused_docs_fences_queued_drive_effects_and_preserves_inflight_id(self):
        rid = self.route(); original = self.google.create_document_once; calls = []
        def create(folder, title):
            result = original(folder, title); calls.append(result)
            self.store.pause_docs_reads(self.generation, 'synthetic-pause')
            return result
        with patch.object(self.google, 'create_document_once', side_effect=create):
            with self.assertRaisesRegex(ProtocolError, 'recovery_pending|cancelled'):
                self.bridge.prepare_child(rid)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.active()['control_document_id'], calls[0])
        self.assertEqual(self.reservation('create-control')['document_id'], calls[0])
        self.assertTrue(self.store.status()['docs_read_paused'])
        self.assertFalse(self.google.files)

    def test_late_creation_preserves_id_but_cannot_advance_after_expiry(self):
        rid = self.route(); original = self.google.create_document_once; calls = []
        def create(folder, title):
            result = original(folder, title); calls.append(result)
            self.now += self.bridge.bootstrap_seconds
            return result
        with patch.object(self.google, 'create_document_once', side_effect=create):
            with self.assertRaisesRegex(ProtocolError, 'expired|cancelled'):
                self.bridge.prepare_child(rid)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.active()['control_document_id'], calls[0])
        self.assertEqual(self.active()['stage'], 'PREPARING')
        self.assertNotIn(rid, self.bridge.read().state['logical']['demands'])
        self.assertFalse(self.google.files)

    def test_serial_default_has_no_worker_port_access(self):
        rid = self.route(parallel=False); owner = threading.get_ident()
        original = self.google.create_document_once
        def create(folder, title):
            self.assertEqual(threading.get_ident(), owner)
            return original(folder, title)
        with patch.object(self.google, 'create_document_once', side_effect=create):
            self.bridge.prepare_child(rid)
        self.assertEqual(self.active()['stage'], 'WAITING_FOR_WORKER')


if __name__ == '__main__':
    unittest.main()
