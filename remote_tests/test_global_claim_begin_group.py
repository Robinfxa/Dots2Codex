"""Atomic claim/begin contracts, durable burn fences and real CLI, all offline."""
import copy
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from remote_tests import test_global_heartbeat as fixtures
from remote_transport import global_control as q, global_native as native
from remote_transport.global_gateway import private_write
from remote_transport.model import ProtocolError, canonical


class GlobalClaimBeginGroupTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.GlobalHeartbeatTests('test_exact_900_root_policy_and_no_old_protocol_migration')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.join();self.rid=self.f.demand()

    def plan(self):
        f=self.f;source=f.source();path=f.path('group')
        out=f.ledger.plan_event(source,'claim-begin',path,route_id=self.rid)
        return source,path,out,json.loads(path.read_bytes())

    def commit(self,path,out):
        f=self.f;f.ledger.check_plan(f.source(),path)
        response=f.google.batch_update_document(**out['tool_arguments'])
        return f.ledger.verify_plan(path,response,f.google.get_document(f.initial['document_id']))

    def document(self,state,revision='r-synthetic'):
        f=self.f;document=f.google.get_document(f.initial['document_id'])
        document['revisionId']=revision
        document['tabs'][0]['documentTab']['body']['content'][1]['paragraph']['elements'][0]['textRun']['content']=q.block(state)
        return document

    def assertError(self,pattern,fn,*args,**kwargs):
        with self.assertRaisesRegex(ProtocolError,pattern):fn(*args,**kwargs)

    def test_one_cas_two_normal_events_one_reservation_then_native_window_unchanged(self):
        f=self.f;before=json.loads(f.ledger.path.read_bytes());calls=len(f.google.calls)
        source,path,out,packet=self.plan()
        self.assertEqual(packet['contract'],q.GROUP_PLAN_CONTRACT)
        self.assertNotIn('operation_id',packet);self.assertNotIn('operation_id',out)
        self.assertEqual(packet['group_kind'],'claim-begin')
        self.assertEqual(packet['operation_ids'],[e['operation_id'] for e in packet['expected_state']['events'][-2:]])
        self.assertEqual([e['kind'] for e in packet['expected_state']['events'][-2:]],['claim','begin'])
        self.assertEqual(packet['tool_arguments']['write_control'],{'requiredRevisionId':source.revision_id})
        self.assertEqual(len(packet['tool_arguments']['requests']),1)
        saved=json.loads(f.ledger.path.read_bytes());record=saved['operations']['claim-begin:'+self.rid]
        self.assertEqual(len(saved['operations']),len(before['operations'])+1)
        self.assertEqual(record['status'],'issued_outcome_unknown');self.assertEqual(record['group_id'],packet['group_id'])
        self.assertError('unresolved',f.ledger.plan_spawn,source,self.rid,f.path('early'),f.package)
        verified=self.commit(path,out)
        self.assertTrue(verified['verified']);self.assertEqual(verified['group_id'],packet['group_id'])
        self.assertEqual(len(f.google.calls),calls+1)
        self.assertEqual(f.source().state['epoch'],source.state['epoch']+2)
        spawn=f.path('spawn');prepared=f.ledger.plan_spawn(f.source(),self.rid,spawn,f.package)
        self.assertEqual(prepared['execute_before'],f.clock+10)
        f.clock+=9.999;self.assertTrue(f.ledger.check_spawn(f.source(),spawn)['dispatch_allowed'])
        f.clock+=.001
        self.assertError('dispatch_window',f.ledger.check_spawn,f.source(),spawn)
        self.assertError('already_reserved',f.ledger.plan_spawn,f.source(),self.rid,f.path('replacement'),f.package)

    def test_separate_contract_cannot_be_masqueraded_as_single_or_relaxed_group(self):
        source,_,_,packet=self.plan();f=self.f
        self.assertError('not_one_transition',q.plan,source,packet['expected_state'],f.code)
        changes=[{'contract':q.SINGLE_PLAN_CONTRACT},{'contract':'dots-global-cas-group-plan/2'},
                 {'operation_id':packet['operation_ids'][0]}, {'group_kind':'begin-claim'},
                 {'group_id':'0'*64},{'operation_ids':packet['operation_ids'][::-1]},
                 {'operation_ids':packet['operation_ids'][:1]}, {'execute_before':packet['execute_before']+1},
                 {'skip_validation':True}, {'tab_id':'other-tab'}]
        for change in changes:
            with self.subTest(change=change):
                self.assertError('cas|group',q.validate_plan,{**packet,**change},f.code)
        for field in packet:
            bad=copy.deepcopy(packet);del bad[field]
            with self.subTest(missing=field):self.assertError('cas',q.validate_plan,bad,f.code)
        bad=copy.deepcopy(packet);bad['tool_arguments']['requests'].append({})
        self.assertError('cas_plan_changed',q.validate_plan,bad,f.code)

    def test_group_rejects_cross_route_begin_even_when_both_events_individually_valid(self):
        f=self.f;other=f.demand();f.event('claim',route_id=other);source=f.source();old=source.state
        controller=old['logical']['controller'];owned=old['logical']['demands'][other]
        args={'native_task_id':f.ledger.identity,'controller_epoch':controller['controller_epoch'],
              'route_id':self.rid,'claim_id':'c'*32}
        claimed=q.transition(old,f.code,'claim','native',args)
        begun=q.transition(claimed,f.code,'begin','native',{**args,'route_id':other,
            'claim_id':owned['claim_id'],'dispatch_id':q.dispatch_id(claimed,other)})
        self.assertError('group_not_claim_begin',q.plan_claim_begin,source,begun,f.code)

    def test_group_rejects_intervening_event_or_distinct_prepare_times(self):
        f=self.f;source,_,_,packet=self.plan();old=source.state;claim,begin=packet['expected_state']['events'][-2:]
        claimed=q.transition(old,f.code,'claim','native',claim['arguments'],now=claim['at'])
        begun=q.transition(claimed,f.code,'begin','native',begin['arguments'],now=claim['at']+1)
        self.assertError('group_not_claim_begin',q.plan_claim_begin,source,begun,f.code)
        c=claimed['logical']['controller']
        middle=q.transition(claimed,f.code,'heartbeat','native',
            {'native_task_id':c['native_task_id'],'controller_epoch':c['controller_epoch']},now=claim['at']+1)
        begun=q.transition(middle,f.code,'begin','native',begin['arguments'],now=claim['at']+1)
        self.assertError('group_not_claim_begin',q.plan_claim_begin,source,begun,f.code)

    def test_partial_claim_readback_burns_whole_group_and_never_exposes_native(self):
        f=self.f;source,path,_,packet=self.plan();claim=packet['expected_state']['events'][-2]
        partial=q.transition(source.state,f.code,'claim','native',claim['arguments'],
                             operation_id=claim['operation_id'],now=claim['at'])
        self.assertError('not_observed',f.ledger.verify_plan,path,None,self.document(partial))
        saved=json.loads(f.ledger.path.read_bytes());self.assertEqual(saved['operations']['claim-begin:'+self.rid]['status'],
            'outcome_unknown_no_replay');self.assertEqual(saved['spawns'],{})
        for kind in ('claim','begin','claim-begin'):
            self.assertError('unresolved',f.ledger.plan_event,source,kind,f.path('retry'),route_id=self.rid)
        self.assertError('unresolved',f.ledger.plan_spawn,source,self.rid,f.path('spawn'),f.package)

    def test_authenticated_equivalent_projection_with_different_event_ids_is_not_group(self):
        f=self.f;source,path,_,packet=self.plan();claim,begin=packet['expected_state']['events'][-2:]
        fork=q.transition(source.state,f.code,'claim','native',claim['arguments'],now=claim['at'])
        fork=q.transition(fork,f.code,'begin','native',begin['arguments'],now=begin['at'])
        self.assertEqual(fork['logical'],packet['expected_state']['logical'])
        self.assertError('not_observed',f.ledger.verify_plan,path,None,self.document(fork))

    def test_full_group_prefix_with_later_event_reconciles_lost_response_read_only(self):
        f=self.f;_,path,out,packet=self.plan();f.google.lose=True
        with self.assertRaises(TimeoutError):f.google.batch_update_document(**out['tool_arguments'])
        f.clock+=1;c=packet['expected_state']['logical']['controller']
        later=q.transition(packet['expected_state'],f.code,'heartbeat','native',
            {'native_task_id':c['native_task_id'],'controller_epoch':c['controller_epoch']})
        calls=len(f.google.calls);result=f.ledger.verify_plan(path,None,self.document(later))
        self.assertTrue(result['reconciled_from_event']);self.assertEqual(len(f.google.calls),calls)
        self.assertEqual(json.loads(f.ledger.path.read_bytes())['operations']['claim-begin:'+self.rid]['status'],'verified')

    def test_unknown_group_survives_restart_and_blocks_legacy_component_reissue(self):
        f=self.f;source,path,_,_=self.plan()
        self.assertError('not_observed',f.ledger.verify_plan,path,None,f.google.get_document(f.initial['document_id']))
        restarted=native.NativeLedger(f.ledger.root,source.state,f.code,f.ledger.identity)
        for kind in ('claim','begin','claim-begin','heartbeat'):
            self.assertError('unresolved',restarted.plan_event,source,kind,f.path('again'),route_id=self.rid)
        self.assertError('plan_not_reserved',restarted.check_plan,source,path)

    def test_concurrent_demand_causes_exact_revision_conflict_without_group_replay(self):
        from remote_transport.control import CASConflict
        f=self.f;_,path,out,_=self.plan();other=f.demand()
        with self.assertRaises(CASConflict):f.google.batch_update_document(**out['tool_arguments'])
        self.assertError('not_observed',f.ledger.verify_plan,path,None,f.google.get_document(f.initial['document_id']))
        self.assertError('unresolved',f.ledger.plan_event,f.source(),'claim-begin',f.path('retry'),route_id=other)
        self.assertEqual(f.source().state['logical']['demands'][self.rid]['state'],'pending')
        self.assertEqual(json.loads(f.ledger.path.read_bytes())['spawns'],{})

    def test_group_response_still_requires_exact_replacement_and_new_revision(self):
        f=self.f;_,path,out,packet=self.plan();response=f.google.batch_update_document(**out['tool_arguments'])
        readback=f.google.get_document(f.initial['document_id'])
        for count in (0,2,True):
            bad=copy.deepcopy(response);bad['replies'][0]['replaceAllText']['occurrencesChanged']=count
            self.assertError('exact_replace',q.verify_update,packet,bad,readback,f.code)
        bad=copy.deepcopy(response);bad['documentId']='different-document'
        self.assertError('response_document',q.verify_update,packet,bad,readback,f.code)
        bad=copy.deepcopy(response);bad['writeControl']['requiredRevisionId']=packet['tool_arguments']['write_control']['requiredRevisionId']
        self.assertError('response_revision',q.verify_update,packet,bad,readback,f.code)
        self.assertTrue(f.ledger.verify_plan(path,response,readback)['verified'])

    def test_verified_group_and_legacy_components_are_mutually_exclusive(self):
        f=self.f;_,path,out,_=self.plan();self.commit(path,out)
        for kind in ('claim','begin','claim-begin'):
            self.assertError('already_issued',f.ledger.plan_event,f.source(),kind,f.path('again'),route_id=self.rid)
        second=f.demand();f.event('claim',route_id=second)
        self.assertError('already_issued',f.ledger.plan_event,f.source(),'claim-begin',f.path('mixed'),route_id=second)
        f.event('begin',route_id=second)
        self.assertTrue(f.ledger.plan_spawn(f.source(),second,f.path('legacy-spawn'),f.package)['one_attempt_only'])

    def test_failed_plan_output_keeps_one_durable_group_reservation(self):
        f=self.f;source=f.source();destination=f.path('failed-output');original=native.private_write
        def fail_plan(path,raw):
            if path==destination:raise OSError('synthetic output failure')
            return original(path,raw)
        with patch.object(native,'private_write',side_effect=fail_plan):
            with self.assertRaises(OSError):f.ledger.plan_event(source,'claim-begin',destination,route_id=self.rid)
        self.assertFalse(destination.exists())
        saved=json.loads(f.ledger.path.read_bytes());record=saved['operations']['claim-begin:'+self.rid]
        self.assertEqual(record['status'],'issued_outcome_unknown');self.assertEqual(len(record['operation_ids']),2)
        restarted=native.NativeLedger(f.ledger.root,source.state,f.code,f.ledger.identity)
        self.assertError('unresolved',restarted.plan_event,source,'claim-begin',f.path('retry'),route_id=self.rid)

    def test_deadline_and_capacity_are_not_extended(self):
        f=self.f;_,path,out,packet=self.plan();self.assertEqual(packet['execute_before'],f.clock+120)
        f.clock=packet['execute_before']
        self.assertError('dispatch_window',f.ledger.check_plan,f.source(),path)
        response=f.google.batch_update_document(**out['tool_arguments'])
        self.assertError('acceptance_window',f.ledger.verify_plan,path,response,f.google.get_document(f.initial['document_id']))

    def test_capacity_failure_reserves_no_group_and_leaves_pending(self):
        f=self.f;_,path,out,_=self.plan();self.commit(path,out)
        second=f.demand();f.event('claim-begin',route_id=second)
        third=f.demand();before=json.loads(f.ledger.path.read_bytes())['operations'];target=f.path('full')
        self.assertError('slots_exhausted',f.ledger.plan_event,f.source(),'claim-begin',target,route_id=third)
        self.assertFalse(target.exists());self.assertEqual(json.loads(f.ledger.path.read_bytes())['operations'],before)
        self.assertEqual(f.source().state['logical']['demands'][third]['state'],'pending')

    def test_group_claim_requires_same_actual_controller_and_fresh_lease(self):
        f=self.f;source=f.source();other=native.NativeLedger(f.root/'other-controller',source.state,f.code,'/root/other')
        self.assertError('actual_controller_identity',other.plan_event,source,'claim-begin',f.path('other'),route_id=self.rid)
        f.clock=source.state['logical']['controller']['heartbeat_at']+900
        self.assertError('not_active',f.ledger.plan_event,source,'claim-begin',f.path('stale'),route_id=self.rid)

    def test_demand_expiry_is_a_group_dispatch_and_acceptance_deadline(self):
        f=self.f;original=f.source();state=f.initial
        # Shorten only the signed demand lifetime to its already valid child
        # lifetime, replaying all ordinary checks and MAC generation exactly.
        for event in original.state['events']:
            args=copy.deepcopy(event['arguments'])
            if event['kind']=='demand':args['expires']=args['child_bootstrap']['expires']
            state=q.transition(state,f.code,event['kind'],event['actor'],args,
                operation_id=event['operation_id'],now=event['at'])
        expires=state['logical']['demands'][self.rid]['expires'];f.clock=expires-10
        c=state['logical']['controller']
        state=q.transition(state,f.code,'heartbeat','native',
            {'native_task_id':c['native_task_id'],'controller_epoch':c['controller_epoch']})
        source=q.snapshot(self.document(state),f.initial['document_id'],'t.0');path=f.path('short-demand')
        out=f.ledger.plan_event(source,'claim-begin',path,route_id=self.rid)
        self.assertEqual(out['execute_before'],expires)
        f.clock=expires
        self.assertError('dispatch_window',f.ledger.check_plan,source,path)
        packet=json.loads(path.read_bytes())
        self.assertError('acceptance_window',f.ledger.verify_plan,path,None,
                         self.document(packet['expected_state'],'r-next'))

    def test_missing_first_heartbeat_or_closed_queue_reserves_no_group(self):
        f=self.f;event=f.source().state['events'][0]
        joined=q.transition(f.initial,f.code,'join','native',event['arguments'],
                            operation_id=event['operation_id'],now=event['at'])
        source=q.snapshot(self.document(joined),f.initial['document_id'],'t.0')
        ledger=native.NativeLedger(f.root/'uninitialized',f.initial,f.code,f.ledger.identity)
        self.assertError('first_heartbeat',ledger.plan_event,source,'claim-begin',f.path('uninitialized'),route_id=self.rid)
        self.assertEqual(json.loads(ledger.path.read_bytes())['operations'],{})
        before=json.loads(f.ledger.path.read_bytes())['operations'];f.bridge.event('close',{'confirm':True})
        self.assertError('queue_closed',f.ledger.plan_event,f.source(),'claim-begin',f.path('closed'),route_id=self.rid)
        self.assertEqual(json.loads(f.ledger.path.read_bytes())['operations'],before)

    def test_full_group_followed_by_close_remains_burned_and_cannot_spawn(self):
        f=self.f;_,path,out,_=self.plan();response=f.google.batch_update_document(**out['tool_arguments'])
        f.bridge.event('close',{'confirm':True})
        self.assertError('queue_closed',f.ledger.verify_plan,path,response,f.google.get_document(f.initial['document_id']))
        saved=json.loads(f.ledger.path.read_bytes());record=saved['operations']['claim-begin:'+self.rid]
        self.assertEqual(record['status'],'outcome_unknown_no_replay')
        self.assertEqual(record['last_failure']['readback'],
            {'authenticated':True,'queue_closed':True,'expected_event_observed':True})
        self.assertEqual(saved['spawns'],{})
        self.assertError('queue_closed',f.ledger.plan_spawn,f.source(),self.rid,f.path('closed-spawn'),f.package)

    def test_group_record_tamper_cannot_authorize_native_plan(self):
        f=self.f;_,path,out,_=self.plan();self.commit(path,out)
        saved=json.loads(f.ledger.path.read_bytes());saved['operations']['claim-begin:'+self.rid]['operation_ids'].reverse()
        private_write(f.ledger.path,canonical(saved))
        self.assertError('spawn_binding',f.ledger.plan_spawn,f.source(),self.rid,f.path('bad-record'),f.package)
        self.assertEqual(json.loads(f.ledger.path.read_bytes())['spawns'],{})

    def test_grouped_native_receipt_and_admitted_import_preserve_separate_child_ledger(self):
        f=self.f;_,path,out,_=self.plan();self.commit(path,out)
        spawn=f.path('spawn');native_plan=f.ledger.plan_spawn(f.source(),self.rid,spawn,f.package)
        f.ledger.check_spawn(f.source(),spawn)
        receipt=f.path('receipt');task='/root/offline/'+native_plan['arguments']['task_name']
        f.ledger.record_spawn(spawn,native_plan['arguments'],{'task_name':task},receipt)
        f.event('admitted',route_id=self.rid)
        result=f.ledger.import_child_admission(f.source(),self.rid,receipt,task)
        self.assertTrue(result['imported']);self.assertEqual(result['child_state_dir'],str(f.ledger.child_state_dir(self.rid)))
        self.assertEqual(len(json.loads(f.ledger.path.read_bytes())['spawns']),1)

    def run_cli(self,operation,*extras,success=True):
        f=self.f;snapshot=f.path('snapshot');code=f.path('code')
        private_write(snapshot,canonical(f.google.get_document(f.initial['document_id'])));private_write(code,f.code.encode())
        script='import runpy,time; time.time=lambda: '+repr(f.clock)+'; runpy.run_module("remote_transport.global_native",run_name="__main__")'
        result=subprocess.run([sys.executable,'-B','-c',script,operation,'--snapshot',str(snapshot),
            '--document-id',f.initial['document_id'],'--tab-id','t.0','--join-code-file',str(code),
            '--state-dir',str(f.ledger.root),'--native-task-id',f.ledger.identity,*map(str,extras)],
            cwd=f.package,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode==0,success,result.stdout+result.stderr)
        return json.loads(result.stdout)

    def test_real_cli_group_inline_check_and_first_chunk_are_full_exact_plan(self):
        f=self.f;target=f.path('cli-plan');result=f.path('cli-result')
        chunk=self.run_cli('plan-claim-begin','--route-id',self.rid,'--save',target,
                           '--check-cas-now','--result-file',result,'--result-first-chunk')
        from remote_transport.connector_files import packet_chunk
        self.assertEqual(chunk,packet_chunk(result));value=json.loads(result.read_bytes())
        self.assertTrue(value['dispatch_check']['dispatch_allowed'])
        self.assertEqual(value['dispatch_check']['execute_before'],value['execute_before'])
        packet=json.loads(target.read_bytes());q.validate_plan(packet,f.code)
        self.assertEqual(value['operation_ids'],packet['operation_ids'])
        self.commit(target,value)

    def test_compact_cli_inspect_and_invalid_flags_never_reserve(self):
        f=self.f;status=self.run_cli('inspect-heartbeat')
        self.assertEqual(set(status),{'controller','first_heartbeat_required','controller_timing'})
        before=json.loads(f.ledger.path.read_bytes())['operations'];target=f.path('invalid-flags')
        result=self.run_cli('plan-claim-begin','--route-id',self.rid,'--save',target,
                            '--result-first-chunk',success=False)
        self.assertEqual(result['error'],'global_first_chunk_requires_result_file')
        result=self.run_cli('plan-native','--route-id',self.rid,'--save',target,'--check-cas-now',success=False)
        self.assertEqual(result['error'],'global_inline_cas_check_requires_event_plan')
        self.assertEqual(json.loads(f.ledger.path.read_bytes())['operations'],before);self.assertFalse(target.exists())


if __name__=='__main__':unittest.main()
