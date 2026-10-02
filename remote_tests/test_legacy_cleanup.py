"""Retired-v1 stop -> explicit Settings migration, using predecessor-made evidence.

All IDs, documents, keys, processes and credentials are isolated synthetic data.
No native admission, provider calls, live queue writes or retired catalog imports.
"""
import copy
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from remote_transport import legacy_cleanup as cleanup
from remote_transport import router_bootstrap as bootstrap
from remote_transport import router_mac as router
from remote_transport import control
from remote_transport.model import Object, ProtocolError, canonical, hash_bytes, deployment
from remote_transport.selection import (load_catalog, select, spawn_arguments, admission_receipt,
                                        validate_selection, validate_admission)
from remote_tests.test_mac_launcher import LauncherFixture
from remote_tests.test_router_lifecycle import FakeDocs, document


FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'retired_v1_sessions.json').read_bytes())


class Documents:
    def __init__(self, fixture):
        self.docs = {
            'control': FakeDocs(cleanup._control_block(fixture['control'])),
            'bootstrap': FakeDocs(cleanup._bootstrap_block(fixture['bootstrap_consumed']), did='bootstrap')}

    def get_document(self, did):
        return self.docs[did].get_document(did)

    def batch_update_document(self, did, requests, wc):
        return self.docs[did].batch_update_document(did, requests, wc)


