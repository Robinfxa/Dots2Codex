"""Global child bounded pairing, real local helpers and synthetic providers only."""
import copy
import json
import io
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from remote_tests import test_global_handoff as handoff_fixtures
from remote_transport import global_control as queue
from remote_transport import router_bootstrap as bootstrap
from remote_transport.global_gateway import private_write
from remote_transport.model import ProtocolError, canonical, hash_bytes
from remote_transport.router_join import JoinLedger
from remote_transport.router_pairing import Pairing, emit_cell, main as pairing_main


class RouterPairingTests(unittest.TestCase):
    def setUp(self):
        self.h = handoff_fixtures.GlobalHandoffTests('test_child_consumption_requires_import_and_preserves_empty_directory')
        self.h.setUp(); self.addCleanup(self.h.doCleanups)
        self.h.imported(); self.f = self.h.f
        self.p = Pairing(self.h.ref, self.h.native, self.h.receipt_path)
        self.b = self.p.bootstrap

    def file(self, value):
        path = self.f.path('pairing-evidence.json')
        private_write(path, value if isinstance(value, bytes) else canonical(value))
        return str(path)

    def snapshot(self):
        return self.file(self.f.google.get_document(self.b['bootstrap_document_id']))

    def metadata(self, fid, name):
        return self.file({'id': fid, 'title': name, 'mime_type': 'application/json', 'parent_ids': ['folder']})

    def prepared(self):
        prepared = self.p.prepare(self.snapshot()); probe = prepared['probe']
        fid = self.f.google.generate_id()
        raw = Path(probe['probe_file']).read_bytes()
        self.f.google.create_bytes('folder', probe['file_name'], raw, fid)
        self.evidence = {'forward_metadata': self.metadata(self.b['forward_probe']['file_id'], self.b['forward_probe']['name']),
            'forward_raw': self.file(self.f.google.get_bytes(self.b['forward_probe']['file_id'], 10000)),
            'upload_response': self.file({'success': True, 'id': fid}),
            'reverse_metadata': self.metadata(fid, probe['file_name']), 'reverse_raw': self.file(raw)}
        return prepared

    def admitted(self, advance=True, verify=True):
        self.prepared()
        plan = self.p.plan_admit(self.snapshot(), **self.evidence)
        self.p.reserve_write(plan['plan_file'])
        response = self.f.google.batch_update_document(**plan['tool_arguments'])
        if advance: self.f.bridge.advance_child(self.h.rid)
        snapshot = self.snapshot()
        if verify: self.p.verify(plan['plan_file'], snapshot, self.file(response))
        return plan, snapshot

    def control(self):
        return self.file(self.f.google.get_document(self.b['control']['document_id']))

    def ledger(self):
        return JoinLedger(self.p.root, self.b)

    def test_full_immediate_bundle_uses_exact_admit_readback_then_ready_once(self):
        plan, snapshot = self.admitted()
        ready = self.p.ready(snapshot, self.control())
        self.assertEqual(ready['stage'], 'WORKER_POLLING')
        packet = json.loads(Path(ready['plan_file']).read_bytes())
        resource = json.loads(Path(snapshot).read_bytes())
        self.assertEqual(packet['tool_arguments']['write_control']['requiredRevisionId'], resource['revisionId'])
        self.p.reserve_write(ready['plan_file'])
        result = self.f.google.batch_update_document(**ready['tool_arguments'])
        self.f.bridge.advance_child(self.h.rid)
        verified = self.p.verify(ready['plan_file'], self.snapshot(), self.file(result))
        self.assertEqual(verified['stage'], 'CONSUMED')
        saved = json.loads(self.ledger().path.read_bytes())
        self.assertTrue(saved['operations']['admit']['pairing_cell_dispatched'])
        self.assertTrue(saved['operations']['ready']['pairing_cell_dispatched'])
        self.assertEqual(saved['reverse_probe']['sha256'], saved['probe']['sha256'])
        self.assertEqual(saved['materialization']['execution_mode'], 'router_parallel_cells_v1')
        self.assertEqual(self.f.store.status()['production_ready'], False)

    def test_wait_is_explicit_readonly_no_implicit_poll_or_new_plan(self):
        _, snapshot = self.admitted(advance=False)
        result = self.p.ready(snapshot)
        self.assertEqual(result['action'], 'wait_for_bundle')
        self.assertFalse(result['retry_writes'])
        saved = json.loads(self.ledger().path.read_bytes())
        self.assertNotIn('ready', saved['operations']); self.assertIsNone(saved['materialization'])
        self.f.bridge.advance_child(self.h.rid)
        self.assertEqual(self.p.ready(self.snapshot(), self.control())['stage'], 'WORKER_POLLING')

    def test_unresolved_admit_never_materializes_or_emits_ready(self):
        _, snapshot = self.admitted(verify=False)
        with self.assertRaisesRegex(ProtocolError, 'worker_admission_operation_unverified'):
            self.p.ready(snapshot, self.control())
        self.assertFalse(self.p.runtime.exists())
        self.assertNotIn('ready', json.loads(self.ledger().path.read_bytes())['operations'])

    def test_probe_reservation_prevents_second_upload_preparation(self):
        self.prepared()
        with self.assertRaisesRegex(ProtocolError, 'router_probe_already_prepared_no_replay'):
            self.p.prepare(self.snapshot())

    def test_forward_mismatch_prevents_admit_even_with_reverse_success(self):
        self.prepared(); self.evidence['forward_raw'] = self.file(b'{}')
        with self.assertRaisesRegex(ProtocolError, 'forward_mac_to_connector_raw_mismatch'):
            self.p.plan_admit(self.snapshot(), **self.evidence)
        self.assertEqual(json.loads(self.ledger().path.read_bytes())['operations'], {})

    def test_reverse_raw_metadata_and_upload_must_all_match(self):
        self.prepared(); original = dict(self.evidence)
        for field, bad in [('reverse_raw', self.file(b'{}')),
                           ('reverse_metadata', self.metadata('wrong', 'wrong')),
                           ('upload_response', self.file({'id': 'guessed'}))]:
            with self.subTest(field=field), self.assertRaises(ProtocolError):
                self.p.plan_admit(self.snapshot(), **{**original, field: bad})
            self.assertEqual(json.loads(self.ledger().path.read_bytes())['operations'], {})

    def test_handoff_expiry_after_probes_before_plan_prevents_admission(self):
        self.prepared()
        with patch('time.time', return_value=self.p.value['expires']):
            with self.assertRaises(ProtocolError): self.p.plan_admit(self.snapshot(), **self.evidence)
        self.assertEqual(json.loads(self.ledger().path.read_bytes())['operations'], {})

    def test_source_change_after_probe_and_before_cas_prevents_progress(self):
        self.prepared()
        sources = queue.controller_source_hashes()
        changed = dict(sources); changed['remote_transport/router_join.py'] = '0' * 64
        with patch.object(queue, 'controller_source_hashes', return_value=changed):
            with self.assertRaisesRegex(ProtocolError, 'source_binding_mismatch'):
                self.p.plan_admit(self.snapshot(), **self.evidence)
        plan = self.p.plan_admit(self.snapshot(), **self.evidence)
        with patch.object(queue, 'controller_source_hashes', return_value=changed):
            with self.assertRaisesRegex(ProtocolError, 'source_binding_mismatch'):
                self.p.reserve_write(plan['plan_file'])
        self.assertFalse(json.loads(self.ledger().path.read_bytes())['operations']['admit'].get('pairing_cell_dispatched'))

    def test_dispatch_is_one_use_and_exact_original_packet_required(self):
        self.prepared(); plan = self.p.plan_admit(self.snapshot(), **self.evidence)
        raw = Path(plan['plan_file']).read_bytes(); changed = json.loads(raw)
        changed['tool_arguments']['write_control']['requiredRevisionId'] = 'wrong'
        private_write(Path(plan['plan_file']), canonical(changed))
        with self.assertRaisesRegex(ProtocolError, 'no_replay'): self.p.reserve_write(plan['plan_file'])
        private_write(Path(plan['plan_file']), raw)
        self.p.reserve_write(plan['plan_file'])
        with self.assertRaisesRegex(ProtocolError, 'no_replay'): self.p.reserve_write(plan['plan_file'])

    def test_expiry_at_dispatch_burns_no_network_permission(self):
        self.prepared(); plan = self.p.plan_admit(self.snapshot(), **self.evidence)
        with patch('time.time', return_value=self.p.value['expires']):
            with self.assertRaises(ProtocolError): self.p.reserve_write(plan['plan_file'])
        self.assertFalse(json.loads(self.ledger().path.read_bytes())['operations']['admit'].get('pairing_cell_dispatched'))

    def test_lost_admit_response_reconciles_exact_signed_advanced_readback(self):
        self.prepared(); plan = self.p.plan_admit(self.snapshot(), **self.evidence)
        self.p.reserve_write(plan['plan_file']); self.f.google.batch_update_document(**plan['tool_arguments'])
        self.f.bridge.advance_child(self.h.rid)
        verified = self.p.verify(plan['plan_file'], self.snapshot())
        self.assertEqual(verified['stage'], 'BUNDLE_READY'); self.assertTrue(verified['reconciled_from_event'])

    def test_same_stage_competing_event_does_not_verify_or_materialize(self):
        self.prepared(); plan = self.p.plan_admit(self.snapshot(), **self.evidence)
        packet = json.loads(Path(plan['plan_file']).read_bytes())
        own = packet['expected_state']; alternate = bootstrap.worker_admitted(
            self.f.bridge.read().state['logical']['demands'][self.h.rid]['child_bootstrap'],
            join_code=self.p.value['descriptor']['join_code'], native_task_id=self.h.native,
            admission=own['worker']['admission'], probe=own['worker']['probe'])
        document = self.f.google.get_document(self.b['bootstrap_document_id'])
        source = bootstrap.snapshot_from_document(document, self.b['bootstrap_document_id'], self.b['bootstrap_tab_id'])
        other = bootstrap.plan(source, alternate, join_code=self.p.value['descriptor']['join_code'])
        self.f.google.batch_update_document(**other['tool_arguments'])
        with self.assertRaisesRegex(ProtocolError, 'no_replay'): self.p.verify(plan['plan_file'], self.snapshot())
        with self.assertRaisesRegex(ProtocolError, 'worker_admission_operation_unverified'): self.p.ready(self.snapshot())

    def test_emission_is_offline_readonly_secret_free_and_requires_import(self):
        before = self.ledger().path.read_bytes()
        destination = self.f.path('pairing-cell.js')
        out = emit_cell(self.p, 'pair', destination)
        self.assertEqual(out['connector_calls'], 0); self.assertFalse(out['executes_on_emission'])
        self.assertEqual(out['sha256'], hash_bytes(destination.read_bytes()))
        self.assertEqual(before, self.ledger().path.read_bytes()); self.assertFalse(self.p.code_path.exists())
        text = destination.read_text()
        self.assertNotIn(self.f.code, text); self.assertNotIn(self.p.value['descriptor']['join_code'], text)
        self.assertIn('pairOnce()', text)
        checked = subprocess.run(['node', '--check', str(destination)], capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        saved = json.loads(before); del saved['native_admission']; private_write(self.ledger().path, canonical(saved))
        with self.assertRaisesRegex(ProtocolError, 'verified_parent_child_import_required'):
            Pairing(self.h.ref, self.h.native, self.h.receipt_path)

    def run_native_adapter(self, **options):
        config = {**self.p.context(), 'handoff': self.h.ref, 'admissionReceipt': str(self.h.receipt_path)}
        fid = self.b['forward_probe']['file_id']
        scenario = {'config': config, 'directory': str(self.p.root), 'forwardId': fid,
            'documents': {did: self.f.google.get_document(did) for did in
                (self.b['bootstrap_document_id'], self.b['control']['document_id'])},
            'files': {fid: {'id': fid, 'path': self.file(self.f.google.get_bytes(fid, 10000)),
                'name': self.b['forward_probe']['name'], 'parents': ['folder']}}}
        scenario.update(options)
        if options.get('largeInput'):
            for document in scenario['documents'].values(): document['title'] = "漢'" * 20000
        executed = subprocess.run(['node', str(self.f.package / 'native_connector/router_pairing_helper_fixture.js'),
            self.file(scenario)], cwd=self.f.package, text=True, capture_output=True)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        result = json.loads(executed.stdout)
        if options.get('expectFailure'): return result
        self.assertTrue(result['outcome']['ok'], result)
        self.assertEqual(result['outcome']['action'], 'paired')
        self.assertEqual(result['counts']['docs'], 4)
        self.assertEqual(result['counts']['upload'], 1)
        self.assertEqual(result['counts']['cas'], 2)
        for key in ('metadata', 'fetch', 'download'): self.assertEqual(result['counts'][key], 2)
        saved = json.loads(self.ledger().path.read_bytes())
        self.assertEqual(saved['operations']['ready']['status'], 'verified')
        self.assertEqual(saved['materialization']['execution_mode'], 'router_parallel_cells_v1')
        return result

    def test_native_adapter_runs_real_local_helpers_with_synthetic_raw_connectors(self):
        result = self.run_native_adapter()
        self.assertEqual(result['operations'], {'context': 1, 'prepare': 1, 'plan-admit': 1,
                                              'verify': 1, 'verify-ready': 1})

    def test_truncated_wide_output_recovers_saved_result_without_reissuing_plan(self):
        result = self.run_native_adapter(truncateResult=True)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['operations']['plan-admit'], 1)
        self.assertEqual(result['operations']['verify-ready'], 1)
        self.assertEqual(result['counts']['cas'], 2)

    def test_large_quote_unicode_docs_use_original_chunked_capture_fallback(self):
        result = self.run_native_adapter(largeInput=True)
        self.assertEqual(result['operations']['prepare'], 1)
        self.assertEqual(result['operations']['verify'], 1)

    def test_probe_branch_failure_drains_and_persists_upload_response_without_replay(self):
        result = self.run_native_adapter(forwardFailure=True, repeatPair=True, expectFailure=True)
        self.assertFalse(result['outcome']['ok']); self.assertFalse(result['second']['ok'])
        self.assertEqual(result['counts']['upload'], 1); self.assertEqual(result['counts']['cas'], 0)
        captures = [json.loads(p.read_bytes()) for p in self.p.root.glob('capture-*.json')]
        self.assertTrue(any(value.get('structuredContent', {}).get('success') is True for value in captures))
        self.assertTrue(any(value.get('structuredContent', {}).get('file_uri') for value in captures))
        self.assertEqual(json.loads(self.ledger().path.read_bytes())['operations'], {})

    def test_true_downloaded_raw_mismatch_prevents_admission(self):
        result = self.run_native_adapter(rawMismatch=True, expectFailure=True)
        self.assertFalse(result['outcome']['ok']); self.assertEqual(result['counts']['download'], 2)
        self.assertEqual(result['counts']['upload'], 1); self.assertEqual(result['counts']['cas'], 0)
        self.assertEqual(json.loads(self.ledger().path.read_bytes())['operations'], {})

    def test_wrong_raw_file_id_is_rejected_by_reused_adapter_validator(self):
        result = self.run_native_adapter(wrongRawId=True, expectFailure=True)
        self.assertFalse(result['outcome']['ok']); self.assertEqual(result['counts']['download'], 1)
        self.assertEqual(result['counts']['upload'], 1); self.assertEqual(result['counts']['cas'], 0)

    def test_same_invocation_snapshot_revision_conflict_is_one_cas_no_overwrite_or_retry(self):
        result = self.run_native_adapter(revisionConflict=True, repeatPair=True, expectFailure=True)
        self.assertFalse(result['outcome']['ok']); self.assertFalse(result['second']['ok'])
        self.assertEqual(result['counts']['cas'], 1); self.assertEqual(result['counts']['rejectedCas'], 1)
        self.assertEqual(result['counts']['upload'], 1); self.assertTrue(result['conflictUnchanged'])
        self.assertFalse(self.p.runtime.exists())

    def test_authenticated_abort_during_probes_is_never_overwritten_by_reused_snapshot(self):
        result = self.run_native_adapter(revisionConflict=True, abortDuringProbes=True,
                                         repeatPair=True, expectFailure=True)
        self.assertFalse(result['outcome']['ok']); self.assertFalse(result['second']['ok'])
        self.assertEqual(result['counts']['cas'], 1); self.assertEqual(result['counts']['rejectedCas'], 1)
        self.assertEqual(result['counts']['upload'], 1); self.assertTrue(result['conflictUnchanged'])
        self.assertFalse(self.p.runtime.exists())

    def test_untrusted_bundle_hint_only_reads_fixed_control_before_full_rejection(self):
        result = self.run_native_adapter(forgeBundleHint=True, advance=False, expectFailure=True)
        self.assertFalse(result['outcome']['ok'])
        self.assertEqual(result['counts']['cas'], 1)
        self.assertIn(self.b['control']['document_id'], result['readIds'])
        self.assertNotIn('unapproved-hint-destination', result['readIds'])
        self.assertFalse(self.p.runtime.exists())
        self.assertNotIn('ready', json.loads(self.ledger().path.read_bytes())['operations'])

    def test_false_negative_stage_hint_preserves_fully_verified_slow_fallback(self):
        result = self.run_native_adapter(forceHintFalse=True)
        self.assertEqual(result['counts']['helpers'], 6)
        self.assertEqual(result['counts']['docs'], 4)
        self.assertEqual(result['operations']['ready'], 1)
        self.assertNotIn('verify-ready', result['operations'])

    def test_inline_evidence_is_fixed_schema_and_cannot_override_a_path(self):
        base = ['prepare', '--handoff-file', self.h.ref['path'], '--sha256', self.h.ref['sha256'],
                '--native-task-id', self.h.native, '--admission-receipt', str(self.h.receipt_path), '--inline-evidence']
        resource = self.f.google.get_document(self.b['bootstrap_document_id'])
        for envelope in ({'operation': 'spawn'}, {'snapshot': None}, {'snapshot': resource, 'other': resource}):
            with patch('sys.stdin', io.TextIOWrapper(io.BytesIO(canonical(envelope)))), self.assertRaises(ProtocolError):
                pairing_main(base)
        with patch('sys.stdin', io.TextIOWrapper(io.BytesIO(canonical({'snapshot': resource})))), self.assertRaises(ProtocolError):
            pairing_main(base + ['--snapshot', self.snapshot()])
        with patch('sys.stdin', io.TextIOWrapper(io.BytesIO(canonical({'snapshot': resource})))):
            result = pairing_main(base)
        captured = result['evidence_files']['snapshot']
        self.assertEqual(json.loads(Path(captured).read_bytes()), resource)
        self.assertTrue(Path(result['probe']['probe_file']).exists())

    def test_closed_or_misbound_control_and_tampered_runtime_cannot_emit_ready(self):
        _, snapshot = self.admitted()
        other = self.file(self.f.google.get_document(self.f.initial['document_id']))
        with self.assertRaises(ProtocolError): self.p.ready(snapshot, other)
        # Materialization was reserved once; the same complete runtime is allowed
        # to resume, but changed raw config is never silently replaced.
        config = json.loads((self.p.runtime / 'config.json').read_bytes()); config['folder_id'] = 'other'
        private_write(self.p.runtime / 'config.json', canonical(config))
        with self.assertRaisesRegex(ProtocolError, 'raw_bundle_mismatch'):
            self.p.ready(snapshot, self.control())


if __name__ == '__main__': unittest.main()
