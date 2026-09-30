"""CAS-fenced wrappers with real local journals and offline Docs authority."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_cas_independent import AtomicDocument
from remote_transport import deployment, Object, ProtocolError, Journal, LocalFSBackend, Worker
from remote_transport.control import GoogleDocsCASControlStore, SessionCoordinator, initial_state, CASUnknown
from remote_transport.controlled import CASController, CASWorker

class IndependentControlledTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.pin=deployment('controlled_audit','synthetic/native/task')
        self.messages=LocalFSBackend(self.root/'messages',create=True)
        self.doc=AtomicDocument(initial_state(self.pin,'control0'))
        self.store=GoogleDocsCASControlStore(self.doc.client('principalA'),'document0','tab0','control0','controlled_audit','principalA')
        self.coordinator=SessionCoordinator(self.store,self.messages)
        self.cj=Journal.provision(self.root/'controller',self.pin,'controller')
        self.wj=Journal.provision(self.root/'worker',self.pin,'worker')
        self.controller=CASController(self.cj,self.messages,self.coordinator)
        self.worker=CASWorker(self.wj,self.messages,self.coordinator)
        self.controller.publish_deployment()
    def tearDown(self):self.tmp.cleanup()
    def test_two_turns_use_refs_without_listing(self):
        with patch.object(self.messages,'scan',side_effect=AssertionError('listing forbidden')):
            for i in range(2):
                req=self.controller.submit('request'+str(i),'key'+str(i))
                permit=self.worker.start_next();self.assertIsNotNone(permit)
                self.assertEqual(permit['control']['request_id'],req)
                result=self.worker.complete(permit,'answer'+str(i))
                self.assertEqual(self.controller.result(req).oid,result)
                self.controller.record_delivery(req,result,'synthetic answer observed'+str(i))
        self.assertEqual(self.store.read().state['phase'],'DELIVERED')
    def test_fresh_local_permit_never_escapes_failed_cas_begin(self):
        self.controller.submit('request','key')
        original=self.doc.client('unused')
        before=self.doc.revision
        old=self.doc.on_commit
        def fail_on_begin():
            if '"phase":"DISPATCH_INTENT"' in self.doc.text:
                self.doc.commit_then_timeout=True
        self.doc.on_commit=fail_on_begin
        with self.assertRaises(CASUnknown):self.worker.start_next()
        self.doc.on_commit=None
        with self.assertRaises(ProtocolError):self.worker.start_next()
        resumed=CASWorker(Journal(self.root/'worker',self.pin,'worker'),self.messages,self.coordinator)
        with self.assertRaises(ProtocolError):resumed.start_next()
        self.assertEqual(self.store.read().state['phase'],'DISPATCH_INTENT')
    def test_old_wrapper_cannot_start_after_control_rebind(self):
        self.controller.submit('request','key')
        new=deployment('controlled_audit','synthetic/native/new',generation=2)
        self.coordinator.transition('rebind',{'deployment':new.value},'rebind1')
        with self.assertRaisesRegex(ProtocolError,'control_stale_worker_binding'):self.worker.start_next()
        with self.assertRaisesRegex(ProtocolError,'control_stale_worker_binding'):self.controller.result(next(iter(json.loads((self.root/'controller'/'state.json').read_text())['requests'].values())))
    def test_unfenced_completion_without_control_permit_rejected(self):
        self.controller.submit('request','key');permit=self.worker.start_next();del permit['control']
        with self.assertRaisesRegex(ProtocolError,'control_permit_required'):self.worker.complete(permit,'answer')
    def test_read_reconciliation_preserves_published_locators(self):
        self.controller.submit('request','key');permit=self.worker.start_next()
        result=self.worker.complete(permit,'answer')
        self.worker.reconcile()
        self.assertEqual(self.worker.complete(permit,'answer'),result)
    def test_enrolled_journal_cannot_downgrade_to_unfenced_worker(self):
        self.controller.submit('request','key')
        new=deployment('controlled_audit','synthetic/native/new',generation=2)
        self.coordinator.transition('rebind',{'deployment':new.value},'rebind1')
        with self.assertRaises(ProtocolError):
            plain=Worker(self.wj,self.messages)
            plain.start_next()
    def test_control_result_ref_tamper_cannot_deliver(self):
        req=self.controller.submit('request','key');permit=self.worker.start_next();self.worker.complete(permit,'answer')
        state=self.store.read().state;state['result']['reference']['object_id']='0'*64
        from remote_transport.control import block_for
        self.doc.text=block_for(state);self.doc.revision+=1
        with self.assertRaises((ProtocolError,FileNotFoundError)):self.controller.result(req)

if __name__=='__main__':unittest.main(verbosity=2)
