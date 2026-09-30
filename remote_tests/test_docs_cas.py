import copy
import threading
import tempfile
from pathlib import Path
import unittest
from dataclasses import replace
from remote_transport import deployment,Object,ProtocolError,LocalFSBackend
from remote_transport.model import canonical,hash_bytes
from remote_transport.control import *


class FakeDocs:
    def __init__(self,state):
        self.block=block_for(state);self.revision=1;self.lock=threading.Lock()
        self.lose_response=False;self.noop=False;self.requests=[];self.flat=False
        self.missing_count=False;self.missing_doc_id=False;self.preserve_final_newline=False
    def get_document(self,doc):
        with self.lock:
            body={'content':[{'sectionBreak':{}},{'paragraph':{'elements':[{'textRun':{'content':self.block}}]}}]}
            tab={'tabId':'tab','parentTabId':None,'body':body} if self.flat else {'tabProperties':{'tabId':'tab'},'documentTab':{'body':body}}
            return {'suggestionsViewMode':'SUGGESTIONS_INLINE','documentId':doc,'revisionId':'opaque-userA-'+str(self.revision),'tabs':[tab]}
    def batch_update_document(self,doc,requests,write_control):
        with self.lock:
            self.requests.append((doc,copy.deepcopy(requests),copy.deepcopy(write_control)))
            if write_control!={'requiredRevisionId':'opaque-userA-'+str(self.revision)}:
                raise CASConflict('required_revision_mismatch')
            request=requests[0]['replaceAllText']
            if self.noop:count=0
            else:count=self.block.count(request['containsText']['text'])
            if count:
                old=request['containsText']['text']
                touches_final=self.preserve_final_newline and old.endswith('\n') and self.block.endswith(old)
                self.block=self.block.replace(old,request['replaceText'])
                if touches_final:self.block+='\n'
            self.revision+=1
            if self.lose_response:
                self.lose_response=False;raise TimeoutError('lost successful response')
            result={'documentId':doc,'writeControl':{'requiredRevisionId':'opaque-userA-'+str(self.revision)},'replies':[{'replaceAllText':{} if self.missing_count else {'occurrencesChanged':count}}]}
            if self.missing_doc_id:result.pop('documentId')
            return result


