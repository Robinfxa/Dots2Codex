"""Private categorical JOIN diagnostics; synthetic Docs and clock only."""
import json
import unittest

from remote_tests import test_global_heartbeat as fixtures
from remote_transport.model import ProtocolError


class GlobalNativeDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.GlobalHeartbeatTests('test_exact_900_root_policy_and_no_old_protocol_migration')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def failure_record(self):
        saved=json.loads(self.f.ledger.path.read_text())
        return saved,next(iter(saved['operations'].values()))

    def test_closed_before_join_never_reserves_a_write(self):
        f=self.f
        f.bridge.event('close',{'confirm':True})
        before=len(f.google.calls)
        with self.assertRaisesRegex(ProtocolError,'global_queue_closed'):
            f.plan('join',capacity=2,seconds=1200)
        saved=json.loads(f.ledger.path.read_text())
        self.assertEqual(saved['operations'],{})
        self.assertEqual(saved['spawns'],{})
        self.assertEqual(len(f.google.calls),before)

    def test_closed_readback_without_join_stays_unknown_and_preserves_both_facts(self):
        f=self.f
        path,_=f.plan('join',capacity=2,seconds=1200)
        f.bridge.event('close',{'confirm':True})
        before=len(f.google.calls)
        with self.assertRaisesRegex(ProtocolError,'global_operation_not_observed_no_replay') as caught:
            f.ledger.verify_plan(path,None,f.google.get_document(f.initial['document_id']))
        expected={'authenticated':True,'queue_closed':True,'expected_event_observed':False}
        self.assertEqual(caught.exception.controller_diagnostic,expected)
        saved,record=self.failure_record()
        self.assertEqual(record['status'],'outcome_unknown_no_replay')
        self.assertEqual(record['last_failure'],{'code':'global_operation_not_observed_no_replay','readback':expected})
        self.assertIsNone(saved['observed_controller'])
        self.assertEqual(saved['spawns'],{})
        with self.assertRaisesRegex(ProtocolError,'global_queue_closed'):
            f.plan('join',capacity=2,seconds=1200)
        self.assertEqual(len(f.google.calls),before)

    def test_observed_join_followed_by_close_never_reports_controller_active(self):
        f=self.f
        path,out=f.plan('join',capacity=2,seconds=1200)
        response=f.google.batch_update_document(**out['tool_arguments'])
        f.clock+=1
        f.bridge.event('close',{'confirm':True})
        with self.assertRaisesRegex(ProtocolError,'global_queue_closed'):
            f.ledger.verify_plan(path,response,f.google.get_document(f.initial['document_id']))
        saved,record=self.failure_record()
        self.assertEqual(record['status'],'outcome_unknown_no_replay')
        self.assertEqual(record['last_failure']['readback'],
            {'authenticated':True,'queue_closed':True,'expected_event_observed':True})
        self.assertEqual(saved['terminal_reason'],'global_queue_closed')
        self.assertIsNone(saved['observed_controller'])
        self.assertEqual(saved['spawns'],{})

    def test_matching_event_after_acceptance_deadline_does_not_get_accepted_by_diagnostics(self):
        f=self.f
        path,out=f.plan('join',capacity=2,seconds=1200)
        f.google.batch_update_document(**out['tool_arguments'])
        f.clock+=120
        with self.assertRaisesRegex(ProtocolError,'global_cas_acceptance_window_expired_no_replay'):
            f.ledger.verify_plan(path,None,f.google.get_document(f.initial['document_id']))
        saved,record=self.failure_record()
        self.assertEqual(record['status'],'outcome_unknown_no_replay')
        self.assertEqual(record['last_failure']['readback'],
            {'authenticated':True,'queue_closed':False,'expected_event_observed':True})
        self.assertIsNone(saved['observed_controller'])
        self.assertEqual(saved['spawns'],{})

    def test_invalid_readback_never_produces_authenticated_closed_fact(self):
        f=self.f
        path,_=f.plan('join',capacity=2,seconds=1200)
        with self.assertRaises(ProtocolError):
            f.ledger.verify_plan(path,None,{'documentId':f.initial['document_id'],'revisionId':'fake','tabs':[]})
        saved,record=self.failure_record()
        self.assertEqual(record['last_failure']['readback'],
            {'authenticated':False,'queue_closed':None,'expected_event_observed':None})
        self.assertEqual(record['status'],'outcome_unknown_no_replay')
        self.assertIsNone(saved['observed_controller'])
        self.assertEqual(saved['spawns'],{})


if __name__=='__main__':unittest.main()
