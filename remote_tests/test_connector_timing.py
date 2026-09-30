"""Offline diagnostics tests: no provider calls, OAuth or native inference."""
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from remote_transport import timing
from remote_transport.connector_worker import ConnectorWorker
from remote_transport.model import ProtocolError
import test_long_sessions as fixtures


class LocalWorker:
    def __init__(self, root):
        self.root = root

    @timing.timed_stage('connector.input')
    def echo(self, value):
        return value

    @timing.timed_stage('connector.result')
    def fail(self, error):
        raise error


class ConnectorTimingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.worker = LocalWorker(self.root)
        self.path = self.root / timing.TIMING_FILE
        self.environment = patch.dict(os.environ, {timing.TIMING_ENV: '1'})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def events(self):
        values = [json.loads(line) for line in self.path.read_text().splitlines()]
        for event in values:
            self.assertEqual(set(event), {'stage', 'started_monotonic_ns', 'duration_ns', 'status'})
            self.assertIn(event['stage'], timing.STAGES)
            self.assertIn(event['status'], {'ok', 'error'})
            self.assertIs(type(event['started_monotonic_ns']), int)
            self.assertIs(type(event['duration_ns']), int)
            self.assertGreaterEqual(event['duration_ns'], 0)
        return values

    def test_disabled_is_default_and_has_no_clock_or_file_access(self):
        for value in (None, '', '0', 'true', 'yes', '/private/trace'):
            with self.subTest(value=value), patch.dict(os.environ):
                if value is None:
                    os.environ.pop(timing.TIMING_ENV, None)
                else:
                    os.environ[timing.TIMING_ENV] = value
                with patch.object(timing.time, 'monotonic_ns') as clock, patch.object(timing.os, 'open') as opened:
                    marker = object()
                    self.assertIs(self.worker.echo(marker), marker)
                    clock.assert_not_called()
                    opened.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_monotonic_only_payload_free_and_preserves_return_identity(self):
        payload = {'prompt': 'PRIVATE PROMPT', 'answer': 'PRIVATE ANSWER',
                   'native_task_id': 'PRIVATE ID', 'file': '/PRIVATE/PATH'}
        with patch.object(timing.time, 'monotonic_ns', side_effect=[100, 145]), \
                patch.object(timing.time, 'time', side_effect=AssertionError('no wall clock')):
            self.assertIs(self.worker.echo(payload), payload)
        self.assertEqual(self.events(), [{'stage': 'connector.input',
                         'started_monotonic_ns': 100, 'duration_ns': 45, 'status': 'ok'}])
        self.assertNotIn('PRIVATE', self.path.read_text())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_original_error_identity_and_message_preserved_but_not_recorded(self):
        for error in (ValueError('PRIVATE error /path ID'), KeyboardInterrupt('PRIVATE interrupt')):
            with self.subTest(error=type(error)), self.assertRaises(type(error)) as raised:
                self.worker.fail(error)
            self.assertIs(raised.exception, error)
        self.assertEqual([event['status'] for event in self.events()], ['error', 'error'])
        self.assertNotIn('PRIVATE', self.path.read_text())

    def test_sink_failure_never_changes_success_or_error(self):
        for name in ('open', 'write', 'fsync'):
            for failure in (OSError('PRIVATE disk failure'), KeyboardInterrupt()):
                with self.subTest(name=name, failure=type(failure)), \
                        patch.object(timing.os, name, side_effect=failure):
                    marker = object()
                    self.assertIs(self.worker.echo(marker), marker)
                    original = ValueError('original operation error')
                    with self.assertRaises(ValueError) as raised:
                        self.worker.fail(original)
                    self.assertIs(raised.exception, original)

    def test_clock_failure_never_changes_operation(self):
        for side_effect in (RuntimeError('clock'), [10, RuntimeError('clock')]):
            with self.subTest(side_effect=side_effect), \
                    patch.object(timing.time, 'monotonic_ns', side_effect=side_effect):
                self.assertEqual(self.worker.echo('result'), 'result')
        self.assertFalse(self.path.exists())

    def test_backwards_clock_is_dropped(self):
        with patch.object(timing.time, 'monotonic_ns', side_effect=[100, 99]):
            self.assertEqual(self.worker.echo('result'), 'result')
        self.assertFalse(self.path.exists())

    def test_records_are_fsynced_and_append_across_instances(self):
        actual_fsync = timing.os.fsync
        with patch.object(timing.os, 'fsync', wraps=actual_fsync) as sync:
            self.worker.echo('first')
            self.assertEqual(sync.call_count, 2)  # File plus directory entry.
            LocalWorker(self.root).echo('second')
            self.assertEqual(sync.call_count, 4)
        self.assertEqual(len(self.events()), 2)

    def test_full_log_is_bounded_without_rotation_or_truncation(self):
        self.worker.echo('first')
        before = self.path.read_bytes()
        with patch.object(timing, 'MAX_TIMING_BYTES', len(before)):
            self.assertEqual(self.worker.echo('second'), 'second')
        self.assertEqual(self.path.read_bytes(), before)

    def test_incomplete_tail_is_preserved(self):
        self.path.write_bytes(b'{"stage":')
        self.path.chmod(0o600)
        self.worker.echo('result')
        self.assertEqual(self.path.read_bytes(), b'{"stage":')

    def test_short_write_is_not_retried_and_later_append_is_dropped(self):
        actual_write = timing.os.write
        with patch.object(timing.os, 'write', side_effect=lambda fd, raw: actual_write(fd, raw[:7])) as write:
            self.assertEqual(self.worker.echo('first'), 'first')
            self.assertEqual(write.call_count, 1)
        partial = self.path.read_bytes()
        self.assertEqual(len(partial), 7)
        self.worker.echo('second')
        self.assertEqual(self.path.read_bytes(), partial)

    def test_busy_log_drops_event_without_waiting(self):
        self.worker.echo('first')
        before = self.path.read_bytes()
        with self.path.open('rb') as locked:
            fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.worker.echo('second'), 'second')
            self.assertEqual(self.path.read_bytes(), before)
        self.worker.echo('third')
        self.assertEqual(len(self.events()), 2)

    def test_unsafe_mode_sink_is_unchanged(self):
        self.path.write_bytes(b'PRIVATE\n')
        self.path.chmod(0o644)
        self.worker.echo('result')
        self.assertEqual(self.path.read_bytes(), b'PRIVATE\n')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)

    def test_symlink_and_hardlink_sink_are_rejected(self):
        other = self.root / 'do-not-change'
        other.write_bytes(b'PRIVATE\n')
        other.chmod(0o600)
        self.path.symlink_to(other)
        self.worker.echo('result')
        self.assertEqual(other.read_bytes(), b'PRIVATE\n')
        self.path.unlink()
        os.link(other, self.path)
        self.worker.echo('result')
        self.assertEqual(other.read_bytes(), b'PRIVATE\n')

    def test_fifo_and_directory_sink_are_rejected(self):
        os.mkfifo(self.path, 0o600)
        self.assertEqual(self.worker.echo('fifo'), 'fifo')
        self.path.unlink()
        self.path.mkdir(mode=0o700)
        self.assertEqual(self.worker.echo('directory'), 'directory')
        self.assertEqual(list(self.path.iterdir()), [])

    def test_unsafe_or_symlink_root_is_rejected(self):
        self.root.chmod(0o755)
        self.worker.echo('result')
        self.assertFalse(self.path.exists())
        self.root.chmod(0o700)
        target = self.root / 'real'
        target.mkdir(mode=0o700)
        link = self.root / 'link'
        link.symlink_to(target, target_is_directory=True)
        LocalWorker(link).echo('result')
        self.assertEqual(list(target.iterdir()), [])

    def test_fixed_stage_and_status_labels_only(self):
        with self.assertRaisesRegex(ValueError, 'unknown_timing_stage'):
            timing.timed_stage('PRIVATE PROMPT')
        timing._append(self.root, 'PRIVATE ID', 10, 1, 'ok')
        timing._append(self.root, 'connector.input', 10, 1, 'PRIVATE ERROR')
        timing._append(self.root, 'connector.input', 'PRIVATE PATH', 1, 'ok')
        self.assertFalse(self.path.exists())


class RealWorkerTimingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ConnectorWorkerTests('test_input_is_one_use_after_restart')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.environment = patch.dict(os.environ, {timing.TIMING_ENV: '1'})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def events(self):
        return [json.loads(line) for line in (self.fixture.cw.root / timing.TIMING_FILE).read_text().splitlines()]

    def test_status_does_not_change_wire_or_pin_or_journal(self):
        worker = self.fixture.cw
        before = {name: (worker.root / name).read_bytes() for name in ('pin.json', 'config.json', 'worker.json')}
        with patch.dict(os.environ, {timing.TIMING_ENV: '0'}):
            expected = worker.status()
        self.assertEqual(worker.status(), expected)
        self.assertEqual(before, {name: (worker.root / name).read_bytes() for name in before})
        self.assertEqual(self.events()[-1]['stage'], 'connector.status')

    def test_full_synthetic_roundtrip_records_only_local_stage_allowlist(self):
        fixture = self.fixture
        fixture.cw.poll()
        request = fixture.controller.submit('SECRET prompt sentinel', 'synthetic key')
        fixture.cw.accept(*fixture.execute(fixture.begin()))
        fixture.finish(1)
        result = fixture.controller.result(request)
        fixture.controller.record_delivery(request, result.oid, 'SECRET receipt sentinel')
        fixture.cw.tick(fixture.resource(), fixture.entries_for_all())
        fixture.cw.status()
        records = self.events()
        self.assertEqual({event['stage'] for event in records}, timing.STAGES)
        for event in records:
            self.assertEqual(set(event), {'stage', 'started_monotonic_ns', 'duration_ns', 'status'})
            self.assertEqual(event['status'], 'ok')
            self.assertGreaterEqual(event['duration_ns'], 0)
        text = (fixture.cw.root / timing.TIMING_FILE).read_text()
        for private in ('SECRET', 'synthetic', str(fixture.root), request, result.oid):
            self.assertNotIn(private, text)

    def test_failed_sink_does_not_replay_input_or_result_after_restart(self):
        fixture = self.fixture
        fixture.controller.submit('SECRET prompt sentinel', 'synthetic key')
        fixture.cw.accept(*fixture.execute(fixture.begin()))
        with patch.object(timing, '_append', side_effect=OSError('SECRET sink error')):
            permit = fixture.cw.input(fixture.entries_for_all(), 1)
            self.assertEqual(permit['action'], 'native_inference_once')
            restarted = ConnectorWorker(fixture.cw.root, 'synthetic/native')
            with self.assertRaisesRegex(ProtocolError, 'one_use_input_unavailable'):
                restarted.input(fixture.entries, 1)
            self.assertEqual(restarted.result(1, 'SECRET answer sentinel')['action'], 'upload_once')
            with self.assertRaisesRegex(ProtocolError, 'no_exposed_input_for_result'):
                restarted.result(1, 'SECRET answer replay')
        self.assertEqual(fixture.cw.status()['records']['1']['phase'], 'result_saved')


if __name__ == '__main__':
    unittest.main()
