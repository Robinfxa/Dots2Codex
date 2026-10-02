"""Offline proof: repeating one frozen RESULT CAS can apply at most once.

No test issues native inference, uploads a file, or calls Google. Permission
review at the adapter boundary is a separate requirement, not attested here.
"""
import copy
from pathlib import Path
import unittest
from unittest import mock

from dots_lite import docs, protocol as p
from dots_lite.storage import private_read, private_write
from dots_lite.worker import Worker
from lite_tests import test_core as fixtures

KEY=fixtures.KEY
ack=fixtures.ack
resource=fixtures.resource


class AtomicDocs:
    """Required-revision semantics only, not a claim of live Google proof."""
    def __init__(self, plan):
        self.text=plan['source']['text'];self.revision=plan['source']['revision_id']
        self.requests=[];self.commits=0

    def dispatch(self, plan, *, drop_reply=False, fail_before_commit=False):
        self.requests.append(p.canonical(plan['body']))
        if fail_before_commit:return None
        if plan['body']['writeControl']['requiredRevisionId']!=self.revision:
            return {'isError':True,'error':{'code':'stale_revision'}}
        self.text=plan['text'];self.commits+=1;self.revision='result-revision-'+str(self.commits)
        return None if drop_reply else ack(plan,self.revision)

    def read(self):return resource(self.text,self.revision)


