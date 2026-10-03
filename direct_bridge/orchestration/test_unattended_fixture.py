"""Synthetic regression tests for the unattended cloud harness, not native proof."""
from __future__ import annotations
import copy
from pathlib import Path
import tempfile
import time
import unittest
import sys
import os
import json
import subprocess

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'orchestration')]
from unattended_fixture import UnattendedFixture, rpc
from facade.test_global_runtime import response


class UnattendedFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = UnattendedFixture(self.temp.name)

    def tearDown(self):
        self.fixture.close()
        self.temp.cleanup()

    def status(self):
        return self.fixture.call('bridge_status', {})

    def wait(self, condition, seconds=8):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            result = condition()
            if result:
                return result
            time.sleep(.02)
        self.fail('bounded fixture condition not reached')

    def claim(self):
        route = self.wait(lambda: self.status()['pending_routes'])[0]
        return self.fixture.call('get_request', {'route_id': route['route_id'],
            'worker_id': 'synthetic-regression-worker', 'context_epoch': 'synthetic-epoch',
            'model': route['model'], 'reasoning_effort': route['reasoning_effort'], 'wait_ms': 0})

    def finish(self, item, number, answer):
        result = response(number)
        result['output'][0]['content'][0]['text'] = answer
        args = {k: item[k] for k in ('route_id', 'claim_token', 'request_id', 'context_token')}
        args.update(action_id=f'synthetic-action-{number}', response=result, schema_tokens=[])
        first = self.fixture.call('finish_request', args)
        second = self.fixture.call('finish_request', args)
        self.assertEqual(first, second)
        return args

    def next(self, first, after, wait=5000):
        return self.fixture.call('get_request', {'route_id': first['route_id'],
            'claim_token': first['claim_token'], 'after_seq': after, 'wait_ms': wait})

    def test_delayed_intake_idle_restart_same_owner_and_duplicate_fences(self):
        self.assertFalse(self.status()['automatic_wake'])
        self.assertEqual(self.status()['routes'], [])
        self.fixture.begin()
        first = self.claim()
        self.assertEqual(first['context']['kind'], 'full')
        self.finish(first, 1, 'HARBOUR-47')
        second = self.next(first, 1)
        self.assertEqual(second['context']['kind'], 'delta')
        self.finish(second, 2, 'HARBOUR-94')
        self.wait(lambda: self.fixture.stage == 'awaiting_restart')
        idle_started = time.monotonic()
        idle = self.next(first, 2, 5000)
        self.assertGreaterEqual(time.monotonic() - idle_started, 4.8)
        self.assertEqual(idle['status'], 'pending')
        self.assertFalse(idle['automatic_wake'])
        self.assertFalse(idle['resubmit_action'])
        self.fixture.replay_last_http()
        self.assertEqual(self.status()['routes'][0]['metrics']['response_commits'], 2)
        self.fixture.restart_bridge()
        self.fixture.replay_last_http()
        self.fixture.continue_after_restart()
        third = self.next(first, 2)
        self.assertEqual(third['context']['kind'], 'delta')
        self.assertEqual(third['logical_worker_id'], first['logical_worker_id'])
        last_args = self.finish(third, 3, 'HARBOUR-141')
        self.wait(lambda: self.fixture.stage == 'complete')
        self.fixture.replay_last_http()
        report = self.fixture.report()
        self.assertEqual(report['failures'], [])
        self.assertEqual(report['restart_count'], 1)
        self.assertEqual(len(report['turns']), 3)
        route = report['status']['routes'][0]
        self.assertEqual(route['metrics']['full_context_returns'], 1)
        self.assertEqual(route['metrics']['delta_context_returns'], 2)
        self.assertEqual(route['metrics']['response_commits'], 3)
        changed = copy.deepcopy(last_args)
        changed['response']['output'][0]['content'][0]['text'] = 'a different answer'
        with self.assertRaisesRegex(AssertionError, 'action_id_conflict'):
            self.fixture.call('finish_request', changed)
        replacement = {'route_id': first['route_id'], 'worker_id': 'replacement',
                       'context_epoch': 'synthetic-epoch', 'model': 'gpt-6-astra',
                       'reasoning_effort': 'xhigh', 'wait_ms': 0}
        with self.assertRaisesRegex(AssertionError, 'native_route_owner_immutable'):
            self.fixture.call('get_request', replacement)
        replacement.update(worker_id=first['logical_worker_id'], context_epoch='changed-epoch')
        with self.assertRaisesRegex(AssertionError, 'native_route_owner_immutable'):
            self.fixture.call('get_request', replacement)
        self.assertEqual(self.status()['routes'][0]['metrics']['response_commits'], 3)

    def test_control_replay_cannot_duplicate_scenario_or_restart_unsettled_work(self):
        with self.assertRaisesRegex(ValueError, 'no_settled_turn'):
            self.fixture.replay_last_http()
        with self.assertRaisesRegex(ValueError, 'restart_requires_two_settled_turns'):
            self.fixture.restart_bridge()
        with self.assertRaisesRegex(ValueError, 'restart_required_before_third_turn'):
            self.fixture.continue_after_restart()
        self.fixture.begin()
        with self.assertRaisesRegex(ValueError, 'scenario_already_started'):
            self.fixture.begin()
        self.assertEqual(self.fixture.restart_count, 0)

    def test_existing_state_is_never_reused_or_overwritten(self):
        before = (Path(self.temp.name) / 'config.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'fixture_directory_must_be_empty'):
            UnattendedFixture(self.temp.name)
        self.assertEqual((Path(self.temp.name) / 'config.json').read_bytes(), before)

    def test_report_preserves_unverified_platform_and_mac_boundary(self):
        result = self.fixture.report()
        for key in ('actual_native_platform_verified_by_bridge', 'live_mac_verified',
                    'deployed_mcp_to_native_wake_verified', 'automatic_wake'):
            self.assertIs(result[key], False)
        self.assertEqual(result['external_model_api_calls'], 0)
        self.assertNotIn(self.fixture.cfg['http_bearer'], str(result))
        self.assertNotIn(self.fixture.token, str(result))


class UnattendedMailboxTests(unittest.TestCase):
    def test_real_cli_mailbox_roundtrip_and_clean_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / 'ready.json'
            proc = subprocess.Popen([sys.executable, '-B', str(ROOT / 'orchestration/unattended_fixture.py'),
                'serve', '--state-dir', directory, '--ready-file', str(ready), '--lifetime-seconds', '60'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
            try:
                deadline = time.monotonic() + 8
                while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(ready.exists(), 'fixture ready file missing')
                self.assertEqual(ready.stat().st_mode & 0o777, 0o600)
                self.assertIn('mailbox', json.loads(ready.read_text()))
                report = rpc(ready, 'report')
                self.assertEqual(report['stage'], 'created')
                self.assertFalse(report['automatic_wake'])
                with self.assertRaisesRegex(ValueError, 'unknown_fixture_command'):
                    rpc(ready, 'unknown')
                self.assertEqual(rpc(ready, 'stop')['status'], 'stopping')
                stdout, stderr = proc.communicate(timeout=8)
                self.assertEqual(proc.returncode, 0, stderr.decode())
                self.assertTrue((Path(directory) / 'final-report.json').exists())
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.communicate(timeout=8)


if __name__ == '__main__':
    unittest.main()
