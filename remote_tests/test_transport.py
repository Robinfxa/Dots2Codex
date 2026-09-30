import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from remote_transport import *
from remote_transport.backend import filename
from remote_transport.model import canonical
from fakes import FakeDrive


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pin = deployment('synthetic-session', 'synthetic/native-task')
        self.api = FakeDrive()
        self.store = GoogleDriveBackend(self.api, 'synthetic-folder')
        self.cj = Journal.provision(self.root/'controller',self.pin,'controller')
        self.wj = Journal.provision(self.root/'worker',self.pin,'worker')
        self.controller = Controller(self.cj,self.store)
        self.worker = Worker(self.wj,self.store)
        self.controller.publish_deployment()

    def tearDown(self):
        self.tmp.cleanup()

    def err(self, code, fn, *args, **kwargs):
        with self.assertRaises(ProtocolError) as c:
            fn(*args, **kwargs)
        self.assertEqual(code, str(c.exception))

    def remote_ids(self, kind):
        return [fid for fid,v in self.api.files.items() if Object.parse(v['raw']).body['kind'] == kind]

    def test_three_sticky_turns_and_receipts(self):
        for seq in range(3):
            req = self.controller.submit('question '+str(seq),str(seq))
            permit = self.worker.start_next()
            self.assertEqual(permit['request_id'],req)
            self.assertEqual(permit['native_task_id'],'synthetic/native-task')
            result = self.worker.complete(permit,'answer '+str(seq))
            self.assertEqual(self.controller.result(req).oid,result)
            self.controller.record_delivery(req,result,'synthetic caller observed result '+str(seq))
        self.err('request_budget_exceeded',self.controller.submit,'four','four')
        self.assertEqual(len(self.remote_ids('started')),3)

    def test_idempotency_and_one_outstanding_request(self):
        req = self.controller.submit('one','key')
        self.assertEqual(req,self.controller.submit('one','key'))
        self.err('idempotency_payload_conflict',self.controller.submit,'other','key')
        self.err('previous_delivery_unconfirmed',self.controller.submit,'two','two')
        permit = self.worker.start_next()
        self.err('execution_outcome_unknown',self.worker.start_next)
        result = self.worker.complete(permit,'answer')
        self.assertEqual(result,self.worker.complete(permit,'answer'))
        self.err('semantic_slot_conflict',self.worker.complete,permit,'changed')
        self.assertIsNone(self.worker.start_next())

    def test_reverse_order_and_physical_duplicates(self):
        self.api.reverse=True;self.api.page_size=2
        req=self.controller.submit('one','key')
        fid=self.remote_ids('request')[0]
        self.api.files['duplicate']=copy.deepcopy(self.api.files[fid])
        permit=self.worker.start_next()
        result=self.worker.complete(permit,'answer')
        self.assertEqual(self.controller.result(req).oid,result)
        self.assertIsNone(self.worker.start_next())

    def test_missing_predecessor_waits(self):
        req=self.controller.submit('one','1');permit=self.worker.start_next()
        result=self.worker.complete(permit,'answer');self.controller.record_delivery(req,result,'observed')
        req2=self.controller.submit('two','2')
        self.api.hidden.update(self.remote_ids('receipt'))
        self.assertIsNone(self.worker.start_next())
        self.api.hidden.clear()
        self.assertEqual(self.worker.start_next()['request_id'],req2)

    def test_partial_pagination_does_not_commit(self):
        self.controller.submit('one','key')
        before=(self.root/'worker'/'state.json').read_bytes()
        self.api.page_size=1;self.api.fail_list_page=1
        self.err('drive_http_429',self.worker.reconcile)
        self.assertEqual(before,(self.root/'worker'/'state.json').read_bytes())
        self.api.fail_list_page=None
        self.assertIsNotNone(self.worker.start_next())

    def test_incomplete_search_rejected(self):
        self.api.incomplete=True
        self.err('incomplete_search',self.worker.reconcile)

    def test_missing_media_is_unavailable_not_absent(self):
        self.controller.submit('one','key')
        self.api.fail_get.update(self.remote_ids('request'))
        self.err('drive_http_403',self.worker.start_next)
        self.api.fail_get.clear();self.assertIsNotNone(self.worker.start_next())

    def test_uncertain_create_same_reserved_id(self):
        self.api.fail_create_after_commit=True
        self.err('drive_network_outcome_unknown',self.controller.submit,'one','key')
        state=json.loads((self.root/'controller'/'state.json').read_text())
        req=state['requests']['key'];reservation=state['uploads'][req]['reservation']
        self.controller.recover_publication(req)
        self.assertEqual(self.api.create_calls[-2:],[reservation,reservation])
        self.assertEqual(len(self.remote_ids('request')),1)
        self.assertIsNotNone(self.worker.start_next())

    def test_duplicate_tolerant_ambiguous_republish(self):
        self.store=GoogleDriveBackend(self.api,'synthetic-folder',mode='duplicate_tolerant')
        self.controller=Controller(self.cj,self.store);self.worker=Worker(self.wj,self.store)
        self.api.fail_create_after_commit=True
        self.err('drive_network_outcome_unknown',self.controller.submit,'one','key')
        req=self.controller.submit('one','key')
        self.api.hidden.update(self.remote_ids('request'))
        self.assertIsNone(self.worker.start_next())
        self.controller.recover_publication(req)
        self.api.hidden.clear()
        self.assertEqual(len(self.remote_ids('request')),2)
        self.assertIsNotNone(self.worker.start_next())
        self.err('execution_outcome_unknown',self.worker.start_next)

    def test_crash_before_permit_is_ambiguous(self):
        self.controller.submit('one','key')
        original=self.store.publish
        def fail_started(obj,reservation):
            if obj.body['kind']=='started':
                raise ProtocolError('synthetic_crash')
            return original(obj,reservation)
        with patch.object(self.store,'publish',side_effect=fail_started):
            self.err('synthetic_crash',self.worker.start_next)
        restarted=Worker(Journal(self.root/'worker',self.pin,'worker'),self.store)
        self.err('execution_outcome_unknown',restarted.start_next)

    def test_crash_after_effect_before_result_is_ambiguous(self):
        req=self.controller.submit('one','key');permit=self.worker.start_next()
        effects=[permit['dispatch_id']]  # Simulated external effect happened; no result persisted.
        restarted=Worker(Journal(self.root/'worker',self.pin,'worker'),self.store)
        self.err('execution_outcome_unknown',restarted.start_next)
        self.assertEqual(len(effects),1)
        restarted.mark_ambiguous(req)
        self.assertIsNone(self.controller.result(req))

    def test_result_publication_failure_does_not_reexecute(self):
        req=self.controller.submit('one','key');permit=self.worker.start_next()
        self.api.fail_create_after_commit=True
        self.err('drive_network_outcome_unknown',self.worker.complete,permit,'answer')
        restarted=Worker(Journal(self.root/'worker',self.pin,'worker'),self.store)
        self.assertIsNone(restarted.start_next())
        self.assertEqual(self.controller.result(req).body['payload']['text'],'answer')

    def test_missing_and_wrong_journal_refused(self):
        (self.root/'worker'/'state.json').unlink()
        with self.assertRaises(FileNotFoundError):Journal(self.root/'worker',self.pin,'worker')
        with self.assertRaises(FileExistsError):Journal.provision(self.root/'worker',self.pin,'worker')
        self.err('journal_pin_mismatch',Journal,self.root/'controller',self.pin,'worker')

    def test_semantic_conflict_blocks_persistently(self):
        self.controller.submit('one','key')
        conflicting=Object.make(self.pin.body['identity'],'request',1,self.pin.oid,{'text':'different'})
        self.store.publish(conflicting,self.store.reserve())
        self.err('semantic_slot_conflict',self.worker.start_next)
        self.err('session_blocked:semantic_slot_conflict',self.worker.reconcile)

    def test_hash_poison_blocks(self):
        self.controller.submit('one','key');fid=self.remote_ids('request')[0]
        val=json.loads(self.api.files[fid]['raw']);val['body']['payload']['text']='tampered'
        self.api.files[fid]['raw']=canonical(val)
        self.err('hash_mismatch',self.worker.start_next)
        self.err('session_blocked:hash_mismatch',self.worker.reconcile)

    def test_generation_pin_not_remote_max(self):
        self.controller.submit('one','key')
        ident=dict(self.pin.body['identity'],generation=2)
        obj=Object.make(ident,'request',1,self.pin.oid,{'text':'new generation'})
        self.store.publish(obj,self.store.reserve())
        self.err('object_pin_mismatch',self.worker.start_next)

    def test_capabilities_gate(self):
        self.api.capabilities=Capabilities(complete_listing=False,create_by_id=True)
        self.err('complete_listing_capability_required',GoogleDriveBackend,self.api,'folder')
        self.api.capabilities=Capabilities(complete_listing=True,create_by_id=False)
        self.err('create_by_id_capability_required',GoogleDriveBackend,self.api,'folder')
        GoogleDriveBackend(self.api,'folder',mode='duplicate_tolerant')

    def test_bounded_poll(self):
        sleeps=[];checks=[]
        self.assertIsNone(self.worker.poll(lambda:checks.append(1),attempts=4,sleep=sleeps.append))
        self.assertEqual(len(checks),4);self.assertEqual(sleeps,[.25,.5,1])

    def test_localfs_object_backend(self):
        store=LocalFSBackend(self.root/'objects',create=True)
        store.publish(self.pin,None);store.publish(self.pin,None)
        self.assertEqual([o.oid for o in store.scan(self.pin.body['identity']['deployment_id'])],[self.pin.oid])

    def test_lost_execution_ledger_halts(self):
        self.controller.submit('one','key');self.worker.start_next()
        state=json.loads((self.root/'worker'/'state.json').read_text());state['executions']={}
        (self.root/'worker'/'state.json').write_text(json.dumps(state))
        self.err('execution_history_missing',self.worker.start_next)

if __name__=='__main__':unittest.main()
