"""Independent 15-minute signed-controller boundaries, with no live ports.

Expected limits are literal policy requirements, not values derived from TIMING.
The signed snapshots, native ledger, and gateway are real local implementations;
there are no Google calls, native tool invocations, or wall-clock sleeps.
"""
import copy
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch

from remote_transport import global_control as queue, router_bootstrap as child
from remote_transport.global_gateway import Store
from remote_transport.global_google import mirror_verified_queue
from remote_transport.global_native import NativeLedger
from remote_transport.global_timing import (
    TIMING, controller_active, event_deadline, observation_window_current,
    require_active, validate_timing,
)
from remote_transport.model import ProtocolError
from remote_transport.router_join import _source_hashes
from remote_transport.selection import load_catalog, select


EXPECTED_TIMING = {
    'contract': 'dots-global-timing/1',
    'freshness_seconds': 900,
    'heartbeat_interval_seconds': 25,
    'operation_budget_seconds': 120,
    'spawn_check_seconds': 10,
}


class IndependentControllerTimingTests(unittest.TestCase):
    def setUp(self):
        self.now = 2_000_000_000.0
        clock = patch('time.time', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.code = secrets.token_hex(32)
        self.native_id = '/root/independent_timing_fixture'

    def controller(self, *, activation_seconds=1800, lease_seconds=1200):
        created = int(self.now)
        store, generation = Store.initialize(
            self.root/'gateway', select(load_catalog(), 'gpt-6.1-sol', 'high'),
            seconds=activation_seconds, idle_seconds=min(1800, activation_seconds))
        initial = queue.initial(
            activation_id=generation, queue_id=secrets.token_hex(16),
            folder_id='synthetic-folder', document_id='synthetic-document',
            tab_id='t.0', join_code=self.code, created=created,
            expires=created+activation_seconds, runtime_source_hashes=_source_hashes())
        state = queue.transition(initial, self.code, 'join', 'native', {
            'native_task_id': self.native_id,
            'controller_epoch': secrets.token_hex(16),
            'lease_expires': min(created+lease_seconds, initial['expires']),
            'capacity': 2,
        }, now=created)
        controller = state['logical']['controller']
        state = queue.transition(state, self.code, 'heartbeat', 'native', {
            'native_task_id': self.native_id,
            'controller_epoch': controller['controller_epoch'],
        }, now=created+1)
        self.now = created+1
        source = queue.Snapshot('synthetic-document', 't.0', 'r3', queue.block(state))
        ledger = NativeLedger(self.root/'native', initial, self.code, self.native_id)
        ledger.inspect(source)
        mirror_verified_queue(store, state, self.code)
        return store, ledger, source

    def test_exact_signed_policy_preserves_all_other_timing_limits(self):
        _, ledger, source = self.controller()
        self.assertEqual(TIMING, EXPECTED_TIMING)
        self.assertEqual(source.state['controller_timing'], EXPECTED_TIMING)
        self.assertEqual(ledger.inspect(source)['controller_timing'], EXPECTED_TIMING)

    def test_correctly_signed_old_180_policy_is_rejected_without_migration(self):
        store, _, source = self.controller()
        old = copy.deepcopy(source.state)
        old['controller_timing']['freshness_seconds'] = 180
        # Authenticate the old root correctly: rejection must be its policy,
        # not a bad signature. No event-history migration is attempted.
        old['events'] = []
        old['epoch'] = 0
        old['logical'] = {'controller': None, 'demands': {}, 'closed': False}
        old['root_mac'] = child.proof(self.code, 'global-root/2', queue.root_of(old))
        child.verify_proof(self.code, 'global-root/2', queue.root_of(old), old['root_mac'])
        for action in (
            lambda: queue.verify(old, self.code),
            lambda: NativeLedger(self.root/'old-native', old, self.code, self.native_id),
            lambda: mirror_verified_queue(store, old, self.code),
        ):
            with self.subTest(action=action):
                with self.assertRaisesRegex(ProtocolError, '^unsupported_global_timing_policy$'):
                    action()
        self.assertEqual(old['controller_timing']['freshness_seconds'], 180)
        self.assertFalse((self.root/'old-native').exists())

    def test_policy_requires_exact_values_types_and_keys(self):
        for replacement in (180, 899, 901, 900.0, True):
            with self.subTest(freshness=replacement):
                policy = dict(EXPECTED_TIMING, freshness_seconds=replacement)
                with self.assertRaisesRegex(ProtocolError, 'unsupported_global_timing_policy'):
                    validate_timing(policy)
        for policy in (
            {key: value for key, value in EXPECTED_TIMING.items() if key != 'spawn_check_seconds'},
            dict(EXPECTED_TIMING, allow_renewal=True),
        ):
            with self.subTest(policy=policy):
                with self.assertRaisesRegex(ProtocolError, 'unsupported_global_timing_policy'):
                    validate_timing(policy)

    def test_literal_strict_899_999_and_900_second_boundaries(self):
        heartbeat = self.now
        for age, expected in ((0, True), (180, True), (899.999, True),
                              (900, False), (900.001, False), (-.001, False)):
            with self.subTest(age=age):
                self.assertIs(controller_active(
                    heartbeat, heartbeat+1800, TIMING, heartbeat+age), expected)

    def test_native_and_gateway_enforce_same_strict_900_boundary(self):
        store, ledger, source = self.controller()
        controller = source.state['logical']['controller']
        heartbeat = controller['heartbeat_at']
        self.now = heartbeat+899.999
        self.assertEqual(ledger.inspect(source)['controller'], controller)
        self.assertTrue(store.status()['controller_active'])
        self.now = heartbeat+900
        self.assertFalse(store.status()['controller_active'])
        with self.assertRaisesRegex(ProtocolError, 'not_active_restart_required'):
            ledger.inspect(source)
        with self.assertRaisesRegex(ProtocolError, 'not_active_restart_required'):
            mirror_verified_queue(store, source.state, self.code)
        self.assertEqual(source.state['logical']['controller'], controller)

    def test_shorter_controller_lease_wins_before_900_seconds(self):
        store, ledger, source = self.controller(lease_seconds=60)
        controller = source.state['logical']['controller']
        deadline = controller['lease_expires']
        self.assertLess(deadline-controller['heartbeat_at'], 900)
        self.now = deadline-.001
        self.assertEqual(require_active(controller, TIMING, self.now), controller)
        self.assertEqual(ledger.inspect(source)['controller'], controller)
        self.assertTrue(store.status()['controller_active'])
        self.now = deadline
        with self.assertRaisesRegex(ProtocolError, 'not_active'):
            require_active(controller, TIMING, self.now)
        with self.assertRaisesRegex(ProtocolError, 'not_active'):
            ledger.inspect(source)
        self.assertFalse(store.status()['controller_active'])

    def test_earlier_activation_expiry_caps_join_and_liveness(self):
        store, ledger, source = self.controller(activation_seconds=60, lease_seconds=1200)
        state = source.state
        controller = state['logical']['controller']
        self.assertEqual(controller['lease_expires'], state['expires'])
        self.assertLess(state['expires']-controller['heartbeat_at'], 900)
        self.now = state['expires']-.001
        self.assertEqual(ledger.inspect(source)['controller'], controller)
        self.assertTrue(store.status()['controller_active'])
        self.now = state['expires']
        with self.assertRaisesRegex(ProtocolError, 'queue_expired_or_future'):
            queue.verify(state, self.code)
        with self.assertRaisesRegex(ProtocolError, 'not_active'):
            ledger.inspect(source)
        self.assertFalse(store.status()['controller_active'])
        self.assertEqual(store.activation()['expires'], state['expires'])

    def test_signed_heartbeat_transition_cannot_resurrect_stale_controller(self):
        _, _, source = self.controller()
        state = source.state
        controller = state['logical']['controller']
        arguments = {key: controller[key] for key in ('native_task_id', 'controller_epoch')}
        before = copy.deepcopy(state)
        with self.assertRaisesRegex(ProtocolError, 'global_controller_not_active'):
            queue.transition(state, self.code, 'heartbeat', 'native', arguments,
                             now=controller['heartbeat_at']+900)
        self.assertEqual(state, before)

    def test_clock_rollback_fence_survives_advance_and_ledger_restart(self):
        store, ledger, source = self.controller()
        heartbeat = source.state['logical']['controller']['heartbeat_at']
        self.now = heartbeat+800
        ledger.inspect(source)
        self.assertTrue(store.status()['controller_active'])
        self.now = heartbeat+799
        with self.assertRaisesRegex(ProtocolError, 'clock_rollback'):
            ledger.inspect(source)
        self.assertFalse(store.status()['controller_active'])
        self.now = heartbeat+801
        restarted = NativeLedger(self.root/'native', source.state, self.code, self.native_id)
        with self.assertRaisesRegex(ProtocolError, 'clock_rollback'):
            restarted.inspect(source)
        with self.assertRaisesRegex(ProtocolError, 'clock_rollback'):
            mirror_verified_queue(store, source.state, self.code)
        self.assertFalse(store.status()['controller_active'])

    def test_cas_deadline_still_takes_earliest_independent_cap(self):
        heartbeat = int(self.now)
        for at, lease, activation, expected in (
            (heartbeat+1, heartbeat+1800, heartbeat+2000, heartbeat+121),
            (heartbeat+800, heartbeat+1800, heartbeat+2000, heartbeat+900),
            (heartbeat+1, heartbeat+60, heartbeat+2000, heartbeat+60),
            (heartbeat+1, heartbeat+1800, heartbeat+50, heartbeat+50),
        ):
            with self.subTest(at=at, lease=lease, activation=activation):
                state = {
                    'controller_timing': copy.deepcopy(EXPECTED_TIMING),
                    'expires': activation,
                    'logical': {'controller': {'heartbeat_at': heartbeat, 'lease_expires': lease}},
                }
                self.assertEqual(event_deadline(state, 'heartbeat', at), expected)

    def test_unobserved_native_event_keeps_strict_120_second_acceptance(self):
        at = int(self.now)
        state = {'controller_timing': copy.deepcopy(EXPECTED_TIMING),
                 'events': [{'actor': 'native', 'at': at}]}
        self.assertTrue(observation_window_current(state, 0, at+119.999))
        self.assertFalse(observation_window_current(state, 0, at+120))
        # Previously verified events are history, never a fresh heartbeat.
        self.assertTrue(observation_window_current(state, 1, at+900))


if __name__ == '__main__':
    unittest.main()
