"""Synthetic signed ready/heartbeat competition; no external provider calls."""
import copy
import json
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from remote_tests import test_global_control as fixtures
from remote_transport import global_control as queue
from remote_transport.control import CASConflict
from remote_transport.global_gateway import private_write
from remote_transport.global_native import NativeLedger
from remote_transport.model import ProtocolError, canonical


class HeartbeatReadyHandoffTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.GlobalControlTests('test_join_heartbeat_claim_and_signed_projection')
        self.f.setUp();self.addCleanup(self.f.tearDown)
        self.now=time.time()+1
        clock=patch('time.time',side_effect=lambda:self.now);clock.start();self.addCleanup(clock.stop)

    def admitted(self):
        rid,_,_=self.f.demand();return rid,self.f.native(rid)

    def heartbeat(self):
        f=self.f;path=f.path('heartbeat.json')
        result=f.ledger.plan_event(f.bridge.read(),'heartbeat',path)
        return path,result

    def saved(self):return json.loads(self.f.ledger.path.read_bytes())

    def test_pending_ready_defers_before_plan_or_reservation_and_never_renews_time(self):
        self.admitted();before=self.saved();calls=len(self.f.google.calls)
        path,first=self.heartbeat()
        self.assertEqual(first['action'],'heartbeat_deferred_for_ready')
        self.assertTrue(first['read_only']);self.assertFalse(first['write_attempted'])
        self.assertFalse(first['heartbeat_verified']);self.assertFalse(first['native_spawn_allowed'])
        self.assertEqual(first['pending_ready_count'],1);self.assertLessEqual(first['recheck_after_seconds'],25)
        self.assertFalse(path.exists());self.assertNotIn('plan_file',first);self.assertNotIn('tool_arguments',first)
        self.now+=25;path,second=self.heartbeat()
        self.assertEqual(first['heartbeat_at'],second['heartbeat_at'])
        self.assertEqual(first['observe_before'],second['observe_before'])
        self.assertEqual(self.saved()['operations'],before['operations'])
        self.assertEqual(self.saved()['spawns'],before['spawns']);self.assertFalse(path.exists())
        self.assertEqual(len(self.f.google.calls),calls)

    def test_competing_ready_wins_read_only_handoff_then_fresh_heartbeat_verifies(self):
        f=self.f;rid,receipt=self.admitted();_,deferred=self.heartbeat()
        stale_admitted=f.bridge.read()
        f.child_admit(rid,receipt)  # Mac commits the genuine signed ready transition.
        # Match the observed race: READY commits after the parent's read but
        # before its helper plans a heartbeat. The stale admitted snapshot must
        # still defer rather than issue a CAS against its now-obsolete revision.
        stale_path=f.path('stale-heartbeat.json');before=self.saved()['operations']
        self.assertEqual(f.ledger.plan_event(stale_admitted,'heartbeat',stale_path)['action'],
                         'heartbeat_deferred_for_ready')
        self.assertFalse(stale_path.exists());self.assertEqual(self.saved()['operations'],before)
        source=f.bridge.read();self.assertEqual(source.state['logical']['demands'][rid]['state'],'ready')
        self.now+=1;path,plan=self.heartbeat()
        self.assertEqual(plan['tool_arguments']['write_control'],{'requiredRevisionId':source.revision_id})
        self.assertLessEqual(plan['execute_before'],deferred['heartbeat_at']+900)
        self.assertLessEqual(plan['execute_before'],self.now+120)
        response=f.google.batch_update_document(**plan['tool_arguments'])
        self.assertTrue(f.ledger.verify_plan(path,response,f.google.get_document(f.initial['document_id']))['verified'])

    def test_one_ready_route_does_not_release_another_admitted_route(self):
        f=self.f;one,receipt_one=self.admitted();two,receipt_two=self.admitted()
        self.assertEqual(self.heartbeat()[1]['pending_ready_count'],2)
        f.child_admit(one,receipt_one)
        path,deferred=self.heartbeat();self.assertEqual(deferred['pending_ready_count'],1);self.assertFalse(path.exists())
        f.child_admit(two,receipt_two);self.now+=1
        self.assertIn('tool_arguments',self.heartbeat()[1])

    def test_child_deadline_ends_handoff_without_allocating_a_plan(self):
        f=self.f;f.bridge.bootstrap_seconds=60;rid,_=self.admitted()
        _,deferred=self.heartbeat()
        self.assertEqual(deferred['observe_before'],f.bridge.read().state['logical']['demands'][rid]['child_bootstrap']['expires'])
        self.now=deferred['observe_before'];before=self.saved()['operations'];path=f.path('expired.json')
        with self.assertRaisesRegex(ProtocolError,'readiness_window_expired'):
            f.ledger.plan_event(f.bridge.read(),'heartbeat',path)
        self.assertFalse(path.exists());self.assertEqual(self.saved()['operations'],before)

    def test_freshness_expiry_cannot_be_reported_as_deferred_or_revived(self):
        f=self.f;f.bridge.bootstrap_seconds=1800;self.admitted();_,deferred=self.heartbeat()
        self.assertEqual(deferred['observe_before'],deferred['heartbeat_at']+900)
        self.now=deferred['observe_before']
        with self.assertRaisesRegex(ProtocolError,'not_active'):self.heartbeat()
        self.now-=1
        with self.assertRaisesRegex(ProtocolError,'not_active'):self.heartbeat()

    def test_lease_deadline_is_not_extended_by_repeated_read_only_handoffs(self):
        f=self.f;f.bridge.bootstrap_seconds=1800;self.admitted();c=f.bridge.read().state['logical']['controller']
        # The existing fixture lease is 1200s; keep the current heartbeat fresh
        # through a signed peer event, without changing the original lease.
        source=f.bridge.read();self.now=c['lease_expires']-500
        newer=queue.transition(source.state,f.code,'heartbeat','native',
            {'native_task_id':c['native_task_id'],'controller_epoch':c['controller_epoch']})
        packet=queue.plan(source,newer,f.code);f.google.batch_update_document(**packet['tool_arguments'])
        _,deferred=self.heartbeat();self.assertEqual(deferred['observe_before'],c['lease_expires'])
        self.now=c['lease_expires']
        with self.assertRaisesRegex(ProtocolError,'not_active'):self.heartbeat()

    def test_closed_queue_cannot_return_deferred(self):
        self.admitted();self.f.bridge.event('close',{'confirm':True})
        with self.assertRaisesRegex(ProtocolError,'queue_closed'):self.heartbeat()

    def test_existing_unknown_write_still_blocks_even_when_a_route_is_admitted(self):
        f=self.f;rid,_,_=f.demand();f.execute('claim',rid);f.execute('begin',rid)
        spawn=f.path('spawn.json');out=f.ledger.plan_spawn(f.bridge.read(),rid,spawn,f.package)
        f.ledger.record_spawn(spawn,out['arguments'],{'task_name':'/root/offline/'+out['arguments']['task_name']},f.path('receipt.json'))
        path=f.path('admitted.json');plan=f.ledger.plan_event(f.bridge.read(),'admitted',path,route_id=rid)
        f.google.batch_update_document(**plan['tool_arguments'])  # Intentionally lost/unverified outcome.
        before=self.saved()['operations']
        with self.assertRaisesRegex(ProtocolError,'unresolved_cas'):self.heartbeat()
        self.assertEqual(self.saved()['operations'],before)
        f.ledger.verify_plan(path,None,f.google.get_document(f.initial['document_id']))
        self.assertEqual(self.heartbeat()[1]['action'],'heartbeat_deferred_for_ready')

    def test_proven_fixture_conflict_does_not_release_untrusted_connector_reservation(self):
        f=self.f;self.now+=25;path,plan=self.heartbeat()
        f.demand()  # A different valid Mac event wins after the heartbeat snapshot.
        with self.assertRaises(CASConflict):f.google.batch_update_document(**plan['tool_arguments'])
        with self.assertRaisesRegex(ProtocolError,'not_observed'):
            f.ledger.verify_plan(path,None,f.google.get_document(f.initial['document_id']))
        before=self.saved()['operations'];self.now+=1
        with self.assertRaisesRegex(ProtocolError,'unresolved_cas'):self.heartbeat()
        self.assertEqual(self.saved()['operations'],before)
        restarted=NativeLedger(f.ledger.root,f.bridge.read().state,f.code,f.ledger.identity)
        with self.assertRaisesRegex(ProtocolError,'unresolved_cas'):
            restarted.plan_event(f.bridge.read(),'heartbeat',f.path('restart.json'))

    def test_ready_does_not_reconcile_an_unobserved_heartbeat_or_extend_its_deadline(self):
        f=self.f;rid,receipt=self.admitted();f.child_admit(rid,receipt);self.now+=25
        path,plan=self.heartbeat();f.demand()
        with self.assertRaises(CASConflict):f.google.batch_update_document(**plan['tool_arguments'])
        self.now=plan['execute_before']
        with self.assertRaisesRegex(ProtocolError,'not_observed'):
            f.ledger.verify_plan(path,None,f.google.get_document(f.initial['document_id']))
        record=next(r for r in self.saved()['operations'].values() if r['operation_id']==plan['operation_id'])
        self.assertEqual(record['status'],'outcome_unknown_no_replay')

    def test_exact_heartbeat_after_ready_still_cannot_be_accepted_at_original_deadline(self):
        f=self.f;rid,receipt=self.admitted();self.heartbeat();f.child_admit(rid,receipt);self.now+=25
        path,plan=self.heartbeat();f.google.batch_update_document(**plan['tool_arguments'])
        self.now=plan['execute_before']
        with self.assertRaisesRegex(ProtocolError,'acceptance_window_expired'):
            f.ledger.verify_plan(path,None,f.google.get_document(f.initial['document_id']))
        record=next(r for r in self.saved()['operations'].values() if r['operation_id']==plan['operation_id'])
        self.assertTrue(record['last_failure']['readback']['expected_event_observed'])
        self.assertEqual(record['status'],'outcome_unknown_no_replay')

    def test_wrong_root_and_signed_history_fork_cannot_yield_a_scheduling_hint(self):
        f=self.f;self.admitted();self.heartbeat();source=f.bridge.read()
        altered=copy.deepcopy(source.state);altered['activation_id']='a'*32
        foreign=queue.Snapshot(source.document_id,source.tab_id,source.revision_id,queue.block(altered))
        with self.assertRaises(ProtocolError):f.ledger.plan_event(foreign,'heartbeat',f.path('foreign.json'))
        historical=queue.Snapshot(source.document_id,source.tab_id,'old-revision',queue.block(f.initial))
        with self.assertRaisesRegex(ProtocolError,'rollback_or_fork'):
            f.ledger.plan_event(historical,'heartbeat',f.path('old.json'))

    def test_signed_text_from_wrong_document_or_tab_cannot_yield_a_hint(self):
        f=self.f;self.admitted();source=f.bridge.read();before=f.ledger.path.read_bytes()
        for document_id,tab_id in [('foreign-doc',source.tab_id),(source.document_id,'foreign-tab')]:
            with self.subTest(document_id=document_id,tab_id=tab_id):
                wrong=queue.Snapshot(document_id,tab_id,source.revision_id,source.text)
                with self.assertRaisesRegex(ProtocolError,'global_document_mismatch'):
                    f.ledger.plan_event(wrong,'heartbeat',f.path('wrong-target.json'))
                self.assertEqual(f.ledger.path.read_bytes(),before)

    def test_cli_inline_dispatch_check_returns_read_only_result_without_plan(self):
        f=self.f;self.admitted();code=f.path('code');private_write(code,f.code.encode());path=f.path('unused-plan.json')
        envelope={'snapshot':f.google.get_document(f.initial['document_id']),'response':None}
        script='import runpy,time; time.time=lambda:'+repr(self.now)+'; runpy.run_module("remote_transport.global_native",run_name="__main__")'
        result=subprocess.run([sys.executable,'-B','-c',script,'plan-heartbeat','--inline-evidence','--check-cas-now',
            '--save',str(path),'--document-id',f.initial['document_id'],'--tab-id','t.0','--join-code-file',str(code),
            '--state-dir',str(f.ledger.root),'--native-task-id',f.ledger.identity],input=json.dumps(envelope),
            text=True,capture_output=True,cwd=f.package,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        deferred=json.loads(result.stdout);self.assertEqual(deferred['action'],'heartbeat_deferred_for_ready')
        self.assertIn('snapshot_file',deferred);self.assertNotIn('dispatch_check',deferred);self.assertFalse(path.exists())

    def test_cleanup_validation_dependencies_are_in_signed_source_bindings(self):
        sources=self.f.initial['controller_source_hashes']
        from remote_transport.model import hash_bytes
        for relative in ('remote_transport/legacy_cleanup.py','remote_transport/model.py','remote_transport/control.py',
                         'examples/google_clients.py'):
            self.assertEqual(sources[relative],hash_bytes((self.f.package/relative).read_bytes()))
        self.assertIn('remote_transport/selection.py',self.f.initial['runtime_source_hashes'])


if __name__=='__main__':unittest.main()
