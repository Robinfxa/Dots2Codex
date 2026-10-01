"""Bounded post-spawn cell emission. Synthetic queue only; no native dispatch."""
import contextlib
import io
import json
import subprocess
import unittest
from unittest.mock import patch

from remote_tests import test_global_heartbeat as fixtures
from remote_transport.global_gateway import private_write
from remote_transport.global_native import emit_cell, main
from remote_transport.model import ProtocolError, canonical, hash_bytes


class GlobalPostSpawnCellTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.GlobalHeartbeatTests('test_exact_900_root_policy_and_no_old_protocol_migration')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.join()
        self.route_id = self.f.demand()
        self.paths = {key: self.f.path(key) for key in (
            'plan_file', 'actual_arguments_file', 'native_result_file', 'record_snapshot_file')}
        self.raw = self.f.google.get_document(self.f.initial['document_id'])
        private_write(self.paths['record_snapshot_file'], canonical(self.raw))

    def emit(self, destination, **paths):
        f = self.f
        return emit_cell(f.ledger, f.source(), destination, f.package, f.root / 'join.txt',
                         'post-spawn', 2, 1200, self.route_id, **paths)

    def test_preemission_binds_exact_evidence_paths_without_reading_or_issuing_them(self):
        f = self.f
        before = json.loads(f.ledger.path.read_bytes())
        destination = f.root / 'native' / 'post-spawn.js'
        result = self.emit(destination, **self.paths)
        source = destination.read_text()
        invocation = {'routeId': self.route_id, 'planFile': str(self.paths['plan_file']),
                      'actualArgumentsFile': str(self.paths['actual_arguments_file']),
                      'nativeResultFile': str(self.paths['native_result_file']),
                      'recordSnapshotFile': str(self.paths['record_snapshot_file'])}
        self.assertTrue(source.endswith('text(await activeController.cell.completeAdmission('
                                        + json.dumps(invocation) + '));\n'))
        self.assertEqual(result['sha256'], hash_bytes(destination.read_bytes()))
        self.assertFalse(result['executes_on_emission'])
        self.assertTrue(result['requires_active_native_agent'])
        self.assertNotIn(f.code, source)
        for name, path in self.paths.items():
            if name != 'record_snapshot_file':
                self.assertFalse(path.exists())
        self.assertEqual(json.loads(self.paths['record_snapshot_file'].read_bytes()), self.raw)
        after = json.loads(f.ledger.path.read_bytes())
        self.assertEqual(after['operations'], before['operations'])
        self.assertEqual(after['spawns'], before['spawns'])
        checked = subprocess.run(['node', '--check', str(destination)], capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_cli_can_preemit_with_future_record_snapshot_separate_from_current_snapshot(self):
        f = self.f
        current_snapshot = self.paths['record_snapshot_file']
        future_snapshot = f.path('future-exact-dispatch-snapshot')
        destination = f.root / 'native' / 'post-spawn-cli.js'
        join_file = f.root / 'join.txt'
        private_write(join_file, f.code.encode())
        args = ['global_native', 'emit-cell', '--snapshot', str(current_snapshot),
                '--document-id', f.initial['document_id'], '--tab-id', 't.0',
                '--join-code-file', str(join_file), '--state-dir', str(f.ledger.root),
                '--native-task-id', f.ledger.identity, '--cell-operation', 'post-spawn',
                '--route-id', self.route_id, '--package-root', str(f.package), '--save', str(destination),
                '--plan-file', str(self.paths['plan_file']),
                '--actual-arguments', str(self.paths['actual_arguments_file']),
                '--native-result', str(self.paths['native_result_file']),
                '--record-snapshot-file', str(future_snapshot)]
        output = io.StringIO()
        with patch('sys.argv', args), contextlib.redirect_stdout(output):
            main()
        self.assertFalse(json.loads(output.getvalue())['executes_on_emission'])
        self.assertIn('"recordSnapshotFile": ' + json.dumps(str(future_snapshot)), destination.read_text())
        self.assertFalse(future_snapshot.exists())
        self.assertEqual(json.loads(current_snapshot.read_bytes()), self.raw)

    def test_every_exact_evidence_path_is_required_before_emission(self):
        for name in self.paths:
            with self.subTest(path=name):
                paths = dict(self.paths)
                paths[name] = None
                destination = self.f.path('missing-' + name)
                with self.assertRaisesRegex(ProtocolError, 'global_post_spawn_evidence_paths_required'):
                    self.emit(destination, **paths)
                self.assertFalse(destination.exists())

    def test_invalid_route_cannot_emit_or_reserve_post_spawn_work(self):
        f = self.f
        destination = f.path('invalid-route')
        before = json.loads(f.ledger.path.read_bytes())
        with self.assertRaisesRegex(ProtocolError, 'invalid_global_token'):
            emit_cell(f.ledger, f.source(), destination, f.package, f.root / 'join.txt',
                      'post-spawn', 2, 1200, 'not-a-route', **self.paths)
        self.assertFalse(destination.exists())
        self.assertEqual(json.loads(f.ledger.path.read_bytes()), before)


if __name__ == '__main__':
    unittest.main()
