import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from remote_transport import *
from remote_transport.model import canonical
from remote_transport.control import *
from remote_transport.controlled import CASController,CASWorker
from fakes import FakeDrive
from test_docs_cas import FakeDocs


class ControlledTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.pin=deployment('controlled','synthetic/native')
        self.drive=FakeDrive()
        # No listing capability: only known IDs + independently checked metadata/media.
        self.drive.capabilities=Capabilities(complete_listing=False,create_by_id=False,direct_metadata_read=True)
        self.messages=GoogleDriveBackend(self.drive,'dedicated-folder',mode='duplicate_tolerant',discovery='control_refs')
        self.docs=FakeDocs(initial_state(self.pin,'control'))
        self.store=GoogleDocsCASControlStore(self.docs,'doc','tab','control','controlled','writer')
        self.coordinator=SessionCoordinator(self.store,self.messages)
        self.cj=Journal.provision(self.root/'controller',self.pin,'controller')
        self.wj=Journal.provision(self.root/'worker',self.pin,'worker')
        self.controller=CASController(self.cj,self.messages,self.coordinator)
        self.worker=CASWorker(self.wj,self.messages,self.coordinator)
        self.controller.publish_deployment()
    def tearDown(self):self.tmp.cleanup()

    def test_two_turns_without_any_drive_list(self):
        with patch.object(self.drive,'list_page',side_effect=AssertionError('list forbidden')):
            for turn in range(2):
                req=self.controller.submit('synthetic '+str(turn),'key'+str(turn))
                state=self.store.read().state
                self.assertEqual(state['request']['object_id'],req)
                self.assertEqual(state['request']['locator']['backend'],'drive')
                permit=self.worker.start_next()
                self.assertIsNotNone(permit['control'])
                result=self.worker.complete(permit,'answer'+str(turn))
                self.assertEqual(self.controller.result(req).oid,result)
                self.controller.record_delivery(req,result,'synthetic observer '+str(turn))
        self.assertEqual(self.store.read().state['phase'],'DELIVERED')
        self.assertEqual(len(self.store.read().state['history']),1)

    def test_locator_hash_mismatch_prevents_admission(self):
        request=Object.make(self.pin.body['identity'],'request',1,self.pin.oid,{'text':'one'})
        published=self.messages.publish(request,None);ref=self.messages.reference(request,published)
        bad=copy.deepcopy(ref);bad['object_id']='0'*64
        with self.assertRaisesRegex(ProtocolError,'message_reference_mismatch'):
            self.coordinator.transition('admit',{'request':bad},'admit')
        self.assertEqual(len(self.docs.requests),0)

    def test_wrong_folder_and_metadata_rejected(self):
        request=Object.make(self.pin.body['identity'],'request',1,self.pin.oid,{'text':'one'})
        published=self.messages.publish(request,None);ref=self.messages.reference(request,published)
        self.drive.files[published]['folder']='different'
        with self.assertRaisesRegex(ProtocolError,'message_metadata_scope_mismatch'):
            self.messages.fetch(ref)

    def test_unknown_begin_no_native_permit_on_restart(self):
        self.controller.submit('one','key')
        original=self.docs.batch_update_document
        def lose_begin(doc,requests,control):
            if 'DISPATCH_INTENT' in requests[0]['replaceAllText']['replaceText']:
                self.docs.lose_response=True
            return original(doc,requests,control)
        with patch.object(self.docs,'batch_update_document',side_effect=lose_begin):
            with self.assertRaises(CASUnknown):self.worker.start_next()
        restarted=CASWorker(Journal(self.root/'worker',self.pin,'worker'),self.messages,self.coordinator)
        with self.assertRaisesRegex(ProtocolError,'control_execution_or_claim_requires_reconciliation'):
            restarted.start_next()
        self.assertEqual(self.store.read().state['phase'],'DISPATCH_INTENT')

    def test_pending_object_not_admitted_cannot_start(self):
        # Simulate publish succeeded but controller crashed before CAS admit.
        with patch.object(self.coordinator,'transition',side_effect=CASUnknown('synthetic admit not committed')):
            with self.assertRaises(CASUnknown):self.controller.submit('one','key')
        self.assertIsNone(self.worker.start_next())
        self.assertEqual(self.store.read().state['phase'],'IDLE')

    def test_completed_blob_not_control_committed_not_returned(self):
        req=self.controller.submit('one','key');permit=self.worker.start_next()
        original=self.coordinator.transition
        def fail(kind,*args,**kwargs):
            if kind=='result':raise CASUnknown('synthetic-control-outcome-unknown')
            return original(kind,*args,**kwargs)
        with patch.object(self.coordinator,'transition',side_effect=fail):
            with self.assertRaises(CASUnknown):self.worker.complete(permit,'answer')
        self.assertIsNone(self.controller.result(req))
        result=self.worker.complete(permit,'answer')
        self.assertEqual(self.controller.result(req).oid,result)

    def test_no_implicit_general_scan(self):
        with self.assertRaisesRegex(ProtocolError,'listing_disabled_for_control_refs'):
            self.messages.scan(self.pin.body['identity']['deployment_id'])

    def test_direct_read_needs_capability(self):
        self.drive.capabilities=Capabilities(complete_listing=False,create_by_id=False)
        with self.assertRaisesRegex(ProtocolError,'direct_metadata_capability_required'):
            GoogleDriveBackend(self.drive,'dedicated-folder',mode='duplicate_tolerant',discovery='control_refs')

    def test_reconcile_preserves_physical_ids_for_completion_retry(self):
        req=self.controller.submit('one','key');permit=self.worker.start_next()
        result=self.worker.complete(permit,'answer')
        self.worker.reconcile()
        self.assertEqual(self.worker.complete(permit,'answer'),result)

    def test_cli_explicit_cas_selection_bypasses_listing(self):
        import types
        import sys
        from remote_transport import cli
        local=LocalFSBackend(self.root/'local-messages',create=True)
        local.publish(self.pin,None)
        argv=['remote_transport.cli','status','--pin',str(self.root/'pin.json'),
              '--journal',str(self.root/'controller'),'--role','controller',
              '--transport','localfs','--object-root',str(local.root),
              '--docs-client-factory','reviewed_module:create_client','--control-document-id','doc',
              '--control-tab-id','tab','--control-id','control','--control-writer-identity','writer']
        (self.root/'pin.json').write_bytes(self.pin.raw);(self.root/'pin.json').chmod(0o600)
        with patch.object(sys,'argv',argv),patch('importlib.import_module',return_value=types.SimpleNamespace(create_client=lambda:self.docs)), \
             patch.object(LocalFSBackend,'scan',side_effect=AssertionError('list forbidden')):
            result=cli.main()
        self.assertEqual(result['objects'],1)

    def test_plain_adapter_refused_after_cas_enrollment(self):
        with self.assertRaisesRegex(ProtocolError,'cas_enrolled_journal_requires_controlled_adapter'):
            Worker(self.wj,self.messages)
        with self.assertRaisesRegex(ProtocolError,'cas_enrolled_journal_requires_controlled_adapter'):
            Controller(self.cj,self.messages)

    def test_existing_plain_actor_stops_after_enrollment(self):
        journal=Journal.provision(self.root/'fresh-worker',self.pin,'worker')
        plain=Worker(journal,self.messages)
        CASWorker(journal,self.messages,self.coordinator)
        with self.assertRaisesRegex(ProtocolError,'cas_enrolled_journal_requires_controlled_adapter'):
            plain.start_next()
