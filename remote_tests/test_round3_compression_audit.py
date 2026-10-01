"""Independent compression boundary audit; synthetic provider/native evidence only."""
import copy
import json
import unittest
from unittest.mock import patch

from remote_tests import test_global_heartbeat as fixtures
from remote_transport import global_control as queue, global_native
from remote_transport.global_gateway import private_write
from remote_transport.model import ProtocolError, canonical
from remote_transport.router_join import JoinLedger


class AdmissionCompressionAuditTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.GlobalHeartbeatTests('test_exact_900_root_policy_and_no_old_protocol_migration')
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.f.join(latency=0)
        self.f.bridge.bootstrap_seconds = 1800
        self.route = self.f.demand()
        self.f.event('claim-begin', route_id=self.route)
        self.native_plan = self.f.path('actual-native-plan')
        self.actual = self.f.ledger.plan_spawn(self.f.source(), self.route, self.native_plan, self.f.package)
        self.native = '/root/audit/' + self.actual['arguments']['task_name']
        self.receipt = self.f.path('actual-receipt')
        self.f.ledger.record_spawn(self.native_plan, self.actual['arguments'],
                                   {'task_name': self.native}, self.receipt)
        self.f.clock += 1

    def group(self):
        source = self.f.source(); path = self.f.path('heartbeat-admitted')
        result = self.f.ledger.plan_event(source, 'heartbeat-admitted', path, route_id=self.route)
        return source, path, result, json.loads(path.read_bytes())

    def document(self, state, revision='independent-new-revision'):
        resource = self.f.google.get_document(self.f.initial['document_id'])
        resource['revisionId'] = revision
        resource['tabs'][0]['documentTab']['body']['content'][1]['paragraph']['elements'][0]['textRun']['content'] = queue.block(state)
        return resource

    def commit(self, path, result):
        response = self.f.google.batch_update_document(**result['tool_arguments'])
        return self.f.ledger.verify_plan(path, response, self.f.google.get_document(self.f.initial['document_id']))

    def test_one_write_has_two_exact_ordered_events_and_one_group_reservation(self):
        source, path, result, packet = self.group()
        self.assertEqual(packet['group_kind'], 'heartbeat-admitted')
        self.assertEqual([e['kind'] for e in packet['expected_state']['events'][-2:]], ['heartbeat', 'admitted'])
        self.assertEqual(len(set(packet['operation_ids'])), 2)
        self.assertEqual(result['tool_arguments']['write_control'], {'requiredRevisionId': source.revision_id})
        self.assertEqual(len(result['tool_arguments']['requests']), 1)
        before = len(self.f.google.calls)
        self.assertTrue(self.commit(path, result)['verified'])
        self.assertEqual(len(self.f.google.calls), before + 1)
        saved = json.loads(self.f.ledger.path.read_bytes())
        self.assertEqual(saved['operations']['heartbeat-admitted:' + self.route]['status'], 'verified')
        self.assertNotIn('admitted:' + self.route, saved['operations'])

    def test_partial_heartbeat_never_accepts_admission_or_import_or_new_write(self):
        f = self.f; source, path, _, packet = self.group(); event = packet['expected_state']['events'][-2]
        partial = queue.transition(source.state, f.code, 'heartbeat', 'native', event['arguments'],
                                   operation_id=event['operation_id'], now=event['at'])
        with self.assertRaisesRegex(ProtocolError, 'not_observed'):
            f.ledger.verify_plan(path, None, self.document(partial))
        for kind in ('heartbeat', 'admitted', 'heartbeat-admitted'):
            with self.subTest(kind=kind), self.assertRaisesRegex(ProtocolError, 'unresolved'):
                f.ledger.plan_event(source, kind, f.path('forbidden-replay'), route_id=self.route)
        with self.assertRaisesRegex(ProtocolError, 'unresolved'):
            f.ledger.import_child_admission(source, self.route, self.receipt, self.native)
        self.assertEqual(list(f.ledger.child_state_dir(self.route).iterdir()), [])

    def test_lost_response_exact_complete_prefix_can_verify_and_import(self):
        f = self.f; source, path, result, packet = self.group()
        f.google.batch_update_document(**result['tool_arguments'])
        f.clock += 1
        state = packet['expected_state']; controller = state['logical']['controller']
        later = queue.transition(state, f.code, 'heartbeat', 'native',
            {'native_task_id': controller['native_task_id'], 'controller_epoch': controller['controller_epoch']})
        readback = self.document(later)
        self.assertTrue(f.ledger.verify_plan(path, None, readback)['verified'])
        accepted = queue.snapshot(readback, source.document_id, source.tab_id)
        imported = f.ledger.import_child_admission(accepted, self.route, self.receipt, self.native)
        self.assertTrue(imported['imported'])
        bootstrap = accepted.state['logical']['demands'][self.route]['child_bootstrap']
        ledger = JoinLedger(f.ledger.child_state_dir(self.route), bootstrap)
        saved = json.loads(ledger.path.read_bytes())
        self.assertEqual(saved['native_admission']['parent_provenance']['admitted_epoch'], packet['expected_state']['epoch'])
        self.assertEqual(saved['native_admission']['receipt'], json.loads(self.receipt.read_bytes()))

    def test_group_cannot_refresh_away_predecessor_deadline(self):
        f = self.f; source = f.source(); heartbeat = source.state['logical']['controller']['heartbeat_at']
        f.clock = heartbeat + 899
        source, path, result, packet = self.group()
        self.assertEqual(packet['execute_before'], heartbeat + 900)
        f.clock = heartbeat + 900
        with self.assertRaisesRegex(ProtocolError, 'not_active|dispatch_window'):
            f.ledger.check_plan(source, path)
        # A late real result can exist, but its newer signed heartbeat grants no authority.
        f.google.batch_update_document(**result['tool_arguments'])
        with self.assertRaisesRegex(ProtocolError, 'acceptance_window'):
            f.ledger.verify_plan(path, None, f.google.get_document(source.document_id))
        self.assertEqual(list(f.ledger.child_state_dir(self.route).iterdir()), [])

    def test_receipt_file_mutation_is_rejected_before_reserving_or_writing_group(self):
        f = self.f; receipt = json.loads(self.receipt.read_bytes()); receipt['native_task_id'] = '/root/other/global_' + self.route
        private_write(self.receipt, canonical(receipt)); before = json.loads(f.ledger.path.read_bytes())
        with self.assertRaisesRegex(ProtocolError, 'actual_admission'):
            self.group()
        after = json.loads(f.ledger.path.read_bytes())
        self.assertEqual(after['operations'], before['operations'])

    def test_group_output_loss_keeps_legacy_and_group_burned_after_restart(self):
        f = self.f; source = f.source(); target = f.path('failed-group-output'); original = global_native.private_write
        def fail_output(path, raw):
            if path == target: raise OSError('synthetic durable-output fault')
            return original(path, raw)
        with patch.object(global_native, 'private_write', side_effect=fail_output):
            with self.assertRaises(OSError):
                f.ledger.plan_event(source, 'heartbeat-admitted', target, route_id=self.route)
        restarted = global_native.NativeLedger(f.ledger.root, source.state, f.code, f.ledger.identity)
        for kind in ('heartbeat', 'admitted', 'heartbeat-admitted'):
            with self.subTest(kind=kind), self.assertRaisesRegex(ProtocolError, 'unresolved'):
                restarted.plan_event(source, kind, f.path('regenerated'), route_id=self.route)
        self.assertFalse(target.exists())

    def test_exact_schema_rejects_kind_order_id_deadline_and_request_mutation(self):
        _, _, _, packet = self.group()
        mutations = [lambda p: p.update(group_kind='claim-begin'),
                     lambda p: p['operation_ids'].reverse(),
                     lambda p: p.update(group_id='0' * 64),
                     lambda p: p.update(execute_before=p['execute_before'] + 1),
                     lambda p: p['tool_arguments']['requests'].append(copy.deepcopy(p['tool_arguments']['requests'][0])),
                     lambda p: p['tool_arguments']['write_control'].update(targetRevisionId='unauthorized')]
        for mutate in mutations:
            changed = copy.deepcopy(packet); mutate(changed)
            with self.subTest(mutation=mutate), self.assertRaises(ProtocolError):
                queue.validate_plan(changed, self.f.code)

    def test_actual_result_remains_durable_when_group_clock_rolls_back(self):
        f = self.f; f.ledger.inspect(f.source()); f.clock -= .5
        with self.assertRaisesRegex(ProtocolError, 'clock_rollback'):
            self.group()
        saved = json.loads(f.ledger.path.read_bytes())
        self.assertEqual(saved['spawns'][self.route]['status'], 'recorded')
        self.assertTrue(self.receipt.exists())
        self.assertEqual(list(f.ledger.child_state_dir(self.route).iterdir()), [])


class JoinCompressionAuditTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.GlobalHeartbeatTests('test_exact_900_root_policy_and_no_old_protocol_migration')
        self.f.setUp(); self.addCleanup(self.f.doCleanups)

    def group(self, sleep=None):
        f = self.f; source = f.source(); path = f.path('join-heartbeat')
        def next_tick(seconds): f.clock += seconds
        with patch.object(global_native.time, 'sleep', side_effect=sleep or next_tick):
            result = f.ledger.plan_event(source, 'join-heartbeat', path, capacity=2, seconds=1200)
        return source, path, result, json.loads(path.read_bytes())

    def document(self, state):
        value = self.f.google.get_document(self.f.initial['document_id']); value['revisionId'] = 'independent-joined'
        value['tabs'][0]['documentTab']['body']['content'][1]['paragraph']['elements'][0]['textRun']['content'] = queue.block(state)
        return value

    def test_real_next_tick_group_retains_join_preparation_deadline(self):
        f = self.f; start = int(f.clock); source, path, result, packet = self.group()
        joined, heartbeat = packet['expected_state']['events'][-2:]
        self.assertEqual([joined['kind'], heartbeat['kind']], ['join', 'heartbeat'])
        self.assertEqual(joined['at'], start); self.assertEqual(heartbeat['at'], start + 1)
        self.assertEqual(packet['execute_before'], start + 120)
        self.assertEqual(joined['arguments']['lease_expires'], start + 1200)
        self.assertTrue(f.ledger.check_plan(source, path)['dispatch_allowed'])
        response = f.google.batch_update_document(**result['tool_arguments'])
        accepted = f.ledger.verify_plan(path, response, f.google.get_document(source.document_id))
        self.assertTrue(accepted['verified']); self.assertFalse(accepted['first_heartbeat_required'])
        f.bridge.sync_heartbeat(); self.assertTrue(f.store.status()['controller_active'])

    def test_partial_join_does_not_satisfy_group_or_first_heartbeat(self):
        f = self.f; source, path, _, packet = self.group(); joined = packet['expected_state']['events'][-2]
        partial = queue.transition(source.state, f.code, 'join', 'native', joined['arguments'],
                                   operation_id=joined['operation_id'], now=joined['at'])
        with self.assertRaisesRegex(ProtocolError, 'not_observed'):
            f.ledger.verify_plan(path, None, self.document(partial))
        for kind in ('join', 'join-heartbeat', 'heartbeat'):
            with self.subTest(kind=kind), self.assertRaisesRegex(ProtocolError, 'unresolved|join_required'):
                f.ledger.plan_event(source, kind, f.path('replacement'), capacity=2, seconds=1200)

    def test_overslept_tick_cannot_be_forged_as_exact_next_second(self):
        f = self.f
        def oversleep(seconds): f.clock += seconds + 2
        with self.assertRaisesRegex(ProtocolError, 'clock_changed'):
            self.group(oversleep)
        saved = json.loads(f.ledger.path.read_bytes())
        self.assertEqual(saved['operations'], {})
        self.assertEqual(f.source().state['events'], [])

    def test_backward_tick_cannot_emit_signed_future_pair(self):
        f = self.f
        def rollback(seconds): f.clock -= 1
        with self.assertRaisesRegex(ProtocolError, 'clock_changed'):
            self.group(rollback)
        saved = json.loads(f.ledger.path.read_bytes())
        self.assertEqual(saved['operations'], {})
        self.assertEqual(f.source().state['events'], [])


class ClaimStartupCompressionAuditTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.GlobalHeartbeatTests('test_exact_900_root_policy_and_no_old_protocol_migration')
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.f.join(latency=0); self.f.bridge.bootstrap_seconds = 1800
        self.route = self.f.demand()

    def group(self):
        source = self.f.source(); path = self.f.path('claim-startup')
        result = self.f.ledger.plan_event(source, 'claim-startup', path, route_id=self.route)
        return source, path, result, json.loads(path.read_bytes())

    def document(self, state):
        value = self.f.google.get_document(self.f.initial['document_id']); value['revisionId'] = 'independent-claim'
        value['tabs'][0]['documentTab']['body']['content'][1]['paragraph']['elements'][0]['textRun']['content'] = queue.block(state)
        return value

    def test_due_heartbeat_group_complete_prefix_authorizes_one_exact_native_plan(self):
        f = self.f; f.clock += 25; source, path, result, packet = self.group()
        self.assertEqual(packet['group_kind'], 'heartbeat-claim-begin')
        self.assertEqual([e['kind'] for e in packet['expected_state']['events'][-3:]], ['heartbeat', 'claim', 'begin'])
        response = f.google.batch_update_document(**result['tool_arguments'])
        f.ledger.verify_plan(path, response, f.google.get_document(source.document_id))
        out = f.ledger.plan_spawn(f.source(), self.route, f.path('native'), f.package)
        self.assertEqual(out['execute_before'], f.clock + 10)
        for kind in ('claim', 'begin', 'claim-begin', 'claim-startup'):
            with self.subTest(kind=kind), self.assertRaisesRegex(ProtocolError, 'already_issued'):
                f.ledger.plan_event(f.source(), kind, f.path('again'), route_id=self.route)

    def test_two_of_three_events_never_satisfies_group_or_exposes_native(self):
        f = self.f; f.clock += 25; source, path, _, packet = self.group(); partial = source.state
        for event in packet['expected_state']['events'][-3:-1]:
            partial = queue.transition(partial, f.code, event['kind'], event['actor'], event['arguments'],
                                       operation_id=event['operation_id'], now=event['at'])
        with self.assertRaisesRegex(ProtocolError, 'not_observed'):
            f.ledger.verify_plan(path, None, self.document(partial))
        with self.assertRaisesRegex(ProtocolError, 'unresolved'):
            f.ledger.plan_spawn(source, self.route, f.path('forbidden-native'), f.package)
        self.assertEqual(json.loads(f.ledger.path.read_bytes())['spawns'], {})

    def test_due_heartbeat_claim_preserves_pre_refresh_expiry_fence(self):
        f = self.f; old = f.source().state['logical']['controller']['heartbeat_at']; f.clock = old + 899
        source, path, result, packet = self.group()
        self.assertEqual(packet['execute_before'], old + 900)
        f.clock = old + 900
        f.google.batch_update_document(**result['tool_arguments'])
        with self.assertRaisesRegex(ProtocolError, 'acceptance_window'):
            f.ledger.verify_plan(path, None, f.google.get_document(source.document_id))
        self.assertEqual(json.loads(f.ledger.path.read_bytes())['spawns'], {})

    def test_preemitted_same_root_snapshot_only_selects_ledger_for_actual_result_recording(self):
        f = self.f; original = f.source()
        source, path, result, packet = self.group()
        response = f.google.batch_update_document(**result['tool_arguments'])
        f.ledger.verify_plan(path, response, f.google.get_document(source.document_id))
        native_path = f.path('fixed-preemitted-native-plan')
        actual = f.ledger.plan_spawn(f.source(), self.route, native_path, f.package)
        task = '/root/audit/' + actual['arguments']['task_name']; receipt = f.path('actual-receipt')
        # This old exact same-root snapshot does not re-authorize any operation.
        # Recording derives proof from the reserved plan and actual call evidence.
        recorder = global_native.NativeLedger(f.ledger.root, original.state, f.code, f.ledger.identity)
        self.assertEqual(recorder.record_spawn(native_path, actual['arguments'], {'task_name': task}, receipt)
                         ['native_task_id'], task)
        with self.assertRaisesRegex(ProtocolError, 'observation_rollback'):
            recorder.import_child_admission(original, self.route, receipt, task)
        self.assertEqual(list(f.ledger.child_state_dir(self.route).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
