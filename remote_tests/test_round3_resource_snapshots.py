"""Exact same-call resource reuse; synthetic ports only, never provider calls."""
import copy
import json
import unittest
from unittest.mock import patch
from remote_tests import test_global_preparation_fast as fixture
from remote_transport import router_mac as mac, router_bootstrap as child, global_control as queue
from remote_transport.model import ProtocolError


class ResourceSnapshotTests(unittest.TestCase):
    setUp = fixture.PreparationFastTests.setUp
    advance = fixture.PreparationFastTests.advance
    plan = fixture.PreparationFastTests.plan
    event = fixture.PreparationFastTests.event
    join = fixture.PreparationFastTests.join
    route = fixture.PreparationFastTests.route
    active = fixture.PreparationFastTests.active
    reservation = fixture.PreparationFastTests.reservation
    admitted_child = fixture.PreparationFastTests.admitted_child

    def test_parallel_blank_read_is_reused_but_actual_initialization_readback_remains(self):
        self._assert_two_bootstrap_reads(True)

    def test_serial_blank_read_is_reused_but_actual_initialization_readback_remains(self):
        self._assert_two_bootstrap_reads(False)

    def _assert_two_bootstrap_reads(self, parallel):
        rid = self.route(parallel=parallel)
        with patch.object(self.docs, 'get_document', wraps=self.docs.get_document) as reads:
            self.bridge.prepare_child(rid)
        did = self.active()['bootstrap_document_id']
        self.assertEqual(len([c for c in reads.call_args_list if c.args[0] == did]), 2)
        self.assertEqual(self.reservation('initialize-bootstrap')['status'], 'verified')

    def test_intervening_revision_burns_initialization_without_refresh_and_retry(self):
        rid = self.route(parallel=False)
        original = mac._initialize_bootstrap
        def changed(docs, did, tab, state, runtime, **kwargs):
            self.assertIsNotNone(kwargs['source_resource'])
            self.google.docs[did][1] += 1
            return original(docs, did, tab, state, runtime, **kwargs)
        with patch.object(mac, '_initialize_bootstrap', side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, 'initialization_outcome_unknown'):
                self.bridge.prepare_child(rid)
        self.assertEqual(self.reservation('initialize-bootstrap')['status'], 'unknown')
        with self.assertRaisesRegex(ProtocolError, 'runtime_exists'):
            self.bridge.prepare_child(rid)
        self.assertEqual(self.active()['stage'], 'PREPARING')

    def test_wrong_captured_document_rejected_before_initialization_reservation(self):
        rid = self.route(parallel=False); original = mac._initialize_bootstrap
        def wrong(docs, did, tab, state, runtime, **kwargs):
            resource = copy.deepcopy(kwargs['source_resource']); resource['documentId'] = 'wrong-document'
            return original(docs, did, tab, state, runtime, source_resource=resource)
        with patch.object(mac, '_initialize_bootstrap', side_effect=wrong):
            with self.assertRaisesRegex(ProtocolError, 'document_mismatch'):
                self.bridge.prepare_child(rid)
        self.assertFalse((self.runtime/'initialize-bootstrap.json').exists())

    def test_same_call_bundle_snapshot_keeps_only_initial_and_exact_cas_readback_gets(self):
        rid, state = self.admitted_child()
        with patch.object(self.docs, 'get_document', wraps=self.docs.get_document) as reads:
            result = self.bridge.advance_child(rid)
        self.assertEqual(result['state'], 'await_child_polling')
        did = state['bootstrap_document_id']
        self.assertEqual(len([c for c in reads.call_args_list if c.args[0] == did]), 2)

    def test_expiry_after_resource_returns_cannot_initialize_bootstrap(self):
        rid = self.route(parallel=False); original = self.bridge._prepare_resources
        def expired(*args, **kwargs):
            result = original(*args, **kwargs)
            self.now += self.bridge.bootstrap_seconds
            return result
        with patch.object(self.bridge, '_prepare_resources', side_effect=expired):
            with self.assertRaisesRegex(ProtocolError, 'preparation_expired'):
                self.bridge.prepare_child(rid)
        self.assertFalse((self.runtime/'initialize-bootstrap.json').exists())


    def polling_child(self):
        rid, state = self.admitted_child()
        self.bridge.advance_child(rid)
        cc = queue.child_code(self.code, self.generation, rid)
        did = state['bootstrap_document_id']
        snap = child.snapshot_from_document(self.google.get_document(did), did, state['bootstrap_tab_id'])
        polling = child.worker_polling(snap.state, join_code=cc,
            native_task_id=snap.state['worker']['native_task_id'], runtime_hash='a'*64)
        self.google.batch_update_document(**child.plan(snap, polling, join_code=cc)['tool_arguments'])
        return rid, state

    def test_consume_then_ready_reuses_exact_queue_readback(self):
        rid, state = self.polling_child()
        with patch.object(self.docs, 'get_document', wraps=self.docs.get_document) as reads:
            self.assertEqual(self.bridge.advance_child(rid)['state'], 'ready')
        queue_id = self.initial['document_id']
        self.assertEqual(len([c for c in reads.call_args_list if c.args[0] == queue_id]), 3)
        active = self.active()
        snap = child.snapshot_from_document(self.google.get_document(active['bootstrap_document_id']),
            active['bootstrap_document_id'], active['bootstrap_tab_id'])
        self.assertEqual(snap.state['stage'], 'CONSUMED')
        self.assertEqual(self.bridge.read().state['logical']['demands'][rid]['ready']['bootstrap_epoch'], snap.state['epoch'])

    def test_unknown_consumption_prevents_global_ready_and_facade_attachment(self):
        rid, state = self.polling_child()
        with patch.object(mac, '_bootstrap_cas', side_effect=RuntimeError('synthetic unknown consume')):
            with self.assertRaisesRegex(RuntimeError, 'unknown consume'):
                self.bridge.advance_child(rid)
        self.assertEqual(self.bridge.read().state['logical']['demands'][rid]['state'], 'admitted')
        self.assertNotIn(rid, self.bridge.facades)

    def test_ready_conflict_after_consumption_stays_burned_without_attachment(self):
        rid, state = self.polling_child(); original = self.docs.batch_update_document
        queue_id = self.initial['document_id']; attempts = []
        def conflict(*args, **kwargs):
            did = args[0] if args else kwargs['document_id']
            if did == queue_id:
                attempts.append(did)
                self.google.docs[did][1] += 1
            return original(*args, **kwargs)
        with patch.object(self.docs, 'batch_update_document', side_effect=conflict):
            with self.assertRaisesRegex(ProtocolError, 'unknown_no_retry'):
                self.bridge.advance_child(rid)
        self.assertEqual(len(attempts), 1)
        self.assertNotIn(rid, self.bridge.facades)
        with self.assertRaises(FileExistsError):
            self.bridge.advance_child(rid)
        self.assertEqual(len(attempts), 1)


if __name__ == '__main__':
    unittest.main()