class ResultCASRetry(unittest.TestCase):
    setUp=fixtures.Core.setUp
    parent=fixtures.Core.parent
    worker=fixtures.Core.worker
    request=fixtures.Core.request
    output=fixtures.Core.output

    def ready(self, publish=True):
        worker=self.worker();inbox,path,desc=self.request(worker)
        begin=worker.prepare_begin(inbox,path);worker.accept_begin_and_expose(ack(begin,'rev3'))
        artifact=worker.save_actual_result(desc['request_id'],self.output());worker.record_upload_attempt()
        receipt={'file_id':'result-file','folder_id':'folder','byte_length':artifact['byte_length']}
        return worker,worker.publish_result(receipt) if publish else receipt

    def restart(self):return Worker(self.base/'route',KEY,'/root/child_route',self.clock)

    def test_first_committed_lost_reply_repeated_cas_cannot_apply_twice(self):
        worker,plan=self.ready();provider=AtomicDocs(plan)
        self.assertEqual(worker.accept_result(provider.dispatch(plan,drop_reply=True))['status'],'unknown')
        worker=self.restart();retry=worker.retry_result(plan['operation_id'],2)
        self.assertEqual(worker.accept_result(provider.dispatch(retry))['status'],'unknown')
        accepted=worker.accept_result(readback=provider.read())
        self.assertEqual(accepted['status'],'applied');self.assertEqual(accepted['revision_id'],provider.revision)
        self.assertEqual(provider.commits,1);self.assertEqual(provider.requests,[p.canonical(plan['body'])]*2)
        self.assertEqual(worker.state['current']['result_publication']['attempts'],2)
        self.assertEqual(worker.state['current']['upload_attempts'],1)

    def test_first_did_not_commit_retry_uses_exact_original_requests_revision_operation(self):
        worker,plan=self.ready();provider=AtomicDocs(plan)
        worker.accept_result(provider.dispatch(plan,fail_before_commit=True))
        retry=self.restart().retry_result(plan['operation_id'],2)
        self.assertEqual(p.canonical(retry),p.canonical(plan))
        self.assertEqual(worker.accept_result(provider.dispatch(retry))['status'],'accepted')
        self.assertEqual(provider.commits,1)
        self.assertEqual(provider.requests,[p.canonical(plan['body'])]*2)
        self.assertEqual(worker.state['outbox']['revision_id'],provider.revision)

    def test_restart_before_first_dispatch_burns_attempt_not_inference(self):
        worker,plan=self.ready();provider=AtomicDocs(plan)
        self.assertEqual(self.restart().result_retry_status()['attempts_used'],1)
        retry=self.restart().retry_result(plan['operation_id'],2)
        self.assertEqual(worker.accept_result(provider.dispatch(retry))['status'],'accepted')
        self.assertEqual(provider.commits,1)
        self.assertEqual(len(list((self.base/'route').glob('exposure-*.once'))),1)
        self.assertEqual(len(list((self.base/'route').glob('spawn.once'))),1)

    def test_crash_after_seal_before_initial_marker_resumes_only_saved_plan(self):
        worker,receipt=self.ready(publish=False)
        with mock.patch('dots_lite.worker.burn_fence',side_effect=RuntimeError('crash')):
            with self.assertRaisesRegex(RuntimeError,'crash'):worker.publish_result(receipt)
        resumed=self.restart();status=resumed.result_retry_status()
        self.assertEqual(status['attempts_used'],0);self.assertEqual(status['next_attempt'],1)
        original=resumed.state['pending_plan'];retry=resumed.retry_result(status['operation_id'],1)
        self.assertEqual(p.canonical(retry),p.canonical(original))

    def test_crash_after_initial_marker_before_counter_commit_consumes_attempt(self):
        worker,receipt=self.ready(publish=False);write=worker._write;calls=[]
        def crash_second_write(state):
            calls.append(state['phase'])
            if len(calls)==2:raise RuntimeError('crash')
            return write(state)
        with mock.patch.object(worker,'_write',side_effect=crash_second_write):
            with self.assertRaisesRegex(RuntimeError,'crash'):worker.publish_result(receipt)
        resumed=self.restart();status=resumed.result_retry_status()
        self.assertEqual(resumed.state['current']['result_publication']['attempts'],0)
        self.assertEqual(status['attempts_used'],1)
        retry=resumed.retry_result(status['operation_id'],2)
        self.assertEqual(retry['body']['writeControl']['requiredRevisionId'],'rev3')

    def test_repeated_stale_remains_unknown_and_exhausts_three_attempts(self):
        worker,plan=self.ready();provider=AtomicDocs(plan);provider.revision='unrelated-revision'
        for attempt in (1,2,3):
            packet=plan if attempt==1 else self.restart().retry_result(plan['operation_id'],attempt)
            self.assertEqual(worker.accept_result(provider.dispatch(packet))['status'],'unknown')
            result=worker.accept_result(readback=provider.read())
            self.assertEqual(result['status'],'unknown');self.assertFalse(result['quarantined'])
        self.assertEqual(provider.commits,0);self.assertEqual(provider.requests,[p.canonical(plan['body'])]*3)
        with self.assertRaisesRegex(p.ProtocolError,'budget_exhausted'):worker.retry_result(plan['operation_id'],4)
        self.assertIsNone(worker.result_retry_status()['next_attempt'])
        self.assertEqual(worker.state['phase'],'RESULT_PREPARED')

    def test_old_attempt_review_cannot_be_reused_for_next_attempt(self):
        worker,plan=self.ready();worker.retry_result(plan['operation_id'],2)
        with self.assertRaisesRegex(p.ProtocolError,'attempt_mismatch'):worker.retry_result(plan['operation_id'],2)
        for invalid in (True,'3',None):
            with self.assertRaises(p.ProtocolError):worker.retry_result(plan['operation_id'],invalid)
        self.assertEqual(worker.result_retry_status()['attempts_used'],2)

    def test_wrong_operation_no_attempt_consumed(self):
        worker,plan=self.ready()
        with self.assertRaisesRegex(p.ProtocolError,'operation_mismatch'):worker.retry_result('different-operation',2)
        self.assertEqual(worker.result_retry_status()['attempts_used'],1)

    def test_journal_rollback_cannot_refund_retry_budget(self):
        worker,plan=self.ready();journal=self.base/'route'/'journal.json';before=private_read(journal)
        worker.retry_result(plan['operation_id'],2);private_write(journal,before)
        self.assertEqual(self.restart().result_retry_status()['attempts_used'],2)
        with self.assertRaisesRegex(p.ProtocolError,'attempt_mismatch'):worker.retry_result(plan['operation_id'],2)
        worker.retry_result(plan['operation_id'],3);private_write(journal,before)
        with self.assertRaisesRegex(p.ProtocolError,'budget_exhausted'):self.restart().retry_result(plan['operation_id'],4)

    def test_newer_authenticated_request_quarantines_without_overwrite(self):
        worker,plan=self.ready();before=worker.state['outbox'];newer=copy.deepcopy(plan['record'])
        newer.update(phase='BEGIN',operation_id='new-begin',begin_operation_id='new-begin',consumed_seq=2,result=None)
        newer['request'].update(seq=2,request_id='request-2',previous_result_ack={k:plan['record']['result'][k] for k in ('result_id','result_sha256')})
        newer=p.sign_record(newer,KEY)
        result=worker.accept_result(readback=resource(p.canonical(newer).decode()+'\n','rev-new'))
        self.assertEqual(result['status'],'unknown');self.assertTrue(result['quarantined'])
        with self.assertRaisesRegex(p.ProtocolError,'quarantined'):worker.retry_result(plan['operation_id'],2)
        self.assertEqual(worker.state['outbox'],before)
        self.assertEqual(worker.accept_result(ack(plan,'late-reply'))['reason'],'result_publication_quarantined')
        self.assertEqual(worker.state['phase'],'RESULT_PREPARED')

    def test_conflicting_authenticated_result_quarantines_even_same_sequence(self):
        worker,plan=self.ready();other=copy.deepcopy(plan['record']);other['operation_id']='other-result'
        other['result']['file_id']='other-file';other=p.sign_record(other,KEY)
        result=worker.accept_result(readback=resource(p.canonical(other).decode()+'\n','rev-other'))
        self.assertTrue(result['quarantined'])
        with self.assertRaisesRegex(p.ProtocolError,'quarantined'):self.restart().retry_result(plan['operation_id'],2)

    def test_unverified_readback_conflict_is_unknown_not_success(self):
        worker,plan=self.ready()
        result=worker.accept_result(readback=resource('untrusted text\n','new-revision'))
        self.assertEqual(result['status'],'unknown');self.assertFalse(result['quarantined'])
        self.assertEqual(worker.state['record']['phase'],'BEGIN')
        self.assertEqual(worker.retry_result(plan['operation_id'],2),plan)

    def test_quarantine_survives_journal_only_rollback(self):
        worker,plan=self.ready();journal=self.base/'route'/'journal.json';before=private_read(journal)
        other=copy.deepcopy(plan['record']);other['operation_id']='other-result';other=p.sign_record(other,KEY)
        worker.accept_result(readback=resource(p.canonical(other).decode()+'\n','new'))
        private_write(journal,before)
        with self.assertRaisesRegex(p.ProtocolError,'quarantined'):self.restart().retry_result(plan['operation_id'],2)

    def test_no_retry_upload_exposure_or_result_regeneration_path(self):
        worker,plan=self.ready()
        with mock.patch.object(worker,'record_upload_attempt',side_effect=AssertionError('upload')), \
             mock.patch.object(worker,'accept_begin_and_expose',side_effect=AssertionError('expose')), \
             mock.patch.object(worker,'save_actual_result',side_effect=AssertionError('inference')), \
             mock.patch.object(worker,'publish_result',side_effect=AssertionError('new plan')), \
             mock.patch('dots_lite.worker.plan_write',side_effect=AssertionError('replan')):
            retry=worker.retry_result(plan['operation_id'],2)
        self.assertEqual(retry,plan)
        self.assertEqual(worker.state['current']['upload_attempts'],1)

    def test_other_phases_never_gain_result_retry(self):
        worker=self.worker()
        with self.assertRaisesRegex(p.ProtocolError,'result_not_pending'):worker.retry_result('op',2)
        inbox,path,_=self.request(worker);plan=worker.prepare_begin(inbox,path)
        with self.assertRaisesRegex(p.ProtocolError,'result_not_pending'):worker.retry_result(plan['operation_id'],2)
        self.assertEqual(worker.state['current']['exposure'],'INPUT_NOT_EXPOSED')

    def test_legacy_result_plan_is_not_retroactively_sealed(self):
        worker,plan=self.ready();state=worker.state
        seal=worker._result_seal_path(plan['operation_id']);seal.unlink()
        del state['current']['result_publication'];worker.journal.write(state)
        with self.assertRaisesRegex(p.ProtocolError,'seal_required_new_release'):self.restart().retry_result(plan['operation_id'],2)
        # Existing exact reconciliation still works, without granting a retry.
        self.assertEqual(worker.accept_result(readback=resource(plan['text'],'real-revision'))['status'],'applied')

    def test_rewritten_journal_plan_or_original_revision_cannot_pass_seal(self):
        worker,plan=self.ready();state=worker.state
        changed=copy.deepcopy(plan['source']);changed['revision_id']='fresh-but-forbidden'
        state['pending_plan']=docs.plan_write(changed,plan['record'],plan['operation_id']);worker.journal.write(state)
        with self.assertRaisesRegex(p.ProtocolError,'plan_changed'):self.restart().retry_result(plan['operation_id'],2)
        with self.assertRaisesRegex(p.ProtocolError,'plan_changed'):worker.accept_result(ack(plan,'reply'))

    def test_modified_seal_and_rehashed_journal_still_need_original_hmac(self):
        worker,plan=self.ready();state=worker.state;path=worker._result_seal_path(plan['operation_id'])
        seal=p.strict_json(private_read(path));seal['binding']['plan']['source']['revision_id']='forged'
        raw=p.canonical(seal);private_write(path,raw);state['current']['result_publication']['seal_sha256']=p.sha256(raw)
        worker.journal.write(state)
        with self.assertRaisesRegex(p.ProtocolError,'seal_authentication_failed'):worker.retry_result(plan['operation_id'],2)

    def test_payload_changed_after_upload_blocks_retry(self):
        worker,plan=self.ready();private_write(worker.state['current']['artifact']['path'],b'{}')
        with self.assertRaisesRegex(p.ProtocolError,'immutable_result_changed'):worker.retry_result(plan['operation_id'],2)

    def test_missing_or_partial_attempt_marker_fails_closed(self):
        worker,plan=self.ready();path=self.base/'route'/('result-cas-'+plan['operation_id']+'-1.once')
        private_write(path,b'')
        with self.assertRaisesRegex(p.ProtocolError,'attempt_history_corrupt'):worker.retry_result(plan['operation_id'],2)

    def test_unknown_result_never_releases_new_begin_or_upload(self):
        worker,plan=self.ready();worker.accept_result(None)
        artifact=worker.state['current']['artifact']
        inbox,path,_=self.request(worker,2,{k:artifact[k] for k in ('result_id','result_sha256')})
        with self.assertRaisesRegex(p.ProtocolError,'route_busy_or_execution_unknown'):
            worker.prepare_begin(inbox,path)
        with self.assertRaisesRegex(p.ProtocolError,'result_upload_not_allowed'):worker.record_upload_attempt()
        with self.assertRaisesRegex(p.ProtocolError,'input_already_exposed_or_journal_unknown'):
            worker.accept_begin_and_expose(ack(plan,'forged-begin'))
        self.assertEqual(worker.state['current']['descriptor']['seq'],1)
        self.assertEqual(worker.state['current']['upload_attempts'],1)

    def test_cli_requires_explicit_review_for_exact_current_operation_and_attempt(self):
        from dots_lite import cli
        worker,plan=self.ready();root=self.base/'route'
        config={'role':'child','child_task_id':worker.identity,'route':self.r}
        with mock.patch.object(cli,'config_at',return_value=config),mock.patch.object(cli,'worker',return_value=worker):
            for decision in (None,False,True,'approved','transport_retry_after_raw_review'):
                with self.assertRaisesRegex(p.ProtocolError,'requires_explicit_review'):
                    cli.invoke(root,worker.identity,None,'child-retry-result',
                               {'retry_decision':decision,'expected_operation_id':plan['operation_id'],'expected_attempt':2})
            self.assertEqual(worker.result_retry_status()['attempts_used'],1)
            decision={'retry_decision':'same_result_cas_after_raw_and_permission_review',
                      'expected_operation_id':plan['operation_id'],'expected_attempt':2}
            status=cli.invoke(root,worker.identity,None,'child-result-retry-status',{})
            self.assertEqual(status['operation_id'],plan['operation_id']);self.assertEqual(status['next_attempt'],2)
            packet=cli.invoke(root,worker.identity,None,'child-retry-result',decision)
            self.assertEqual(p.canonical(packet['plan']),p.canonical(cli.public_plan(plan)))
            with self.assertRaisesRegex(p.ProtocolError,'attempt_mismatch'):
                cli.invoke(root,worker.identity,None,'child-retry-result',decision)
            decision['expected_operation_id']='not-this-operation';decision['expected_attempt']=3
            with self.assertRaisesRegex(p.ProtocolError,'operation_mismatch'):
                cli.invoke(root,worker.identity,None,'child-retry-result',decision)
            self.assertEqual(worker.result_retry_status()['attempts_used'],2)

    def test_ack_cleanup_keeps_only_current_seal_and_all_bounded_fences(self):
        worker,plan=self.ready();worker.accept_result(ack(plan,'rev4'))
        artifact=worker.state['current']['artifact']
        inbox,path,_=self.request(worker,2,{k:artifact[k] for k in ('result_id','result_sha256')})
        worker.prepare_begin(inbox,path)
        self.assertFalse(worker._result_seal_path(plan['operation_id']).exists())
        self.assertTrue((self.base/'route'/('result-cas-'+plan['operation_id']+'-1.once')).exists())


if __name__=='__main__':unittest.main()
