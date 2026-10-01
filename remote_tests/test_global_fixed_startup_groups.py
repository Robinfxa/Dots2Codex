"""Explicit post-native and first-JOIN groups, all synthetic/offline."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from remote_tests import test_global_heartbeat as fixtures
from remote_transport import global_control as q, global_native as native
from remote_transport.global_gateway import private_write
from remote_transport.model import ProtocolError, canonical, hash_bytes


class FixedStartupGroupTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.GlobalHeartbeatTests('test_exact_900_root_policy_and_no_old_protocol_migration')
        self.f.setUp();self.addCleanup(self.f.doCleanups)

    def assertError(self,pattern,fn,*args,**kwargs):
        with self.assertRaisesRegex(ProtocolError,pattern):fn(*args,**kwargs)

    def start(self,*,record=True,claim_kind='claim-begin',claim_delay=0):
        f=self.f;f.join();self.rid=f.demand();f.clock+=claim_delay;f.event(claim_kind,route_id=self.rid)
        self.spawn=f.path('spawn');self.native_plan=f.ledger.plan_spawn(f.source(),self.rid,self.spawn,f.package)
        f.ledger.check_spawn(f.source(),self.spawn)
        self.receipt=f.path('receipt');self.task='/root/offline/'+self.native_plan['arguments']['task_name']
        if record:f.ledger.record_spawn(self.spawn,self.native_plan['arguments'],{'task_name':self.task},self.receipt)
        f.clock+=1

    def plan(self,kind='heartbeat-admitted',**kwargs):
        f=self.f;source=f.source();path=f.path(kind)
        out=f.ledger.plan_event(source,kind,path,**({'route_id':self.rid} if hasattr(self,'rid') else {}),**kwargs)
        return source,path,out,json.loads(path.read_bytes())

    def document(self,state,revision='r-synthetic'):
        f=self.f;doc=f.google.get_document(f.initial['document_id']);doc['revisionId']=revision
        doc['tabs'][0]['documentTab']['body']['content'][1]['paragraph']['elements'][0]['textRun']['content']=q.block(state)
        return doc

    def import_admission(self):
        f=self.f;return f.ledger.import_child_admission(f.source(),self.rid,self.receipt,self.task)

    def run_cli(self,operation,*extras,success=True,inline=None,raw=None):
        f=self.f;snapshot=f.path('snapshot');code=f.path('code')
        private_write(snapshot,canonical(f.google.get_document(f.initial['document_id'])));private_write(code,f.code.encode())
        script='import runpy,time; clock=['+repr(f.clock)+']; time.time=lambda: clock[0]; time.sleep=lambda n: clock.__setitem__(0,clock[0]+n); runpy.run_module("remote_transport.global_native",run_name="__main__")'
        use_inline=inline is not None or raw is not None
        result=subprocess.run([sys.executable,'-B','-c',script,operation,
            *(['--inline-evidence'] if use_inline else ['--snapshot',str(snapshot)]),
            '--document-id',f.initial['document_id'],'--tab-id','t.0','--join-code-file',str(code),
            '--state-dir',str(f.ledger.root),'--native-task-id',f.ledger.identity,*map(str,extras)],
            cwd=f.package,capture_output=True,text=True,timeout=15,
            input=raw if raw is not None else json.dumps(inline) if inline is not None else None)
        self.assertEqual(result.returncode==0,success,result.stdout+result.stderr)
        return json.loads(result.stdout)

    def test_post_native_one_exact_cas_two_events_one_record_and_import(self):
        self.start();f=self.f;before=json.loads(f.ledger.path.read_bytes());calls=len(f.google.calls)
        source,path,out,packet=self.plan()
        self.assertEqual(packet['group_kind'],'heartbeat-admitted')
        self.assertEqual([e['kind'] for e in packet['expected_state']['events'][-2:]],['heartbeat','admitted'])
        self.assertEqual(packet['expected_state']['epoch'],source.state['epoch']+2)
        self.assertEqual(len(json.loads(f.ledger.path.read_bytes())['operations']),len(before['operations'])+1)
        self.assertEqual(len(out['tool_arguments']['requests']),1)
        verified=f.commit(path,out);self.assertTrue(verified['verified']);self.assertFalse(verified['first_heartbeat_required'])
        self.assertEqual(len(f.google.calls),calls+1)
        self.assertTrue(self.import_admission()['imported'])
        self.assertTrue(self.import_admission()['reconciled_existing'])
        for kind in ('admitted','heartbeat-admitted'):
            self.assertError('already_issued',f.ledger.plan_event,f.source(),kind,f.path('repeat'),route_id=self.rid)
        self.assertEqual(len(json.loads(f.ledger.path.read_bytes())['spawns']),1)

    def test_native_result_is_required_before_any_heartbeat_or_group_reservation(self):
        self.start(record=False);f=self.f;source=f.source();before=json.loads(f.ledger.path.read_bytes())['operations']
        self.assertError('recorded_native_result',self.plan)
        self.assertEqual(json.loads(f.ledger.path.read_bytes())['operations'],before)
        self.assertEqual(f.source().state,source.state)
        self.assertEqual(json.loads(f.ledger.path.read_bytes())['spawns'][self.rid]['status'],'reserved_outcome_unknown')

    def test_exact_saved_receipt_and_plan_binding_required_before_group(self):
        self.start();f=self.f;before=json.loads(f.ledger.path.read_bytes())['operations']
        receipt=json.loads(self.receipt.read_bytes());receipt['native_task_id']='/root/other'
        private_write(self.receipt,canonical(receipt))
        self.assertError('actual_admission_mismatch',self.plan)
        self.assertEqual(json.loads(f.ledger.path.read_bytes())['operations'],before)

    def test_legacy_admitted_excludes_group_and_still_imports(self):
        self.start();f=self.f;f.event('admitted',route_id=self.rid)
        self.assertError('already_issued',self.plan)
        self.assertTrue(self.import_admission()['imported'])

    def test_unknown_partial_heartbeat_burns_pair_on_restart_and_prevents_import(self):
        self.start();f=self.f;source,path,_,packet=self.plan();event=packet['expected_state']['events'][-2]
        partial=q.transition(source.state,f.code,event['kind'],event['actor'],event['arguments'],
                             operation_id=event['operation_id'],now=event['at'])
        self.assertError('not_observed',f.ledger.verify_plan,path,None,self.document(partial))
        restarted=native.NativeLedger(f.ledger.root,source.state,f.code,f.ledger.identity)
        for kind in ('admitted','heartbeat-admitted','heartbeat'):
            self.assertError('unresolved',restarted.plan_event,source,kind,f.path('retry'),route_id=self.rid)
        self.assertEqual(json.loads(f.ledger.path.read_bytes())['operations']['heartbeat-admitted:'+self.rid]['status'],
                         'outcome_unknown_no_replay')
        self.assertError('unresolved',self.import_admission)

    def test_full_prefix_plus_later_heartbeat_reconciles_response_loss(self):
        self.start();f=self.f;_,path,_,packet=self.plan();f.clock+=1
        c=packet['expected_state']['logical']['controller']
        later=q.transition(packet['expected_state'],f.code,'heartbeat','native',
            {'native_task_id':c['native_task_id'],'controller_epoch':c['controller_epoch']})
        before=len(f.google.calls)
        self.assertTrue(f.ledger.verify_plan(path,None,self.document(later))['reconciled_from_event'])
        self.assertEqual(len(f.google.calls),before)
        imported=f.ledger.import_child_admission(q.snapshot(self.document(later),f.initial['document_id'],'t.0'),
                                                 self.rid,self.receipt,self.task)
        self.assertTrue(imported['imported'])

    def test_group_schema_deadline_identity_and_order_are_exact(self):
        self.start();f=self.f;source,_,_,packet=self.plan()
        self.assertError('not_one_transition',q.plan,source,packet['expected_state'],f.code)
        for change in ({'group_kind':'claim-begin'},{'group_kind':'anything'},
                       {'operation_ids':packet['operation_ids'][::-1]}, {'execute_before':packet['execute_before']+1},
                       {'group_id':'0'*64},{'operation_id':packet['operation_ids'][0]}):
            with self.subTest(change=change):self.assertError('group|cas',q.validate_plan,{**packet,**change},f.code)
        hb,admitted=packet['expected_state']['events'][-2:]
        middle=q.transition(source.state,f.code,'heartbeat','native',hb['arguments'],now=hb['at'])
        changed=q.transition(middle,f.code,'admitted','native',admitted['arguments'],now=admitted['at']+1)
        self.assertError('group_not_heartbeat_admitted',q.plan_heartbeat_admitted,source,changed,f.code)

    def test_import_rejects_tampered_group_record_or_two_admitted_reservations(self):
        self.start();f=self.f;_,path,out,_=self.plan();f.commit(path,out)
        saved=json.loads(f.ledger.path.read_bytes());good=copy.deepcopy(saved)
        saved['operations']['heartbeat-admitted:'+self.rid]['operation_ids'].reverse();private_write(f.ledger.path,canonical(saved))
        self.assertError('verified_admitted_binding',self.import_admission)
        good['operations']['admitted:'+self.rid]=copy.deepcopy(good['operations']['heartbeat-admitted:'+self.rid])
        private_write(f.ledger.path,canonical(good));self.assertError('verified_global_admitted_required',self.import_admission)

    def test_original_pre_refresh_deadline_cannot_extend_through_group(self):
        self.start();f=self.f;c=f.source().state['logical']['controller'];f.clock=c['heartbeat_at']+899
        # Existing child lifetime may be shorter; pure construction still verifies
        # the old heartbeat fence even with synthetic valid native admission data.
        state=f.source().state;args={'native_task_id':f.ledger.identity,'controller_epoch':c['controller_epoch']}
        tick=q.transition(state,f.code,'heartbeat','native',args)
        d=state['logical']['demands'][self.rid];rec=json.loads(f.ledger.path.read_bytes())['spawns'][self.rid]
        new=q.transition(tick,f.code,'admitted','native',{**args,'route_id':self.rid,'claim_id':d['claim_id'],
            'admission':rec['receipt'],'spawn_arguments_sha256':rec['arguments_sha256']})
        packet=q.plan_heartbeat_admitted(f.source(),new,f.code)
        self.assertLessEqual(packet['execute_before'],c['heartbeat_at']+900)
        f.clock=packet['execute_before']
        self.assertError('acceptance_window',q.verify_update,packet,None,self.document(new),f.code)

    def test_dead_controller_never_gains_post_result_heartbeat(self):
        self.start();f=self.f;c=f.source().state['logical']['controller'];f.clock=c['heartbeat_at']+900
        self.assertError('not_active',self.plan)
        self.assertNotIn('heartbeat-admitted:'+self.rid,json.loads(f.ledger.path.read_bytes())['operations'])

    def test_failed_group_output_stays_reserved(self):
        self.start();f=self.f;destination=f.path('failed');original=native.private_write
        def fail(path,raw):
            if path==destination:raise OSError('output failure')
            return original(path,raw)
        with patch.object(native,'private_write',side_effect=fail):
            with self.assertRaises(OSError):f.ledger.plan_event(f.source(),'heartbeat-admitted',destination,route_id=self.rid)
        self.assertFalse(destination.exists());self.assertError('unresolved',self.plan)

    def test_group_cli_check_and_verify_import_fold(self):
        self.start();f=self.f;path=f.path('group');out=self.run_cli('plan-heartbeat-admitted',
            '--route-id',self.rid,'--save',path,'--check-cas-now')
        self.assertTrue(out['dispatch_check']['dispatch_allowed'])
        response=f.google.batch_update_document(**out['tool_arguments']);rp=f.path('response');back=f.path('back')
        private_write(rp,canonical(response));private_write(back,canonical(f.google.get_document(f.initial['document_id'])))
        result=self.run_cli('verify','--plan-file',path,'--response',rp,'--readback',back,
            '--import-child-admission','--route-id',self.rid,'--admission-receipt',self.receipt,'--child-native-task-id',self.task)
        self.assertTrue(result['verified']);self.assertTrue(result['child_import']['imported'])

    def test_invalid_inline_flags_do_not_change_ledger(self):
        self.start();f=self.f;before=f.ledger.path.read_bytes()
        for args,error in [(('verify','--import-child-admission'),'global_inline_import_requires_verified_admission'),
            (('plan-heartbeat-admitted','--check-native-now'),'global_inline_native_check_requires_native_plan')]:
            self.assertEqual(self.run_cli(*args,success=False)['error'],error)
            self.assertEqual(f.ledger.path.read_bytes(),before)

    def test_plan_native_inline_check_preserves_original_ten_second_window(self):
        f=self.f;f.join();rid=f.demand();f.event('claim-begin',route_id=rid);path=f.path('inline-native')
        out=self.run_cli('plan-native','--route-id',rid,'--save',path,'--package-root',f.package,'--check-native-now')
        self.assertEqual(out['execute_before'],f.clock+10)
        self.assertEqual(out['dispatch_check']['checked_at'],f.clock)
        self.assertEqual(out['dispatch_check']['execute_before'],out['execute_before'])
        f.clock+=10;self.assertError('dispatch_window',f.ledger.check_spawn,f.source(),path)
        self.assertError('already_reserved',f.ledger.plan_spawn,f.source(),rid,f.path('again'),f.package)

    def join_plan(self,seconds=1200):
        f=self.f
        def sleep(n):f.clock+=n
        with patch.object(native.time,'sleep',side_effect=sleep):
            return self.plan('join-heartbeat',capacity=2,seconds=seconds)

    def test_join_group_samples_two_real_times_in_one_cas_and_initializes(self):
        f=self.f;start=f.clock;source,path,out,packet=self.join_plan()
        self.assertEqual(packet['group_kind'],'join-heartbeat')
        events=packet['expected_state']['events'];self.assertEqual([e['kind'] for e in events],['join','heartbeat'])
        self.assertEqual([e['at'] for e in events],[start,start+1])
        self.assertEqual(packet['execute_before'],start+120)
        self.assertEqual(len(json.loads(f.ledger.path.read_bytes())['operations']),1)
        self.assertFalse(f.commit(path,out)['first_heartbeat_required'])
        f.bridge.sync_heartbeat();self.assertTrue(f.store.status()['controller_active'])
        self.assertError('already_issued',f.ledger.plan_event,f.source(),'join',f.path('again'),capacity=2,seconds=1200)
        self.assertError('already_issued',self.join_plan)
        # This only consumed the first heartbeat; genuine later ticks remain legal.
        f.clock+=25;f.event('heartbeat')

    def test_join_wait_never_fakes_future_time_or_accepts_delayed_or_rollback_clock(self):
        f=self.f;start=f.clock
        for delta in (0,-1,3):
            def sleep(n):f.clock+=delta
            f.clock=start
            with patch.object(native.time,'sleep',side_effect=sleep):
                self.assertError('clock_changed',self.plan,'join-heartbeat',capacity=2,seconds=1200)
            self.assertEqual(json.loads(f.ledger.path.read_bytes())['operations'],{})
        self.assertError('requires_host_clock',f.ledger.plan_event,f.source(),'join-heartbeat',f.path('fabricated'),
                         capacity=2,seconds=1200,now=int(start))

    def test_partial_join_burns_entire_pair_across_restart(self):
        f=self.f;source,path,_,packet=self.join_plan();event=packet['expected_state']['events'][0]
        partial=q.transition(source.state,f.code,'join','native',event['arguments'],
                             operation_id=event['operation_id'],now=event['at'])
        self.assertError('not_observed',f.ledger.verify_plan,path,None,self.document(partial))
        ledger=native.NativeLedger(f.ledger.root,source.state,f.code,f.ledger.identity)
        for kind in ('join','join-heartbeat','heartbeat'):
            self.assertError('unresolved',ledger.plan_event,source,kind,f.path('retry'),capacity=2,seconds=1200)
        self.assertEqual(json.loads(ledger.path.read_bytes())['operations']['join-heartbeat:controller']['status'],
                         'outcome_unknown_no_replay')

    def test_join_group_legacy_join_exclusion_and_lease_deadline(self):
        f=self.f;_,path,out,packet=self.join_plan(seconds=2)
        self.assertEqual(packet['execute_before'],f.initial['created']+2)
        f.clock=packet['execute_before'];self.assertError('dispatch_window',f.ledger.check_plan,f.source(),path)
        response=f.google.batch_update_document(**out['tool_arguments'])
        self.assertError('acceptance_window',f.ledger.verify_plan,path,response,f.google.get_document(f.initial['document_id']))

    def test_legacy_join_blocks_group(self):
        f=self.f;f.event('join',capacity=2,seconds=1200)
        self.assertError('already_issued',self.join_plan)

    def test_join_same_time_invalid_and_arbitrary_time_gap_cannot_be_grouped(self):
        f=self.f;source,_,_,packet=self.join_plan();join,hb=packet['expected_state']['events']
        joined=q.transition(source.state,f.code,'join','native',join['arguments'],now=join['at'])
        self.assertError('too_frequent',q.transition,joined,f.code,'heartbeat','native',hb['arguments'],now=join['at'])
        later=q.transition(joined,f.code,'heartbeat','native',hb['arguments'],now=join['at']+2)
        self.assertError('group_not_join_heartbeat',q.plan_join_heartbeat,source,later,f.code)
        for change in ({'group_kind':'heartbeat-admitted'},{'operation_ids':packet['operation_ids'][::-1]},
                       {'execute_before':packet['execute_before']+1}):
            self.assertError('group|cas',q.validate_plan,{**packet,**change},f.code)

    def test_real_join_cli_check_preserves_sampled_next_second(self):
        f=self.f;path=f.path('cli-join')
        out=self.run_cli('plan-join-heartbeat','--save',path,'--capacity',2,'--seconds',1200,'--check-cas-now')
        self.assertTrue(out['dispatch_check']['dispatch_allowed'])
        packet=json.loads(path.read_bytes());self.assertEqual(packet['expected_state']['events'][1]['at'],f.clock+1)
        f.clock+=1.001;self.assertTrue(f.commit(path,out)['verified'])

    def test_inline_plan_captures_full_exact_resource_and_preserves_original_check(self):
        self.start();f=self.f;document=f.google.get_document(f.initial['document_id']);path=f.path('inline')
        envelope={'snapshot':document,'response':None}
        out=self.run_cli('plan-heartbeat-admitted','--route-id',self.rid,'--save',path,'--check-cas-now',inline=envelope)
        self.assertEqual(json.loads(Path(out['snapshot_file']).read_bytes()),document)
        self.assertIsNone(out['response_file']);self.assertTrue(out['dispatch_check']['dispatch_allowed'])
        captured=[json.loads(p.read_bytes()) for p in f.ledger.root.glob('capture-*.json')]
        self.assertIn(envelope,captured)
        response=f.google.batch_update_document(**out['tool_arguments']);back=f.google.get_document(f.initial['document_id'])
        verified=self.run_cli('verify','--plan-file',path,'--import-child-admission','--route-id',self.rid,
            '--admission-receipt',self.receipt,'--child-native-task-id',self.task,
            inline={'snapshot':back,'response':response})
        self.assertTrue(verified['verified']);self.assertTrue(verified['child_import']['imported'])
        self.assertEqual(json.loads(Path(verified['snapshot_file']).read_bytes()),back)
        self.assertEqual(json.loads(Path(verified['response_file']).read_bytes()),response)

    def test_invalid_inline_envelopes_and_ops_do_not_reserve(self):
        self.start();f=self.f;before=f.ledger.path.read_bytes();doc=f.google.get_document(f.initial['document_id'])
        cases=[({'snapshot':doc},'global_invalid_inline_evidence'),
               ({'snapshot':doc,'response':None,'extra':1},'global_invalid_inline_evidence'),
               ({'snapshot':doc,'response':{}},'global_invalid_inline_evidence')]
        for envelope,error in cases:
            out=self.run_cli('plan-heartbeat-admitted','--route-id',self.rid,'--save',f.path('bad'),inline=envelope,success=False)
            self.assertEqual(out['error'],error);self.assertEqual(f.ledger.path.read_bytes(),before)
        out=self.run_cli('record-native',inline={'snapshot':doc,'response':None},success=False)
        self.assertEqual(out['error'],'global_invalid_inline_evidence_operation')
        out=self.run_cli('plan-heartbeat-admitted',raw=' '*60001,success=False)
        self.assertEqual(out['error'],'global_inline_evidence_size_exceeded')
        self.assertEqual(f.ledger.path.read_bytes(),before)

    def test_same_second_admission_waits_for_actual_host_tick(self):
        self.start();f=self.f;f.clock-=1;before=f.clock
        def sleep(n):f.clock+=n
        with patch.object(native.time,'sleep',side_effect=sleep):_,path,out,packet=self.plan()
        self.assertEqual(packet['expected_state']['events'][-1]['at'],before+1)
        self.assertTrue(f.commit(path,out)['verified'])

    def test_explicit_same_second_is_rejected_without_wait_or_reservation(self):
        self.start();f=self.f;f.clock-=1
        with patch.object(native.time,'sleep') as sleeper:
            self.assertError('too_frequent',self.plan,now=int(f.clock))
        sleeper.assert_not_called()
        self.assertNotIn('heartbeat-admitted:'+self.rid,json.loads(f.ledger.path.read_bytes())['operations'])

    def test_prenative_emitter_has_complete_bounded_output_and_route_validation(self):
        f=self.f;f.join();rid=f.demand();code=f.path('code');private_write(code,f.code.encode())
        for operation,call in [('pre-native','prepareNative'),('claim-prepare-native','claimAndPrepareNative')]:
            path=f.path('cell');fixed=f.path('fixed-native-plan')
            native.emit_cell(f.ledger,f.source(),path,f.package,code,operation,2,1200,rid,plan_file=fixed)
            text=path.read_text();metadata=json.loads(text.splitlines()[0].removeprefix('// @exec: '))
            self.assertEqual(metadata,{'yield_time_ms':120000,'max_output_tokens':4000})
            self.assertIn('activeController.cell.'+call+'(',text)
            self.assertIn('"nativePlanFile": '+json.dumps(str(fixed)),text)
            self.assertFalse(fixed.exists())

    def test_verified_cas_with_failed_inline_import_never_reopens_group(self):
        self.start();f=self.f;_,path,out,_=self.plan()
        response=f.google.batch_update_document(**out['tool_arguments']);back=f.google.get_document(f.initial['document_id'])
        failed=self.run_cli('verify','--plan-file',path,'--import-child-admission','--route-id',self.rid,
            '--admission-receipt',self.receipt,'--child-native-task-id','/root/wrong',
            inline={'snapshot':back,'response':response},success=False)
        self.assertEqual(failed['error'],'global_child_actual_admission_mismatch')
        saved=json.loads(f.ledger.path.read_bytes())
        self.assertEqual(saved['operations']['heartbeat-admitted:'+self.rid]['status'],'verified')
        self.assertNotIn('child_admission_import',saved['spawns'][self.rid])
        self.assertError('already_issued',self.plan)
        self.assertError('plan_not_reserved',f.ledger.check_plan,f.source(),path)
        self.assertTrue(self.import_admission()['imported'])

    def test_wrong_plan_for_inline_import_is_rejected_before_verification(self):
        self.start();f=self.f;path,out=f.plan('heartbeat');response=f.google.batch_update_document(**out['tool_arguments'])
        back=f.google.get_document(f.initial['document_id']);before=f.ledger.path.read_bytes()
        failed=self.run_cli('verify','--plan-file',path,'--import-child-admission','--route-id',self.rid,
            '--admission-receipt',self.receipt,'--child-native-task-id',self.task,
            inline={'snapshot':back,'response':response},success=False)
        self.assertEqual(failed['error'],'global_inline_import_requires_verified_admission')
        self.assertEqual(f.ledger.path.read_bytes(),before)

    def test_claim_startup_chooses_existing_two_event_group_before_due(self):
        f=self.f;f.join();self.rid=f.demand();_,path,out,packet=self.plan('claim-startup')
        self.assertEqual(packet['group_kind'],'claim-begin');self.assertEqual(len(packet['operation_ids']),2)
        self.assertTrue(f.commit(path,out)['verified'])
        saved=json.loads(f.ledger.path.read_bytes())
        self.assertIn('claim-begin:'+self.rid,saved['operations'])
        self.assertNotIn('heartbeat-claim-begin:'+self.rid,saved['operations'])

    def test_due_claim_startup_one_cas_three_events_then_real_native_and_import(self):
        self.start(claim_kind='claim-startup',claim_delay=25);f=self.f
        saved=json.loads(f.ledger.path.read_bytes());record=saved['operations']['heartbeat-claim-begin:'+self.rid]
        packet=json.loads(Path(record['path']).read_bytes())
        self.assertEqual(packet['group_kind'],'heartbeat-claim-begin')
        self.assertEqual([e['kind'] for e in packet['expected_state']['events'][-3:]],['heartbeat','claim','begin'])
        self.assertEqual(record['operation_ids'],[e['operation_id'] for e in packet['expected_state']['events'][-3:]])
        self.assertEqual(record['status'],'verified')
        for kind in ('claim','begin','claim-begin','claim-startup'):
            self.assertError('already_issued',f.ledger.plan_event,f.source(),kind,f.path('retry'),route_id=self.rid)
        _,path,out,_=self.plan();f.commit(path,out)
        self.assertTrue(self.import_admission()['imported'])

    def test_due_claim_partial_heartbeat_claim_burns_complete_group(self):
        f=self.f;f.join();self.rid=f.demand();f.clock+=25;source,path,_,packet=self.plan('claim-startup')
        partial=source.state
        for event in packet['expected_state']['events'][-3:-1]:
            partial=q.transition(partial,f.code,event['kind'],event['actor'],event['arguments'],
                operation_id=event['operation_id'],now=event['at'])
        self.assertError('not_observed',f.ledger.verify_plan,path,None,self.document(partial))
        ledger=native.NativeLedger(f.ledger.root,source.state,f.code,f.ledger.identity)
        for kind in ('claim','begin','claim-begin','claim-startup','heartbeat'):
            self.assertError('unresolved',ledger.plan_event,source,kind,f.path('retry'),route_id=self.rid)
        self.assertError('unresolved',ledger.plan_spawn,source,self.rid,f.path('spawn'),f.package)
        self.assertEqual(json.loads(ledger.path.read_bytes())['spawns'],{})

    def test_due_claim_cannot_extend_pre_refresh_deadline(self):
        f=self.f;f.join();self.rid=f.demand();c=f.source().state['logical']['controller'];f.clock=c['heartbeat_at']+899
        _,path,out,packet=self.plan('claim-startup');self.assertEqual(packet['execute_before'],c['heartbeat_at']+900)
        f.clock=packet['execute_before'];self.assertError('not_active|dispatch_window',f.ledger.check_plan,f.source(),path)
        response=f.google.batch_update_document(**out['tool_arguments'])
        self.assertError('acceptance_window',f.ledger.verify_plan,path,response,f.google.get_document(f.initial['document_id']))

    def test_due_claim_schema_rejects_reorder_missing_component_and_other_kind(self):
        f=self.f;f.join();self.rid=f.demand();f.clock+=25;source,_,_,packet=self.plan('claim-startup')
        for change in ({'operation_ids':packet['operation_ids'][::-1]},{'operation_ids':packet['operation_ids'][:2]},
                       {'group_kind':'claim-begin'},{'execute_before':packet['execute_before']+1}):
            self.assertError('group|cas',q.validate_plan,{**packet,**change},f.code)
        self.assertError('not_one_transition',q.plan,source,packet['expected_state'],f.code)

    def test_real_claim_startup_cli_with_large_result_chunk(self):
        from remote_transport.connector_files import packet_chunk
        f=self.f;f.join();rid=f.demand();f.clock+=25;path=f.path('startup');result=f.path('large-result')
        first=self.run_cli('plan-claim-startup','--route-id',rid,'--save',path,'--check-cas-now',
            '--result-file',result,'--result-first-chunk','--result-large-chunk')
        self.assertEqual(first,packet_chunk(result,max_chars=65536,large_output=True))
        self.assertEqual(json.loads(path.read_bytes())['group_kind'],'heartbeat-claim-begin')
        self.assertTrue(json.loads(result.read_bytes())['dispatch_check']['dispatch_allowed'])

    def test_large_packet_chunks_bound_unicode_bytes_and_allow_legacy_read_only_fallback(self):
        from remote_transport.connector_files import packet_chunk
        f=self.f;path=f.path('unicode');raw=canonical({'text':'雪😀"\n'*20000},max_bytes=8*1024*1024)
        private_write(path,raw)
        first=packet_chunk(path,max_chars=65536,large_output=True)
        self.assertLessEqual(len(json.dumps(first,ensure_ascii=False).encode()),64000)
        self.assertGreater(first['next_offset_chars'],packet_chunk(path)['next_offset_chars'])
        parts=[first['text']];offset=first['next_offset_chars']
        while offset<first['total_chars']:
            part=packet_chunk(path,first['sha256'],offset)
            self.assertLessEqual(len(json.dumps(part,ensure_ascii=False).encode()),16000)
            parts.append(part['text']);offset=part['next_offset_chars']
        self.assertEqual(''.join(parts),raw.decode())
        self.assertError('invalid_packet_chunk_range',packet_chunk,path,max_chars=65536)
        self.assertError('hash_mismatch',packet_chunk,path,'0'*64,0,65536,large_output=True)
        result=subprocess.run([sys.executable,'-B','-m','remote_transport.connector_files','packet-chunk',
            '--path',str(path),'--large-output','--max-chars','65536'],cwd=f.package,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr);self.assertLessEqual(len(result.stdout.encode()),64001)
        self.assertEqual(json.loads(result.stdout),first)


if __name__=='__main__':unittest.main()
