"""Offline emitter checks; no connector calls or native admission."""
import json
import unittest
from unittest.mock import patch

from remote_tests import test_global_heartbeat as fixtures
from remote_transport.global_native import emit_cell
from remote_transport.model import ProtocolError


class GlobalClaimBeginCellTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.GlobalHeartbeatTests('test_exact_900_root_policy_and_no_old_protocol_migration')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_emission_binds_exact_route_without_reserving_cas_or_native_attempt(self):
        f = self.f
        f.join()
        route_id = f.demand()
        before = json.loads(f.ledger.path.read_text())
        calls = len(f.google.calls)
        target = f.root / 'native' / 'claim-begin.js'
        result = emit_cell(f.ledger, f.source(), target, f.package, f.root / 'join.txt',
                           'claim-begin', 2, 1200, route_id)
        source = target.read_text()
        self.assertIn('activeController.cell.claimAndBegin('+json.dumps({'routeId': route_id})+')', source)
        self.assertNotIn(f.code, source)
        self.assertFalse(result['executes_on_emission'])
        after = json.loads(f.ledger.path.read_text())
        self.assertEqual(after['operations'], before['operations'])
        self.assertEqual(after['spawns'], before['spawns'])
        self.assertEqual(len(f.google.calls), calls)

    def test_missing_route_fails_without_emitting_or_reserving(self):
        f = self.f
        target = f.root / 'native' / 'missing-route.js'
        with self.assertRaises(ProtocolError):
            emit_cell(f.ledger, f.source(), target, f.package, f.root / 'join.txt',
                      'claim-begin', 2, 1200)
        self.assertFalse(target.exists())
        self.assertFalse(f.ledger.path.exists())

    def test_native_plan_and_check_share_just_read_snapshot_without_network_after_reservation(self):
        f = self.f
        f.join()
        route_id = f.demand()
        f.event('claim', route_id=route_id)
        f.event('begin', route_id=route_id)
        fresh = f.source()
        target = f.path('native-plan')
        with patch.object(f.google, 'get_document', side_effect=AssertionError('unexpected network read')):
            planned = f.ledger.plan_spawn(fresh, route_id, target, f.package)
            f.clock += 1
            checked = f.ledger.check_spawn(fresh, target)
            self.assertTrue(checked['dispatch_allowed'])
            self.assertEqual(checked['execute_before'], planned['execute_before'])
            self.assertFalse(checked['retry_allowed'])
            f.clock += 9
            with self.assertRaisesRegex(ProtocolError, 'global_native_dispatch_window_expired_no_replay'):
                f.ledger.check_spawn(fresh, target)


if __name__ == '__main__':
    unittest.main()