class DocsCASTests(unittest.TestCase):
    def setUp(self):
        self.pin=deployment('session','synthetic/native')
        self.initial=initial_state(self.pin,'control')
        self.api=FakeDocs(self.initial)
        self.store=GoogleDocsCASControlStore(self.api,'doc','tab','control','session','writer-A')
        self.tmp=tempfile.TemporaryDirectory()
        self.messages=LocalFSBackend(Path(self.tmp.name)/'messages',create=True)
        self.c=SessionCoordinator(self.store,self.messages)
        self.binding=binding_for(self.pin)
        self.request=Object.make(self.pin.body['identity'],'request',1,self.pin.oid,{'text':'synthetic request'})
        self.dispatch=hash_bytes(canonical({'request':self.request.oid,'incarnation':self.binding['journal_id']}))
    def tearDown(self):self.tmp.cleanup()
    def ref(self,obj):return self.messages.reference(obj,self.messages.publish(obj,None))
    def admit(self):return self.c.transition('admit',{'request':self.ref(self.request)},'admit-1')
    def claim(self):return self.c.transition('claim',{'binding':self.binding,'claim_id':'claim-1'},'claim-1')
    def begin(self,op='begin-1',snapshot=None):return self.c.transition('begin',{'binding':self.binding,'claim_id':'claim-1','dispatch_id':self.dispatch},op,snapshot=snapshot)
    def err(self,code,fn,*args,**kwargs):
        with self.assertRaises(ProtocolError) as error:fn(*args,**kwargs)
        self.assertEqual(str(error.exception),code)
    def new_pin(self):return deployment('session','synthetic/new-native',generation=2,assignment_epoch=2)

    def test_same_revision_claim_race_one_winner(self):
        self.admit();snapshot=self.store.read()
        first=self.c.transition('claim',{'binding':self.binding,'claim_id':'one'},'one',snapshot=snapshot)
        self.assertEqual(first['status'],'applied')
        with self.assertRaises(CASConflict):self.c.transition('claim',{'binding':self.binding,'claim_id':'two'},'two',snapshot=snapshot)
        self.assertEqual(self.store.read().state['claim']['id'],'one')

    def test_begin_wins_rebind_cannot_pass(self):
        self.admit();self.claim();snapshot=self.store.read()
        self.assertIsNotNone(self.begin(snapshot=snapshot)['permit'])
        with self.assertRaises(CASConflict):self.c.transition('rebind',{'deployment':self.new_pin().value},'rebind',snapshot=snapshot)
        self.err('control_rebind_after_dispatch_forbidden',self.c.transition,'rebind',{'deployment':self.new_pin().value},'rebind-fresh')

    def test_rebind_wins_stale_begin_cannot_dispatch(self):
        self.admit();self.claim();snapshot=self.store.read()
        self.c.transition('rebind',{'deployment':self.new_pin().value},'rebind',snapshot=snapshot)
        with self.assertRaises(CASConflict):self.begin(snapshot=snapshot)
        self.err('control_stale_worker_binding',self.begin)
        self.assertEqual(self.store.read().state['phase'],'IDLE')
        self.assertEqual(self.store.read().state['history'][0]['phase'],'RETIRED_BEFORE_DISPATCH')

    def test_unknown_begin_commit_never_reissues_permit(self):
        self.admit();self.claim();self.api.lose_response=True
        with self.assertRaises(CASUnknown):self.begin()
        repeated=self.begin()
        self.assertEqual(repeated['status'],'already_applied');self.assertIsNone(repeated['permit'])
        self.err('control_not_beginable',self.begin,'another-op')

    def test_missing_count_and_noop_never_return_permit(self):
        self.admit();self.claim();self.api.noop=True;self.api.missing_count=True
        with self.assertRaises(CASUnknown):self.begin()
        self.assertEqual(self.store.read().state['phase'],'CLAIMED')
        self.api.noop=False;self.api.missing_count=False
        self.assertIsNotNone(self.begin()['permit'])

    def test_changed_revision_without_match_is_not_ownership(self):
        self.admit();self.claim();self.api.noop=True
        before=self.store.read()
        with self.assertRaises(CASUnknown):self.begin()
        after=self.store.read();self.assertNotEqual(before.revision_id,after.revision_id)
        self.assertEqual(after.state['phase'],'CLAIMED')

    def test_no_target_revision_and_literal_single_tab(self):
        self.admit();_,requests,control=self.api.requests[-1]
        self.assertEqual(set(control),{'requiredRevisionId'})
        replacement=requests[0]['replaceAllText']
        self.assertEqual(replacement['tabsCriteria'],{'tabIds':['tab']})
        self.assertIs(replacement['containsText']['searchByRegex'],False)
        self.assertIs(replacement['containsText']['matchCase'],True)

    def test_snapshot_bound_to_writer_and_expiry(self):
        snapshot=self.store.read();other=GoogleDocsCASControlStore(self.api,'doc','tab','control','session','writer-B')
        state=copy.deepcopy(snapshot.state);state['control_epoch']=1
        state['operations']=[{'id':'x','kind':'admit','arguments_hash':'0'*64}]
        self.err('snapshot_writer_mismatch',other.compare_and_swap,snapshot,state)
        self.err('control_snapshot_expired',self.store.compare_and_swap,replace(snapshot,acquired_at=snapshot.acquired_at-61),state)

    def test_snapshot_state_is_copy(self):
        snapshot=self.store.read();s=snapshot.state;s['phase']='WRONG'
        self.assertEqual(snapshot.state['phase'],'IDLE')

    def test_normalized_connector_tab_shape(self):
        self.api.flat=True
        self.assertEqual(self.store.read().state,self.initial)
        self.admit();self.assertEqual(self.store.read().state['phase'],'REQUESTED')

    def test_duplicate_block_and_outside_newline_halt(self):
        self.api.block*=2
        self.err('invalid_control_block',self.store.read)
        self.api.block=block_for(self.initial)+'\n'
        self.err('invalid_control_block',self.store.read)

    def test_suggested_text_rejected(self):
        original=self.api.get_document
        def get(doc):
            data=original(doc);data['tabs'][0]['documentTab']['body']['content'][1]['paragraph']['elements'][0]['suggestedInsertionIds']=['s']
            return data
        self.api.get_document=get
        self.err('suggested_control_edit',self.store.read)

    def test_request_three_cannot_be_first(self):
        request=Object.make(self.pin.body['identity'],'request',3,self.pin.oid,{'text':'bad'},{'previous_receipt':'0'*64})
        self.err('control_first_request_required',self.c.transition,'admit',{'request':self.ref(request)},'bad')

    def test_exact_identity_not_subset(self):
        identity=dict(self.pin.body['identity'],assignment_epoch=2)
        request=Object.make(identity,'request',1,self.pin.oid,{'text':'bad'})
        self.err('control_message_scope_mismatch',self.c.transition,'admit',{'request':self.ref(request)},'bad')

    def test_full_result_and_receipt_chain(self):
        self.admit();self.claim();self.begin()
        claim=Object.make(self.pin.body['identity'],'claim',1,self.pin.oid,{'status':'claimed'},{'request':self.request.oid})
        started=Object.make(self.pin.body['identity'],'started',1,self.pin.oid,{'status':'dispatch_intent','dispatch_id':self.dispatch},{'request':self.request.oid,'claim':claim.oid})
        result=Object.make(self.pin.body['identity'],'result',1,self.pin.oid,{'text':'synthetic answer'},{'request':self.request.oid,'started':started.oid})
        args={'binding':self.binding,'dispatch_id':self.dispatch,'request':self.ref(self.request),'claim':self.ref(claim),'started':self.ref(started),'result':self.ref(result)}
        self.c.transition('result',args,'result-1')
        receipt=Object.make(self.pin.body['identity'],'receipt',1,self.pin.oid,{'status':'delivered','evidence':'synthetic observation'},{'request':self.request.oid,'result':result.oid})
        self.c.transition('receipt',{'binding':self.binding,'receipt':self.ref(receipt)},'receipt-1')
        req2=Object.make(self.pin.body['identity'],'request',2,self.pin.oid,{'text':'second'},{'previous_receipt':receipt.oid})
        self.c.transition('admit',{'request':self.ref(req2)},'admit-2')
        self.assertEqual(self.store.read().state['admissions'],2)

    def test_ambiguous_dispatch_blocks_rebind(self):
        self.admit();self.claim();self.begin()
        self.c.transition('ambiguous',{'binding':self.binding,'dispatch_id':self.dispatch},'ambiguous')
        self.err('control_rebind_after_dispatch_forbidden',self.c.transition,'rebind',{'deployment':self.new_pin().value},'rebind')

    def test_missing_response_document_id_unknown(self):
        self.api.missing_doc_id=True
        with self.assertRaises(CASUnknown):self.admit()

    def test_operation_id_reuse_with_different_arguments_blocks(self):
        self.admit()
        other=Object.make(self.pin.body['identity'],'request',1,self.pin.oid,{'text':'changed'})
        self.err('control_operation_conflict',self.c.transition,'admit',{'request':self.ref(other)},'admit-1')

    def test_docs_mandatory_final_newline_is_not_replaced(self):
        self.api.preserve_final_newline=True
        self.admit();self.claim();self.begin()
        self.assertEqual(self.store.read().state['phase'],'DISPATCH_INTENT')
        self.assertTrue(self.api.block.endswith(END))
        self.assertFalse(self.api.block.endswith('\n\n'))
        for _,requests,_ in self.api.requests:
            replace_text=requests[0]['replaceAllText']
            self.assertFalse(replace_text['containsText']['text'].endswith('\n'))
            self.assertFalse(replace_text['replaceText'].endswith('\n'))

    def test_provider_model_reproduces_old_final_newline_bug(self):
        self.api.preserve_final_newline=True
        block=self.api.block
        reply=self.api.batch_update_document('doc',[{'replaceAllText':{
            'containsText':{'text':block,'matchCase':True,'searchByRegex':False},
            'replaceText':block,'tabsCriteria':{'tabIds':['tab']}}}],
            {'requiredRevisionId':'opaque-userA-1'})
        self.assertEqual(reply['replies'][0]['replaceAllText']['occurrencesChanged'],1)
        self.assertTrue(self.api.block.endswith('\n\n'))
        self.err('invalid_control_block',self.store.read)
