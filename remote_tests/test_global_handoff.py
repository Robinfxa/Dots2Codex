"""Compact native handoff adversarial checks; no native or external calls."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from remote_tests import test_global_control as fixtures
from remote_transport import global_handoff as handoff
from remote_transport import global_control as queue
from remote_transport.global_native import NativeLedger
from remote_transport.global_gateway import private_write
from remote_transport.model import ProtocolError, canonical, hash_bytes
from remote_transport.router_join import JoinLedger


class GlobalHandoffTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.GlobalControlTests('test_join_heartbeat_claim_and_signed_projection')
        self.f.setUp(); self.addCleanup(self.f.tearDown)
        self.f.bridge.bootstrap_seconds = 1800
        self.rid, _, _ = self.f.demand()
        self.f.execute('claim', self.rid); self.f.execute('begin', self.rid)
        self.plan = self.f.path('native.json')
        self.directory = self.f.ledger.child_state_dir(self.rid)
        self.artifact = self.f.ledger.handoff_path(self.rid)

    def prepare(self, package=None):
        self.out = self.f.ledger.plan_spawn(self.f.bridge.read(), self.rid, self.plan, package or self.f.package)
        self.packet = json.loads(self.plan.read_bytes()); self.ref = self.packet['handoff']
        self.native = '/root/offline/' + self.out['arguments']['task_name']
        return self.out

    def record(self):
        self.receipt_path = self.f.path('receipt.json')
        return self.f.ledger.record_spawn(self.plan, self.out['arguments'], {'task_name': self.native}, self.receipt_path)

    def imported(self):
        self.prepare(); self.record(); self.f.execute('admitted', self.rid)
        return self.f.ledger.import_child_admission(self.f.bridge.read(), self.rid, self.receipt_path, self.native)

    def corrupt(self, raw=None):
        raw = self.artifact.read_bytes() + b' ' if raw is None else raw
        self.artifact.chmod(0o600); self.artifact.write_bytes(raw); self.artifact.chmod(0o400)

    def test_compact_exact_arguments_private_artifact_and_separate_empty_child_directory(self):
        self.prepare(); value = handoff.read(self.ref); args = self.out['arguments']
        self.assertEqual(args, handoff.arguments(value, self.ref))
        self.assertEqual(args['fork_turns'], 'none')
        self.assertEqual(args['model'], value['selection']['model'])
        self.assertEqual(args['reasoning_effort'], value['selection']['reasoning_effort'])
        self.assertIn(self.ref['sha256'], args['message']); self.assertIn(str(self.artifact), args['message'])
        self.assertIn('import-child-admission', args['message'])
        self.assertNotIn(self.f.code, args['message']); self.assertNotIn(value['join_code'], args['message'])
        self.assertNotIn(self.f.code, self.artifact.read_text())
        self.assertLess(len(args['message']), 1600)
        self.assertLess(len(args['message']), len(self.artifact.read_bytes()))
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertNotEqual(self.artifact.parent, self.directory)
        self.assertEqual(self.artifact.stat().st_mode & 0o777, 0o400)
        self.assertIn('remote_transport/global_handoff.py', value['controller_source_hashes'])
        self.assertTrue(self.f.ledger.check_spawn(self.f.bridge.read(), self.plan)['dispatch_allowed'])

    def test_exact_byte_tampering_blocks_check_record_and_import(self):
        self.prepare(); self.corrupt()
        with self.assertRaisesRegex(ProtocolError, 'handoff_bytes_mismatch'):
            self.f.ledger.check_spawn(self.f.bridge.read(), self.plan)
        with self.assertRaisesRegex(ProtocolError, 'handoff_bytes_mismatch'): self.record()
        self.assertEqual(json.loads(self.f.ledger.path.read_bytes())['spawns'][self.rid]['status'], 'reserved_outcome_unknown')

    def test_import_rechecks_handoff_after_actual_result_recorded(self):
        self.prepare(); self.record(); self.f.execute('admitted', self.rid); self.corrupt()
        with self.assertRaisesRegex(ProtocolError, 'handoff_bytes_mismatch'):
            self.f.ledger.import_child_admission(self.f.bridge.read(), self.rid, self.receipt_path, self.native)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_child_consumption_requires_import_and_preserves_empty_directory(self):
        self.prepare(); self.record()
        with self.assertRaisesRegex(ProtocolError, 'verified_parent_child_import_required'):
            handoff.consume(self.ref, self.native, self.receipt_path)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.f.execute('admitted', self.rid)
        self.f.ledger.import_child_admission(self.f.bridge.read(), self.rid, self.receipt_path, self.native)
        before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        out = handoff.consume(self.ref, self.native, self.receipt_path)
        self.assertTrue(out['verified']); self.assertTrue(out['read_only'])
        self.assertEqual(out['descriptor']['join_code'], queue.child_code(self.f.code, self.f.gen, self.rid))
        self.assertEqual(out['descriptor']['state_dir'], str(self.directory))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.directory.iterdir()})
        self.corrupt()
        with self.assertRaisesRegex(ProtocolError, 'handoff_bytes_mismatch'):
            handoff.consume(self.ref, self.native, self.receipt_path)

    def test_child_cli_consumes_exact_imported_artifact_without_global_secret(self):
        self.imported()
        result = subprocess.run([sys.executable, '-B', '-m', 'remote_transport.global_handoff', 'consume',
            '--handoff-file', str(self.artifact), '--sha256', self.ref['sha256'],
            '--native-task-id', self.native, '--admission-receipt', str(self.receipt_path)],
            cwd=self.f.package, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        value = json.loads(result.stdout); self.assertTrue(value['verified'])
        self.assertNotIn(self.f.code, result.stdout)

    def test_wrong_child_receipt_path_and_parent_provenance_rejected(self):
        self.imported()
        with self.assertRaisesRegex(ProtocolError, 'native_admission_binding_mismatch'):
            handoff.consume(self.ref, '/root/different', self.receipt_path)
        clone = self.f.path('receipt-clone'); private_write(clone, self.receipt_path.read_bytes())
        with self.assertRaisesRegex(ProtocolError, 'handoff_import_binding_mismatch'):
            handoff.consume(self.ref, self.native, clone)
        bootstrap = self.f.bridge.read().state['logical']['demands'][self.rid]['child_bootstrap']
        child = JoinLedger(self.directory, bootstrap); saved = json.loads(child.path.read_bytes())
        saved['native_admission']['parent_provenance']['handoff']['sha256'] = '0' * 64
        private_write(child.path, canonical(saved))
        with self.assertRaisesRegex(ProtocolError, 'handoff_import_binding_mismatch'):
            handoff.consume(self.ref, self.native, self.receipt_path)

    def test_symlink_file_parent_and_child_directory_rejected(self):
        self.prepare(); raw = self.artifact.read_bytes()
        target = self.f.path('linked-handoff'); handoff.write_new(target, raw)
        self.artifact.unlink(); self.artifact.symlink_to(target)
        with self.assertRaisesRegex(ProtocolError, 'symlink_path_rejected'): handoff.read(self.ref)
        self.artifact.unlink(); handoff.write_new(self.artifact, raw)
        original = self.artifact.parent; moved = original.with_name('moved-handoffs')
        original.rename(moved); original.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ProtocolError, 'symlink_path_rejected'): handoff.read(self.ref)
        original.unlink(); moved.rename(original)
        self.directory.rmdir(); self.directory.symlink_to(self.f.ledger.root, target_is_directory=True)
        with self.assertRaisesRegex(ProtocolError, 'symlink_path_rejected'):
            self.f.ledger.check_spawn(self.f.bridge.read(), self.plan)

    def test_wrong_path_private_mode_and_hardlink_rejected(self):
        self.prepare(); clone = self.f.path('other-handoff.json'); handoff.write_new(clone, self.artifact.read_bytes())
        with self.assertRaisesRegex(ProtocolError, 'invalid_global_handoff'):
            handoff.read({'path': str(clone), 'sha256': self.ref['sha256']})
        self.artifact.chmod(0o600)
        with self.assertRaisesRegex(ProtocolError, 'unsafe_global_handoff_file'): handoff.read(self.ref)
        self.artifact.chmod(0o400); os.link(self.artifact, self.f.path('hardlink'))
        with self.assertRaisesRegex(ProtocolError, 'unsafe_global_handoff_file'): handoff.read(self.ref)

    def test_artifact_binding_changes_cannot_reuse_other_route_or_selection(self):
        self.prepare(); original = self.artifact.read_bytes(); value = json.loads(original)
        for key, replacement in [('route_id', '0' * 32), ('state_dir', str(self.f.ledger.root)),
                                 ('selection', value['selection'] | {'reasoning_effort': 'max'})]:
            with self.subTest(key=key):
                changed = copy.deepcopy(value); changed[key] = replacement
                self.corrupt(canonical(changed)); ref = {'path': str(self.artifact), 'sha256': hash_bytes(self.artifact.read_bytes())}
                with self.assertRaises(ProtocolError): handoff.read(ref)
        self.corrupt(original)

    def test_changed_package_bytes_with_restored_mtime_rejected(self):
        package = self.f.root / 'package'; package.mkdir()
        hashes = {**self.f.initial['runtime_source_hashes'], **self.f.initial['controller_source_hashes']}
        for relative in hashes:
            target = package / relative; target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.f.package / relative, target)
        self.prepare(package)
        target = package / 'remote_transport/global_handoff.py'; before = target.stat()
        target.write_bytes(target.read_bytes() + b'\n# changed bytes\n')
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaisesRegex(ProtocolError, 'global_worker_package_source_mismatch'):
            self.f.ledger.check_spawn(self.f.bridge.read(), self.plan)

    def test_failure_after_reservation_burns_attempt_before_artifact_write(self):
        with patch.object(handoff, 'write_new', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError): self.prepare()
        saved = json.loads(self.f.ledger.path.read_bytes())
        self.assertEqual(saved['spawns'][self.rid]['status'], 'reserved_outcome_unknown')
        self.assertFalse(self.artifact.exists()); self.assertFalse(self.plan.exists())
        restarted = NativeLedger(self.f.ledger.root, self.f.bridge.read().state, self.f.code, self.f.ledger.identity)
        with self.assertRaisesRegex(ProtocolError, 'already_reserved_no_replay'):
            restarted.plan_spawn(self.f.bridge.read(), self.rid, self.f.path('replacement'), self.f.package)

    def test_lost_plan_output_after_artifact_write_never_replaces_artifact(self):
        original = handoff.write_new
        def fail_plan(path, raw):
            if Path(path) == self.plan: raise OSError('synthetic plan write failure')
            return original(path, raw)
        with patch.object(handoff, 'write_new', side_effect=fail_plan):
            with self.assertRaises(OSError): self.prepare()
        before = self.artifact.read_bytes()
        with self.assertRaisesRegex(ProtocolError, 'already_reserved_no_replay'):
            self.f.ledger.plan_spawn(self.f.bridge.read(), self.rid, self.f.path('replacement'), self.f.package)
        self.assertEqual(self.artifact.read_bytes(), before)
        with self.assertRaises(FileExistsError): handoff.write_new(self.artifact, b'replacement')
        self.assertEqual(self.artifact.read_bytes(), before)

    def test_reservation_failure_creates_no_handoff_or_child_directory(self):
        original = self.f.ledger.save
        def fail_reservation(saved):
            if self.rid in saved['spawns']: raise OSError('synthetic reservation failure')
            return original(saved)
        with patch.object(self.f.ledger, 'save', side_effect=fail_reservation):
            with self.assertRaises(OSError): self.prepare()
        self.assertFalse(self.artifact.exists()); self.assertFalse(self.directory.exists())

    def test_dispatch_deadline_and_late_result_recording_remain_distinct(self):
        self.prepare(); deadline = self.packet['execute_before']
        with patch('time.time', return_value=deadline):
            with self.assertRaisesRegex(ProtocolError, 'dispatch_window_expired'):
                self.f.ledger.check_spawn(self.f.bridge.read(), self.plan)
        descriptor = handoff.read(self.ref)
        with patch('time.time', return_value=descriptor['expires'] + 1):
            result = self.record(); self.assertEqual(result['native_task_id'], self.native)
            with self.assertRaisesRegex(ProtocolError, 'expired'): handoff.read(self.ref)
        saved = json.loads(self.f.ledger.path.read_bytes())
        self.assertEqual(saved['spawns'][self.rid]['status'], 'recorded')

    def test_current_heartbeats_allow_plan_after_original_begin_heartbeat_ages_out(self):
        original = self.f.bridge.read().state['logical']['controller']['heartbeat_at']
        # Each new tick is observed/verified while the previous tick is live.
        for delta in (300, 600, 901):
            with patch('time.time', return_value=original + delta):
                self.f.execute('heartbeat', now=original + delta)
        with patch('time.time', return_value=original + 902):
            self.prepare()
            self.assertTrue(self.f.ledger.check_spawn(self.f.bridge.read(), self.plan)['dispatch_allowed'])
            self.assertEqual(self.record()['native_task_id'], self.native)

    def test_slow_handoff_verification_cannot_overrun_dispatch_deadline(self):
        self.prepare(); clock = [self.packet['created'] + 1]
        original = self.f.ledger._verify_handoff
        def delayed(*args, **kwargs):
            result = original(*args, **kwargs)
            clock[0] = self.packet['execute_before']
            return result
        with patch('time.time', side_effect=lambda: clock[0]), patch.object(self.f.ledger, '_verify_handoff', side_effect=delayed):
            with self.assertRaisesRegex(ProtocolError, 'dispatch_window_expired'):
                self.f.ledger.check_spawn(self.f.bridge.read(), self.plan)

    def test_slow_package_verification_cannot_release_expired_handoff(self):
        self.prepare(); value = handoff.read(self.ref); clock = [value['created'] + 1]
        original = handoff.verify_package
        def delayed(*args, **kwargs):
            result = original(*args, **kwargs); clock[0] = value['expires']; return result
        with patch('time.time', side_effect=lambda: clock[0]), patch.object(handoff, 'verify_package', side_effect=delayed):
            with self.assertRaisesRegex(ProtocolError, 'handoff_expired'): handoff.read(self.ref)

    def test_crashed_parent_import_never_leaks_join_or_repairs_child_evidence(self):
        self.prepare(); self.record(); self.f.execute('admitted', self.rid)
        with patch.object(JoinLedger, 'import_parent_admission', side_effect=OSError('synthetic crash')):
            with self.assertRaises(OSError):
                self.f.ledger.import_child_admission(self.f.bridge.read(), self.rid, self.receipt_path, self.native)
        before = list(self.directory.iterdir())
        with self.assertRaisesRegex(ProtocolError, 'verified_parent_child_import_required'):
            handoff.consume(self.ref, self.native, self.receipt_path)
        self.assertEqual(list(self.directory.iterdir()), before)
        with self.assertRaisesRegex(ProtocolError, 'parent_child_import_outcome_unknown_no_replay'):
            self.f.ledger.import_child_admission(self.f.bridge.read(), self.rid, self.receipt_path, self.native)

    def test_no_synthetic_expanded_arguments_are_accepted(self):
        self.prepare(); expanded = dict(self.out['arguments'])
        expanded['message'] += '\n' + self.artifact.read_text()
        with self.assertRaisesRegex(ProtocolError, 'actual_native_arguments_mismatch'):
            self.f.ledger.record_spawn(self.plan, expanded, {'task_name': self.native}, self.f.path('receipt'))


if __name__ == '__main__': unittest.main()
