"""Independent atomic-startup fence audit. Synthetic ports and clocks only."""
import copy
import json
import unittest
from unittest.mock import patch

from remote_tests import test_global_heartbeat as fixtures
from remote_transport import global_control as queue, global_native
from remote_transport.model import ProtocolError


class AtomicStartupAuditTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.GlobalHeartbeatTests('test_exact_900_root_policy_and_no_old_protocol_migration')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.join(latency=0)
        self.rid = self.f.demand()

    def group(self):
        f = self.f
        source = f.source()
        path = f.path('audit-group')
        result = f.ledger.plan_claim_begin(source, self.rid, path)
        return source, path, result, json.loads(path.read_text())

    def resource(self, state, revision='audit-new-revision'):
        source = self.f.google.get_document(self.f.initial['document_id'])
        source['revisionId'] = revision
        source['tabs'][0]['documentTab']['body']['content'][1]['paragraph']['elements'][0]['textRun']['content'] = queue.block(state)
        return source

    def test_group_is_one_replacement_with_ordered_distinct_ids(self):
        source, path, result, packet = self.group()
        self.assertEqual(packet['contract'], queue.GROUP_PLAN_CONTRACT)
        self.assertEqual(len(packet['tool_arguments']['requests']), 1)
        self.assertEqual(packet['tool_arguments']['write_control'], {'requiredRevisionId': source.revision_id})
        events = packet['expected_state']['events'][-2:]
        self.assertEqual([event['kind'] for event in events], ['claim', 'begin'])
        self.assertEqual(packet['operation_ids'], [event['operation_id'] for event in events])
        self.assertEqual(len(set(packet['operation_ids'])), 2)
        self.assertTrue(self.f.ledger.check_plan(source, path)['dispatch_allowed'])
        before = len(self.f.google.calls)
        response = self.f.google.batch_update_document(**result['tool_arguments'])
        accepted = self.f.ledger.verify_plan(path, response, self.f.google.get_document(source.document_id))
        self.assertTrue(accepted['verified'])
        self.assertEqual(len(self.f.google.calls), before+1)

    def test_partial_claim_readback_burns_both_semantics_and_native(self):
        f = self.f
        source, path, _, packet = self.group()
        claim = packet['expected_state']['events'][-2]
        partial = queue.transition(source.state, f.code, 'claim', 'native', claim['arguments'],
                                   now=claim['at'], operation_id=claim['operation_id'])
        with self.assertRaisesRegex(ProtocolError, 'not_observed'):
            f.ledger.verify_plan(path, None, self.resource(partial))
        for kind in ('claim', 'begin', 'claim-begin'):
            with self.subTest(kind=kind), self.assertRaisesRegex(ProtocolError, 'unresolved'):
                f.ledger.plan_event(source, kind, f.path('retry'), route_id=self.rid)
        with self.assertRaisesRegex(ProtocolError, 'unresolved'):
            f.ledger.plan_spawn(source, self.rid, f.path('spawn'), f.package)

    def test_full_signed_prefix_allows_authentic_later_event_without_rewrite(self):
        f = self.f
        source, path, result, packet = self.group()
        f.google.batch_update_document(**result['tool_arguments'])
        f.clock += 1
        state = packet['expected_state']
        c = state['logical']['controller']
        later = queue.transition(state, f.code, 'heartbeat', 'native',
            {'native_task_id': c['native_task_id'], 'controller_epoch': c['controller_epoch']})
        accepted = f.ledger.verify_plan(path, None, self.resource(later))
        self.assertTrue(accepted['verified'])
        self.assertEqual(accepted['epoch'], packet['expected_state']['epoch']+1)

    def test_single_contract_cannot_accept_pair_and_group_cannot_include_extra_event(self):
        f = self.f
        source, _, _, packet = self.group()
        with self.assertRaisesRegex(ProtocolError, 'one_transition'):
            queue.plan(source, packet['expected_state'], f.code)
        f.clock += 1
        state = packet['expected_state']; c = state['logical']['controller']
        later = queue.transition(state, f.code, 'heartbeat', 'native',
            {'native_task_id': c['native_task_id'], 'controller_epoch': c['controller_epoch']})
        with self.assertRaisesRegex(ProtocolError, 'group_not_claim_begin'):
            queue.plan_claim_begin(source, later, f.code)

    def test_exact_group_schema_rejects_identity_deadline_revision_and_request_tampering(self):
        _, _, _, packet = self.group()
        changes = [lambda p: p.update(unrequested=True),
                   lambda p: p['operation_ids'].reverse(),
                   lambda p: p.update(group_id='0'*64),
                   lambda p: p.update(execute_before=p['execute_before']+1),
                   lambda p: p['tool_arguments']['write_control'].update(targetRevisionId='r1'),
                   lambda p: p['tool_arguments']['requests'].append(copy.deepcopy(p['tool_arguments']['requests'][0]))]
        for change in changes:
            bad = copy.deepcopy(packet); change(bad)
            with self.subTest(packet=list(bad)), self.assertRaises(ProtocolError):
                queue.validate_plan(bad, self.f.code)

    def test_same_revision_and_different_signed_claim_prefix_cannot_accept_group(self):
        f = self.f
        source, path, _, packet = self.group()
        with self.assertRaisesRegex(ProtocolError, 'not_observed'):
            queue.verify_update(packet, None, self.resource(packet['expected_state'], source.revision_id), f.code)
        claim = packet['expected_state']['events'][-2]
        other_args = {**claim['arguments'], 'claim_id': 'f'*32}
        other = queue.transition(source.state, f.code, 'claim', 'native', other_args, now=claim['at'])
        other = queue.transition(other, f.code, 'begin', 'native',
            {**other_args, 'dispatch_id': queue.dispatch_id(other, self.rid)}, now=claim['at'])
        with self.assertRaisesRegex(ProtocolError, 'not_observed'):
            f.ledger.verify_plan(path, None, self.resource(other))

    def test_output_failure_after_reservation_cannot_regenerate_group_or_legacy(self):
        f = self.f
        source = f.source(); target = f.path('failed-output')
        original = global_native.private_write
        def fail_only_output(path, raw):
            if path == target:
                raise OSError('synthetic output failure')
            return original(path, raw)
        with patch.object(global_native, 'private_write', side_effect=fail_only_output):
            with self.assertRaisesRegex(OSError, 'synthetic'):
                f.ledger.plan_claim_begin(source, self.rid, target)
        self.assertFalse(target.exists())
        record = json.loads(f.ledger.path.read_text())['operations']['claim-begin:'+self.rid]
        self.assertEqual(record['status'], 'issued_outcome_unknown')
        restarted = global_native.NativeLedger(f.ledger.root, source.state, f.code, f.ledger.identity)
        for kind in ('claim', 'begin', 'claim-begin'):
            with self.subTest(kind=kind), self.assertRaisesRegex(ProtocolError, 'unresolved'):
                restarted.plan_event(source, kind, f.path('replacement'), route_id=self.rid)

    def test_native_plan_after_verified_group_retains_exact_ten_second_expiry(self):
        f = self.f
        source, path, result, packet = self.group()
        response = f.google.batch_update_document(**result['tool_arguments'])
        f.ledger.verify_plan(path, response, f.google.get_document(source.document_id))
        for kind in ('claim', 'begin', 'claim-begin'):
            with self.subTest(kind=kind), self.assertRaisesRegex(ProtocolError, 'already_issued'):
                f.ledger.plan_event(f.source(), kind, f.path('replay'), route_id=self.rid)
        spawn = f.path('native')
        fresh = f.source(); result = f.ledger.plan_spawn(fresh, self.rid, spawn, f.package)
        self.assertEqual(result['execute_before'], f.clock+10)
        f.clock += 9.999
        self.assertTrue(f.ledger.check_spawn(fresh, spawn)['dispatch_allowed'])
        f.clock += .001
        with self.assertRaisesRegex(ProtocolError, 'dispatch_window_expired'):
            f.ledger.check_spawn(fresh, spawn)

    def test_desktop_pause_dispatch_gate_is_bound_to_signed_source(self):
        from pathlib import Path
        from remote_transport.model import hash_bytes
        root = Path(queue.__file__).resolve().parents[1]
        self.assertEqual(queue.controller_source_hashes()['remote_transport/global_desktop.py'],
                         hash_bytes((root/'remote_transport/global_desktop.py').read_bytes()))


if __name__ == '__main__':
    unittest.main()
