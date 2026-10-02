"""Offline worker evidence for one-use views and permission-reviewed exact upload.

Synthetic provenance is never platform approval. No test calls a model or cloud.
"""
import copy
from pathlib import Path
import unittest
from unittest import mock

from dots_lite import protocol as p
from dots_lite.storage import private_read, private_write
from dots_lite.worker import Worker
from lite_tests import test_core as fixtures


class WorkerViewsRecovery(unittest.TestCase):
    setUp=fixtures.Core.setUp
    parent=fixtures.Core.parent
    worker=fixtures.Core.worker
    request=fixtures.Core.request
    output=fixtures.Core.output

    def start(self, *, tools=False, chunked=False):
        worker=self.worker();inbox,path,desc=self.request(worker)
        if tools:
            request=p.strict_json(private_read(path));request['instructions']='Keep ALL instructions 😀'
            request['tools']=[{'type':'function','name':'echo','description':'exact constraint '*500,
                'parameters':{'type':'object','properties':{'value':{'type':'string'}},'required':['value'],'additionalProperties':False}},
                {'type':'custom','name':'patch','format':{'type':'text'}}]
            raw=p.canonical(request);private_write(path,raw)
            desc.update(request_sha256=p.sha256(raw),byte_length=len(raw))
            inbox=p.make_inbox(self.g,[{**self.r,'request':desc}],fixtures.KEY,'inbox-op-1')
        begin=worker.prepare_begin(inbox,path)
        exposed=worker.accept_begin_and_expose(fixtures.ack(begin,'rev3'),expose_to_path=chunked)
        return worker,exposed,desc,private_read(path)

    def args(self,exposed):
        b=exposed['binding']
        return [b['request_id'],b['request_sha256'],b['package_sha256'],exposed['exposure_token']]

    def finish_view(self,worker,exposed):
        parts=[];offset=0
        while True:
            chunk=worker.acquire_model_view(*self.args(exposed),offset=offset,max_bytes=256)
            parts.append(chunk['text'])
            if chunk['complete']:return p.strict_json(''.join(parts))
            offset=chunk['next_offset']

    def finish_schema(self,worker,exposed,name,offset=0):
        while True:
            chunk=worker.expose_tool_schema(*self.args(exposed),None,name,offset=offset,max_bytes=256)
            if chunk['complete']:return chunk
            offset=chunk['next_offset']

    def call(self,kind='function_call'):
        item={'id':'item_original','type':kind,'call_id':'call_original','name':'echo' if kind=='function_call' else 'patch'}
        item.update({'arguments':'{"value":"unchanged"}'} if kind=='function_call' else {'input':'original custom bytes'})
        return {'id':'response_original','status':'completed','output':[item]}

    def review(self,worker,disposition='transport_unknown'):
        return {'decision':'same_immutable_result_upload_after_raw_and_permission_review',
                'controller_task_id':worker.identity,'permission_reference':'message-fresh-user-permission',
                'raw_result_reference':'tool-call-original-outcome','prior_disposition':disposition}

    def ready_upload(self):
        worker,exposed,desc,raw=self.start()
        artifact=worker.save_actual_result(desc['request_id'],self.output())
        worker.record_upload_attempt()
        return worker,artifact

    def error(self,kind='transport_unknown'):
        return {'category':kind,'code':{'transport_unknown':'lite_transport_unknown','provider_unknown':'lite_response_invalid',
                                      'approval_blocked':'lite_approval_blocked'}[kind]}

    def retry(self,worker,artifact,attempt,review=None):
        return worker.retry_upload(artifact['result_id'],artifact['result_sha256'],attempt,'folder',review or self.review(worker))

    def test_lossy_numeric_preflight_fails_before_begin_or_any_view_cache(self):
        worker=self.worker();inbox,path,desc=self.request(worker)
        raw=b'{"model":"gpt-6.1-sol","reasoning":{"effort":"xhigh"},"input":[{"role":"user","content":"full"}],"temperature":0.12345678901234567890123456789}'
        private_write(path,raw);desc.update(request_sha256=p.sha256(raw),byte_length=len(raw))
        inbox=p.make_inbox(self.g,[{**self.r,'request':desc}],fixtures.KEY,'inbox-op-1')
        before=private_read(self.base/'route'/'journal.json')
        with self.assertRaises(p.ProtocolError):worker.prepare_begin(inbox,path)
        self.assertEqual(private_read(self.base/'route'/'journal.json'),before)
        self.assertFalse((self.base/'route'/'request-view-cache').exists())
        self.assertFalse(list((self.base/'route').glob('exposure-*.once')))

    def test_original_bytes_and_full_effective_non_tool_context_preserved(self):
        worker,exposed,desc,raw=self.start(tools=True,chunked=True)
        self.assertNotIn('exposed_path',exposed)
        self.assertNotIn(exposed['exposure_token'],p.canonical(worker.state).decode())
        view=self.finish_view(worker,exposed);source=p.strict_json(raw)
        self.assertEqual(view['request']['input'],source['input'])
        self.assertEqual(view['request']['instructions'],source['instructions'])
        self.assertNotIn('parameters',view['request']['tools'][0])
        self.assertEqual(private_read(worker.state['current']['input_path']),raw)
        with self.assertRaises(p.ProtocolError):worker.accept_begin_and_expose()

    def test_schema_and_result_require_complete_view(self):
        worker,exposed,desc,_=self.start(tools=True,chunked=True)
        with self.assertRaisesRegex(p.ProtocolError,'request_view_incomplete'):
            worker.expose_tool_schema(*self.args(exposed),None,'echo')
        with self.assertRaisesRegex(p.ProtocolError,'request_view_incomplete'):
            worker.save_actual_result(desc['request_id'],self.output())
        self.finish_view(worker,exposed)
        with self.assertRaisesRegex(p.ProtocolError,'schema_not_exposed'):
            worker.save_actual_result(desc['request_id'],self.call())

    def test_partial_schema_never_authorizes_call_but_exact_complete_schema_does(self):
        worker,exposed,desc,_=self.start(tools=True)
        chunk=worker.expose_tool_schema(*self.args(exposed),None,'echo',max_bytes=256)
        self.assertFalse(chunk['complete'])
        with self.assertRaisesRegex(p.ProtocolError,'schema_not_exposed'):
            worker.save_actual_result(desc['request_id'],self.call())
        self.finish_schema(worker,exposed,'echo',chunk['next_offset'])
        artifact=worker.save_actual_result(desc['request_id'],self.call())
        self.assertEqual(p.strict_json(private_read(artifact['path']))['output'],self.call())

    def test_custom_schema_also_required(self):
        worker,exposed,desc,_=self.start(tools=True)
        with self.assertRaisesRegex(p.ProtocolError,'schema_not_exposed'):
            worker.save_actual_result(desc['request_id'],self.call('custom_tool_call'))
        self.finish_schema(worker,exposed,'patch')
        worker.save_actual_result(desc['request_id'],self.call('custom_tool_call'))

    def test_wrong_actor_hash_package_token_and_skip_fail(self):
        worker,exposed,desc,_=self.start(chunked=True)
        for index,value in ((0,'wrong-request'),(1,'0'*64),(2,'0'*64),(3,'0'*64)):
            args=self.args(exposed);args[index]=value
            with self.subTest(index=index),self.assertRaises(p.ProtocolError):worker.acquire_model_view(*args)
        with self.assertRaises(p.ProtocolError):worker.acquire_model_view(*self.args(exposed),offset=1)
        with self.assertRaises(p.ProtocolError):Worker(self.base/'route',fixtures.KEY,'/root/other',self.clock)

    def test_restart_cannot_recover_token_or_new_output_authority(self):
        worker,exposed,desc,_=self.start()
        restarted=Worker(self.base/'route',fixtures.KEY,worker.identity,self.clock)
        with self.assertRaisesRegex(p.ProtocolError,'exposure_continuation_required'):
            restarted.save_actual_result(desc['request_id'],self.output())
        with self.assertRaises(p.ProtocolError):restarted.accept_begin_and_expose()
        restarted.save_actual_result(desc['request_id'],self.output(),exposure_token=exposed['exposure_token'])

    def test_source_mutation_fails_schema_lookup(self):
        worker,exposed,_,_=self.start(tools=True)
        private_write(worker.state['current']['input_path'],b'{}')
        with self.assertRaisesRegex(p.ProtocolError,'durable_input_changed'):
            worker.expose_tool_schema(*self.args(exposed),None,'echo')

    def test_build_failure_burns_original_exposure_no_restart_rebuild(self):
        worker=self.worker();inbox,path,_=self.request(worker);plan=worker.prepare_begin(inbox,path)
        with mock.patch.object(worker,'_build_view',side_effect=RuntimeError('synthetic build failure')):
            with self.assertRaises(RuntimeError):worker.accept_begin_and_expose(fixtures.ack(plan,'rev3'))
        restarted=Worker(self.base/'route',fixtures.KEY,worker.identity,self.clock)
        with self.assertRaises(p.ProtocolError):restarted.accept_begin_and_expose()
        self.assertEqual(restarted.state['current']['exposure'],'EXPOSED')
        self.assertIsNone(restarted.state['current']['request_view'])

    def test_same_session_view_reread_has_no_new_permit_or_metadata_mutation(self):
        worker,exposed,desc,_=self.start(chunked=True)
        first=worker.acquire_model_view(*self.args(exposed),max_bytes=256)
        before=private_read(self.base/'route'/'journal.json')
        markers=set((self.base/'route').glob('*.once'))
        replay=worker.acquire_model_view(*self.args(exposed),max_bytes=128)
        self.assertTrue(replay['replayed']);self.assertTrue(first['text'].startswith(replay['text']))
        self.assertEqual(private_read(self.base/'route'/'journal.json'),before)
        self.assertEqual(set((self.base/'route').glob('*.once')),markers)
        larger=worker.acquire_model_view(*self.args(exposed),max_bytes=4096)
        self.assertTrue(larger['replayed']);self.assertEqual(larger['text'],first['text'])
        self.assertEqual(larger['next_offset'],first['next_offset'])
        self.assertEqual(private_read(self.base/'route'/'journal.json'),before)

    def test_complete_schema_reread_does_not_issue_another_receipt(self):
        worker,exposed,_,_=self.start(tools=True)
        self.finish_schema(worker,exposed,'patch')
        before=private_read(self.base/'route'/'journal.json')
        chunk=worker.expose_tool_schema(*self.args(exposed),None,'patch',max_bytes=128)
        self.assertTrue(chunk['replayed'])
        self.assertEqual(private_read(self.base/'route'/'journal.json'),before)

    def test_forged_complete_view_ledger_without_ranges_cannot_save(self):
        worker,exposed,desc,_=self.start(chunked=True)
        with worker.journal.locked():
            state=worker.journal.read();state['current']['request_view']['complete']=True;worker._write(state)
        with self.assertRaisesRegex(p.ProtocolError,'request_view_incomplete'):
            worker.save_actual_result(desc['request_id'],self.output())

    def test_forged_schema_receipt_without_ranges_cannot_save(self):
        worker,exposed,desc,_=self.start(tools=True)
        receipt=worker._build_view(worker.state).schema_receipt(None,'echo')
        with worker.journal.locked():
            state=worker.journal.read();state['current']['request_view']['schema_receipts']=[receipt];worker._write(state)
        with self.assertRaisesRegex(p.ProtocolError,'schema_not_exposed'):
            worker.save_actual_result(desc['request_id'],self.call())

    def test_mutable_token_replacement_does_not_recover_exposure(self):
        worker,exposed,desc,_=self.start()
        with worker.journal.locked():
            state=worker.journal.read();state['current']['request_view']['token_sha256']=p.sha256(b'0'*64);worker._write(state)
        with self.assertRaisesRegex(p.ProtocolError,'exposure_binding_changed'):
            worker.save_actual_result(desc['request_id'],self.output(),exposure_token='0'*64)

    def test_restart_saved_result_before_first_upload_can_issue_exact_reviewed_attempt_one(self):
        worker,exposed,desc,_=self.start();artifact=worker.save_actual_result(desc['request_id'],self.output())
        raw=private_read(artifact['path']);restarted=Worker(self.base/'route',fixtures.KEY,worker.identity,self.clock)
        status=restarted.upload_retry_status();self.assertEqual(status['attempts_used'],0)
        self.assertEqual(status['next_attempt'],1);self.assertIsNone(status['original_failure'])
        with mock.patch.object(restarted,'save_actual_result',side_effect=AssertionError('regeneration')), \
             mock.patch.object(restarted,'accept_begin_and_expose',side_effect=AssertionError('exposure')):
            issued=self.retry(restarted,artifact,1,self.review(restarted,'not_attempted'))
        self.assertEqual(issued,artifact);self.assertEqual(private_read(artifact['path']),raw)
        state=restarted.state['current'];self.assertEqual(state['upload_attempts'],1)
        self.assertEqual(state['upload_recovery']['failures'],[]);self.assertNotIn('upload_failure',state)
        evidence=state['upload_recovery']['reviews'][0]
        self.assertEqual(evidence['outcome'],'initial_dispatch_declared');self.assertIsNone(evidence['failure_sha256'])
        self.assertTrue((self.base/'route'/('result-upload-'+artifact['result_id']+'-1.once')).exists())
        with self.assertRaises(p.ProtocolError):self.retry(restarted,artifact,1,self.review(restarted,'not_attempted'))

    def test_saved_first_dispatch_requires_honest_not_attempted_review(self):
        worker,exposed,desc,_=self.start();artifact=worker.save_actual_result(desc['request_id'],self.output())
        with self.assertRaisesRegex(p.ProtocolError,'review_disposition_required'):
            self.retry(worker,artifact,1,self.review(worker,'transport_unknown'))
        self.assertEqual(worker.upload_retry_status()['attempts_used'],0)
        self.retry(worker,artifact,1,self.review(worker,'not_attempted'))
        worker.record_upload_failure(self.error())
        with self.assertRaisesRegex(p.ProtocolError,'review_disposition_required'):
            self.retry(worker,artifact,2,self.review(worker,'not_attempted'))

    def test_reviewed_first_dispatch_crash_after_marker_does_not_refund_attempt(self):
        worker,exposed,desc,_=self.start();artifact=worker.save_actual_result(desc['request_id'],self.output())
        write=worker._write;calls=[]
        def fail_second(state):
            calls.append(state['current']['upload_attempts'])
            if len(calls)==2:raise RuntimeError('synthetic after marker crash')
            return write(state)
        with mock.patch.object(worker,'_write',side_effect=fail_second):
            with self.assertRaises(RuntimeError):self.retry(worker,artifact,1,self.review(worker,'not_attempted'))
        restarted=Worker(self.base/'route',fixtures.KEY,worker.identity,self.clock)
        self.assertEqual(restarted.state['current']['upload_attempts'],0)
        self.assertEqual(restarted.upload_retry_status()['attempts_used'],1)
        with self.assertRaises(p.ProtocolError):self.retry(restarted,artifact,1,self.review(restarted,'not_attempted'))
        with self.assertRaisesRegex(p.ProtocolError,'failure_capture_review_required'):
            self.retry(restarted,artifact,2,self.review(restarted,'transport_unknown'))

    def test_initial_permission_denial_retry_preserves_original_disposition(self):
        worker,artifact=self.ready_upload();worker.record_upload_failure(self.error('approval_blocked'))
        original=copy.deepcopy(worker.state['current']['upload_failure']);raw=private_read(artifact['path'])
        self.assertEqual(self.retry(worker,artifact,2,self.review(worker,'permission_denied')),artifact)
        state=worker.state['current']
        self.assertEqual(state['upload_failure'],original);self.assertEqual(private_read(artifact['path']),raw)
        review=state['upload_recovery']['reviews'][0]
        self.assertEqual(review['prior_disposition'],'permission_denied')
        self.assertEqual(review['authority'],'controller_declaration_only_actual_tool_review_required')

    def test_unrecognized_denial_keeps_provider_unknown_plus_distinct_review(self):
        worker,artifact=self.ready_upload();worker.record_upload_failure(self.error('provider_unknown'))
        self.retry(worker,artifact,2,self.review(worker,'permission_denied'))
        self.assertEqual(worker.state['current']['upload_failure']['category'],'provider_unknown')
        worker.record_upload_failure(self.error('provider_unknown'))
        with self.assertRaisesRegex(p.ProtocolError,'denied_again_stop'):
            self.retry(worker,artifact,3,self.review(worker,'permission_denied'))
        status=worker.upload_retry_status();self.assertTrue(status['retry_blocked'])
        self.assertEqual(status['reviews'][-1]['outcome'],'denied_again_stop')
        self.assertIsNone(status['next_attempt'])

    def test_second_machine_permission_denial_stops_even_new_declaration(self):
        worker,artifact=self.ready_upload();worker.record_upload_failure(self.error('approval_blocked'))
        self.retry(worker,artifact,2,self.review(worker,'permission_denied'))
        worker.record_upload_failure(self.error('approval_blocked'))
        with self.assertRaisesRegex(p.ProtocolError,'denied_again_stop'):
            self.retry(worker,artifact,3,self.review(worker,'permission_denied'))
        self.assertEqual(worker.upload_retry_status()['attempts_used'],2)

    def test_binding_provenance_and_old_transport_decision_rejected(self):
        worker,artifact=self.ready_upload();worker.record_upload_failure(self.error('approval_blocked'))
        base=[artifact['result_id'],artifact['result_sha256'],2,'folder',self.review(worker,'permission_denied')]
        for index,value in ((0,'wrong-result'),(1,'0'*64),(2,1),(3,'other-folder')):
            args=copy.deepcopy(base);args[index]=value
            with self.subTest(index=index),self.assertRaises(p.ProtocolError):worker.retry_upload(*args)
        for field,value in (('decision','transport_retry_after_raw_review'),('permission_reference',''),
                            ('raw_result_reference',''),('controller_task_id','/root'),('prior_disposition','transport_unknown')):
            args=copy.deepcopy(base);args[4][field]=value
            with self.subTest(field=field),self.assertRaises(p.ProtocolError):worker.retry_upload(*args)
        self.assertEqual(worker.upload_retry_status()['attempts_used'],1)

    def test_three_attempt_budget_survives_counter_rollback(self):
        worker,artifact=self.ready_upload()
        for attempt in (2,3):
            worker.record_upload_failure(self.error());self.assertEqual(self.retry(worker,artifact,attempt),artifact)
        with worker.journal.locked():
            state=worker.journal.read();state['current']['upload_attempts']=1;worker._write(state)
        self.assertEqual(worker.upload_retry_status()['attempts_used'],3)
        with self.assertRaises(p.ProtocolError):worker.record_upload_attempt()
        worker.record_upload_failure(self.error())
        with self.assertRaisesRegex(p.ProtocolError,'budget_exhausted'):self.retry(worker,artifact,4)

    def test_upload_attempt_persistence_failure_consumes_marker(self):
        worker,exposed,desc,_=self.start();artifact=worker.save_actual_result(desc['request_id'],self.output())
        with mock.patch.object(worker,'_write',side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError):worker.record_upload_attempt()
        self.assertEqual(worker.upload_retry_status()['attempts_used'],1)
        with self.assertRaises(p.ProtocolError):worker.record_upload_attempt()
        worker.record_upload_failure(self.error());self.retry(worker,artifact,2)

    def test_no_retry_after_receipt_accepted_result_pending(self):
        worker,artifact=self.ready_upload()
        worker.publish_result({'file_id':'result-file','folder_id':'folder','byte_length':artifact['byte_length']})
        with self.assertRaisesRegex(p.ProtocolError,'result_upload_not_allowed'):self.retry(worker,artifact,2)
        self.assertEqual(worker.state['phase'],'RESULT_PREPARED')

    def test_old_live_upload_records_not_retrofitted(self):
        worker,artifact=self.ready_upload()
        with worker.journal.locked():
            state=worker.journal.read();del state['current']['upload_recovery'];worker._write(state)
        before=private_read(self.base/'route'/'journal.json')
        with self.assertRaisesRegex(p.ProtocolError,'required_new_release'):worker.upload_retry_status()
        self.assertEqual(private_read(self.base/'route'/'journal.json'),before)


if __name__=='__main__':unittest.main()