class LegacyCleanupTests(LauncherFixture):
    def prepare(self, effort='max'):
        self.configure()
        self.fixture = copy.deepcopy(FIXTURE['sessions'][effort])
        config = self.launcher.load()
        config['model_selection'] = self.fixture['selection']
        self.catalog = self.root / 'retired-catalog.json'
        # Stop must not import/normalize an old external catalog file.
        self.catalog.write_bytes(b'SYNTHETIC_RETIRED_CATALOG_PRESERVE')
        config['catalog'] = str(self.catalog)
        router._private_json(self.config, config)
        self.runtime = self.root / 'router-session'
        self.runtime.mkdir(mode=0o700)
        self.pin_path = self.runtime / 'pin.json'
        router.write_new(self.pin_path, canonical(self.fixture['pin']))
        self.pairing_path = self.runtime / 'private-pairing.json'
        router._private_json(self.pairing_path, {'join_code': FIXTURE['join_code']})
        self.journal = self.runtime / 'controller-journal.json'
        router._private_json(self.journal, {'synthetic_evidence': 'must survive'})
        self.active_value = {'contract': 'dots-router-active/2', 'session_id': 'router-session',
            'stage': 'READY', 'closed': False, 'process_stopped': False, 'runtime': str(self.runtime),
            'pin_file': str(self.pin_path), 'deployment': self.fixture['pin']['object_id'],
            'control_document_id': 'control', 'control_tab_id': 't.0', 'control_id': 'cid',
            'bootstrap_document_id': 'bootstrap', 'bootstrap_tab_id': 't.0',
            'bootstrap_root': bootstrap.root_context(self.fixture['bootstrap_consumed']),
            'model_selection': self.fixture['selection'], 'control_initialization_attempted': True,
            'facade_pid': None, 'facade_identity': None}
        router._private_json(self.active, self.active_value)
        self.docs = Documents(self.fixture)
        self.args.model = self.args.effort = None
        self.original = {p: p.read_bytes() for p in
            (self.pin_path, self.pairing_path, self.journal, self.catalog, self.config, self.credential)}
        return config

    def stop(self):
        with patch.object(router, '_set_google_env'), \
             patch('examples.google_clients.create_docs_client', return_value=self.docs) as factory:
            outcome = router.stop(self.args)
        return outcome, factory

    def assert_preserved(self, *, include_config=True):
        for path, raw in self.original.items():
            if include_config or path != self.config:
                self.assertEqual(path.read_bytes(), raw, str(path))

    def test_retired_max_stop_then_explicit_settings_xhigh(self):
        self.prepare('max')
        self.assertTrue(self.launcher.active_blocks())
        with self.assertRaisesRegex(ProtocolError, 'stop_before_settings'):
            self.configure()
        result, factory = self.stop()
        factory.assert_called_once()
        self.assertEqual(result['stage'], 'CLOSED')
        self.assertTrue(result['control_close']['authoritative'])
        self.assertEqual(result['bootstrap_cleanup']['stage'], 'CLOSED')
        self.assertFalse(result['evidence_deleted'])
        self.assertFalse(self.launcher.active_blocks())
        self.assert_preserved()
        closed = cleanup._decode_control(self.docs.docs['control'].text)
        self.assertEqual(closed['binding'], self.fixture['control']['binding'])
        self.assertEqual(closed['operations'][-1]['kind'], 'close')
        self.assertEqual(closed['binding']['selection']['reasoning_effort'], 'max')
        boot = cleanup._decode_bootstrap(self.docs.docs['bootstrap'].text)
        self.assertEqual(boot['events'][:-1], self.fixture['bootstrap_consumed']['events'])
        self.assertEqual(boot['root_mac'], self.fixture['bootstrap_consumed']['root_mac'])
        saved = self.configure()
        self.assertEqual(saved['effort'], 'xhigh')
        self.assertEqual(self.launcher.load()['model_selection'], select(load_catalog(), 'gpt-6.1-sol', 'xhigh'))
        self.assert_preserved(include_config=False)

    def test_retired_xhigh_also_closes_without_relabeling_catalog(self):
        self.prepare('xhigh')
        result, _ = self.stop()
        self.assertEqual(result['stage'], 'CLOSED')
        self.assert_preserved()
        self.configure()
        self.assertEqual(self.launcher.load()['model_selection'], select(load_catalog(), 'gpt-6.1-sol', 'xhigh'))
        self.assert_preserved(include_config=False)

    def test_retired_evidence_never_becomes_valid_runtime_or_admission_input(self):
        self.prepare()
        selected = self.fixture['selection']
        admission = self.fixture['pin']['body']['payload']['inference']['admission']
        actions = [lambda: router._load_config(self.config),
            lambda: validate_selection(selected), lambda: spawn_arguments(selected, 'worker', 'fixture'),
            lambda: validate_admission(admission, selected, '/root/synthetic_worker'),
            lambda: admission_receipt(selected, {}, '/root/synthetic_worker'),
            lambda: Object.parse(self.pin_path.read_bytes()),
            lambda: control.validate_state(self.fixture['control']),
            lambda: bootstrap.validate_state(self.fixture['bootstrap_consumed']),
            lambda: deployment('new', '/root/synthetic_worker', inference={'selection': selected, 'admission': admission}),
            lambda: router._start_locked(self.args, self.root / "other-active.json", "fixture_intent")]
        for index, action in enumerate(actions):
            with self.subTest(index=index), self.assertRaises(ProtocolError):
                action()
        self.assert_preserved()

    def test_unknown_or_malformed_config_stops_locally_but_cannot_unlock_settings(self):
        self.prepare()
        original_config = json.loads(self.config.read_bytes())
        for change in ({'catalog_sha256': 'f' * 64}, {'catalog_version': 'unknown'},
                       {'model': 'invented'}, {'reasoning_effort': 'ultra'}, {'extra': True}):
            with self.subTest(change=change):
                config = copy.deepcopy(original_config)
                config['model_selection'].update(change)
                router._private_json(self.config, config)
                self.original[self.config] = self.config.read_bytes()
                result, factory = self.stop()
                factory.assert_not_called()
                self.assertEqual(result['stage'], 'RECOVERY_REQUIRED')
                self.assertFalse(result['closed'])
                self.assertTrue(result['process_stopped'])
                self.assertTrue(self.launcher.active_blocks())
                self.assertEqual(sum(d.calls for d in self.docs.docs.values()), 0)
                self.assert_preserved()

    def test_tampered_original_pin_or_binding_is_rejected_without_close_write(self):
        config = self.prepare()
        for mutation in ('hash', 'selection', 'admission', 'control'):
            with self.subTest(mutation=mutation):
                pin = copy.deepcopy(self.fixture['pin'])
                self.docs = Documents(self.fixture)
                if mutation == 'hash':
                    pin['object_id'] = 'f' * 64
                elif mutation == 'selection':
                    pin['body']['payload']['inference']['selection']['catalog_sha256'] = 'f' * 64
                    pin['object_id'] = hash_bytes(canonical(pin['body']))
                elif mutation == 'admission':
                    pin['body']['payload']['inference']['admission']['submitted_reasoning_effort'] = 'high'
                    pin['object_id'] = hash_bytes(canonical(pin['body']))
                else:
                    changed = copy.deepcopy(self.fixture['control'])
                    changed['binding']['deployment_hash'] = 'f' * 64
                    self.docs.docs['control'].text = cleanup._control_block(changed)
                router._private_json(self.pin_path, pin)
                with self.assertRaises(ProtocolError):
                    router._close_control(self.docs, self.active_value, config)
                self.assertEqual(self.docs.docs['control'].calls, 0)

    def test_ambiguous_control_close_never_replays_and_preserves_evidence(self):
        self.prepare()
        self.docs.docs['control'].mode = 'lost_absent'
        first, _ = self.stop()
        second, _ = self.stop()
        self.assertEqual(first['stage'], 'RECOVERY_REQUIRED')
        self.assertEqual(second['stage'], 'RECOVERY_REQUIRED')
        self.assertEqual(self.docs.docs['control'].calls, 1)
        self.assertEqual(router._load_private_json(self.runtime / 'control-close.json')['status'], 'unknown')
        self.assertTrue(self.launcher.active_blocks())
        self.assert_preserved()

    def test_applied_close_with_lost_reply_reconciles_without_replay(self):
        self.prepare()
        self.docs.docs['control'].mode = 'lost_applied'
        self.docs.docs['bootstrap'].mode = 'lost_applied'
        first, _ = self.stop()
        second, _ = self.stop()
        self.assertEqual(first['stage'], 'CLOSED')
        self.assertEqual(second['stage'], 'CLOSED')
        self.assertEqual([d.calls for d in self.docs.docs.values()], [1, 1])
        self.assertEqual(first['control_close']['operation_id'], second['control_close']['operation_id'])
        self.assert_preserved()

    def test_rejected_close_needs_a_new_stop_and_keeps_rejection_evidence(self):
        self.prepare()
        self.docs.docs['control'].mode = 'reject'
        first, _ = self.stop()
        self.assertEqual(first['stage'], 'RECOVERY_REQUIRED')
        record = router._load_private_json(self.runtime / 'control-close.json')
        self.assertEqual(record['status'], 'rejected')
        self.assertEqual(self.docs.docs['control'].calls, 1)
        self.docs.docs['control'].mode = 'success'
        second, _ = self.stop()
        self.assertEqual(second['stage'], 'CLOSED')
        archive = self.runtime / ('control-close-rejected-' + record['operation']['id'] + '.json')
        self.assertEqual(router._load_private_json(archive), record)
        self.assertEqual(self.docs.docs['control'].calls, 2)
        self.assert_preserved()

    def test_unverified_local_process_keeps_settings_blocked_after_control_closes(self):
        self.prepare()
        with patch.object(router, '_stop_process', return_value=False):
            outcome, _ = self.stop()
        self.assertTrue(outcome['closed'])
        self.assertEqual(outcome['stage'], 'RECOVERY_REQUIRED')
        self.assertTrue(self.launcher.active_blocks())
        self.assert_preserved()

    def test_bootstrap_unknown_close_never_replays(self):
        self.prepare()
        self.docs.docs['bootstrap'].mode = 'lost_absent'
        for _ in range(2):
            with self.assertRaises(Exception):
                router._finish_bootstrap(self.docs, self.active_value, FIXTURE['join_code'], closed=True)
        self.assertEqual(self.docs.docs['bootstrap'].calls, 1)
        self.assertEqual(router._load_private_json(self.runtime / 'bootstrap-5.json')['status'], 'unknown')
        self.assert_preserved()

    def test_bootstrap_requires_original_signature_source_and_root(self):
        self.prepare()
        state = self.fixture['bootstrap_consumed']
        for changed, did, tab, root in [
            ({**state, 'root_mac': '0' * 64}, 'bootstrap', 't.0', bootstrap.root_context(state)),
            (state, 'other', 't.0', bootstrap.root_context(state)),
            (state, 'bootstrap', 'other', bootstrap.root_context(state)),
            (state, 'bootstrap', 't.0', {**bootstrap.root_context(state), 'folder_id': 'other'})]:
            with self.assertRaises(ProtocolError):
                cleanup.bootstrap_snapshot(document(did, tab, 'r1', cleanup._bootstrap_block(changed)),
                    did, tab, join_code=FIXTURE['join_code'], expected_root=root)

    def test_close_only_store_cannot_admit_rebind_or_change_signed_history(self):
        self.prepare()
        store = cleanup.CloseOnlyControlStore(self.docs, 'control', 't.0', 'cid', 'router-session', 'mac')
        snapshot = store.read()
        state = store.plan_close(snapshot, snapshot.state['binding'], 'close_once')
        for key, value in [('closed', False), ('max_requests', 127), ('session_id', 'other'),
                           ('control_id', 'other')]:
            changed = copy.deepcopy(state); changed[key] = value
            with self.assertRaises(ProtocolError):
                store.compare_and_swap(snapshot, changed)
        changed = copy.deepcopy(state); changed['operations'][-1]['kind'] = 'begin'
        with self.assertRaises(ProtocolError): store.compare_and_swap(snapshot, changed)
        self.assertEqual(self.docs.docs['control'].calls, 0)
        boot = self.fixture['bootstrap_initial']
        snap = cleanup.bootstrap_snapshot(document('bootstrap', 't.0', 'r1', cleanup._bootstrap_block(boot)),
            'bootstrap', 't.0', join_code=FIXTURE['join_code'], expected_root=bootstrap.root_context(boot))
        with self.assertRaises(ProtocolError):
            cleanup.bootstrap_plan(snap, self.fixture['bootstrap_consumed'], join_code=FIXTURE['join_code'])

    def test_expired_pre_admission_bootstrap_can_only_abort(self):
        self.prepare()
        initial = self.fixture['bootstrap_initial']
        self.active_value['bootstrap_root'] = bootstrap.root_context(initial)
        self.docs.docs['bootstrap'].text = cleanup._bootstrap_block(initial)
        result = router._finish_bootstrap(self.docs, self.active_value, FIXTURE['join_code'], closed=False)
        self.assertEqual(result['stage'], 'ABORTED')
        final = cleanup._decode_bootstrap(self.docs.docs['bootstrap'].text)
        self.assertIsNone(final['worker'])
        self.assertEqual(final['required_selection'], self.fixture['selection'])
        self.assertEqual(final['events'][0]['kind'], 'aborted')

    def test_fabricated_snapshots_cannot_redirect_close_to_other_session_or_doc(self):
        self.prepare()
        store = cleanup.CloseOnlyControlStore(self.docs, 'control', 't.0', 'cid', 'router-session', 'mac')
        snapshot = store.read()
        changed = snapshot.state
        changed['control_id'] = 'other_control'
        forged = replace(snapshot, block=cleanup._control_block(changed))
        target = store.plan_close(forged, changed['binding'], 'close_once')
        with self.assertRaisesRegex(ProtocolError, 'control_pin_mismatch'):
            store.compare_and_swap(forged, target)
        self.assertEqual(self.docs.docs['control'].calls, 0)
        state = self.fixture['bootstrap_consumed']
        snap = cleanup.bootstrap_snapshot(self.docs.get_document('bootstrap'), 'bootstrap', 't.0',
            join_code=FIXTURE['join_code'], expected_root=bootstrap.root_context(state))
        target = cleanup.finish_bootstrap_state(state, join_code=FIXTURE['join_code'], closed=True)
        for forged in (replace(snap, document_id='other'), replace(snap, tab_id='other')):
            with self.assertRaisesRegex(ProtocolError, 'source_identity_mismatch'):
                cleanup.bootstrap_plan(forged, target, join_code=FIXTURE['join_code'])


if __name__ == '__main__':
    unittest.main()
