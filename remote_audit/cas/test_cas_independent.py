"""Independent Docs-CAS control tests. Offline only, no live documents."""
import concurrent.futures
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

HERE=Path(__file__).resolve()
REPO=next(p for p in HERE.parents if (p/'remote_transport'/'control.py').is_file())
sys.path.insert(0,str(REPO))
from remote_transport import deployment, Object, ProtocolError
from remote_transport.model import canonical, hash_bytes
from remote_transport.control import (GoogleDocsCASControlStore, SessionCoordinator, CASConflict,
    CASUnknown, initial_state, block_for, binding_for)


class AtomicDocument:
    def __init__(self, state):
        self.text=block_for(state);self.revision=0;self.lock=threading.Lock()
        self.calls=[];self.reply_count=None;self.omit_count=False;self.omit_document=False
        self.commit_then_timeout=False;self.document_extra={};self.transform=None;self.on_commit=None
    def client(self, principal):
        doc=self
        class Client:
            def token(self):return str(doc.revision)+'@'+principal
            def get_document(self, document_id):
                with doc.lock:
                    out={'documentId':document_id,'revisionId':self.token(),'suggestionsViewMode':'SUGGESTIONS_INLINE',
                         'tabs':[{'tabProperties':{'tabId':'tab0'},'documentTab':{'body':{'content':[
                             {'sectionBreak':{}},{'paragraph':{'elements':[{'textRun':{'content':doc.text}}]}}
                         ]}}}]}
                    out.update(copy.deepcopy(doc.document_extra))
                    return doc.transform(out) if doc.transform else out
            def batch_update_document(self, document_id, requests, write_control):
                with doc.lock:
                    doc.calls.append((document_id,copy.deepcopy(requests),copy.deepcopy(write_control),principal))
                    if write_control!={'requiredRevisionId':self.token()}:
                        raise CASConflict('stale_required_revision')
                    r=requests[0]['replaceAllText'];old=r['containsText']['text']
                    # Native Docs preserves the mandatory final paragraph newline,
                    # even when a replaceAllText match includes that newline.
                    count=doc.text.count(old);consumed_terminal=count and doc.text.endswith(old)
                    doc.text=doc.text.replace(old,r['replaceText'])+('\n' if consumed_terminal else '')
                    doc.revision+=1
                    if doc.on_commit:doc.on_commit()
                    if doc.commit_then_timeout:
                        doc.commit_then_timeout=False;raise TimeoutError('commit response lost')
                    reply={} if doc.omit_count or count==0 else {'occurrencesChanged':count if doc.reply_count is None else doc.reply_count}
                    out={'documentId':document_id,'replies':[{'replaceAllText':reply}],
                         'writeControl':{'requiredRevisionId':self.token()}}
                    if doc.omit_document:del out['documentId']
                    return out
        return Client()


class MemoryMessages:
    def __init__(self):self.objects={}
    def reference(self,obj):
        name='ddv0-'+obj.body['identity']['deployment_id']+'-'+obj.oid+'.json'
        self.objects[obj.oid]=obj
        return {'object_id':obj.oid,'locator':{'backend':'localfs','name':name}}
    def fetch(self,ref):
        obj=self.objects.get(ref.get('object_id'))
        if obj is None or self.reference(obj)!=ref:raise ProtocolError('message_reference_mismatch')
        return obj


class IndependentCASTests(unittest.TestCase):
    def setUp(self):
        self.pin=deployment('cas_audit','synthetic/native/task')
        self.initial=initial_state(self.pin,'control0')
        self.doc=AtomicDocument(self.initial)
        self.store=GoogleDocsCASControlStore(self.doc.client('principalA'),'document0','tab0','control0','cas_audit','principalA')
        self.messages=MemoryMessages();self.coordinator=SessionCoordinator(self.store,self.messages)
        self.request=Object.make(self.pin.body['identity'],'request',1,self.pin.oid,{'text':'synthetic'})
    def ref(self,obj):return self.messages.reference(obj)
    def admit(self):return self.coordinator.transition('admit',{'request':self.ref(self.request)},'admit1')
    def claim(self):
        self.admit()
        return self.coordinator.transition('claim',{'binding':binding_for(self.pin),'claim_id':'claim1'},'claim1')
    def dispatch_id(self):return hash_bytes(canonical({'request':self.request.oid,'incarnation':self.pin.body['identity']['worker_journal_id']}))
    def begin_args(self):return {'binding':binding_for(self.pin),'claim_id':'claim1','dispatch_id':self.dispatch_id()}
    def test_state_snapshot_is_immutable_copy(self):
        snapshot=self.store.read();state=snapshot.state;state['phase']='DISPATCH_INTENT'
        self.assertEqual(snapshot.state['phase'],'IDLE')
    def test_two_concurrent_claims_have_one_winner(self):
        self.admit();snapshot=self.store.read()
        def attempt(number):
            try:return self.coordinator.transition('claim',{'binding':binding_for(self.pin),'claim_id':'claim'+str(number)},'op'+str(number),snapshot=snapshot)
            except CASConflict:return 'conflict'
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:out=list(pool.map(attempt,[1,2]))
        self.assertEqual(sum(isinstance(x,dict) for x in out),1)
        self.assertEqual(out.count('conflict'),1)
    def test_two_begins_get_one_fresh_permit(self):
        self.claim();snapshot=self.store.read()
        def attempt(number):
            args=self.begin_args()
            try:return self.coordinator.transition('begin',args,'begin'+str(number),snapshot=snapshot)
            except CASConflict:return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:out=list(pool.map(attempt,[1,2]))
        self.assertEqual(sum(x is not None and x['permit'] is not None for x in out),1)
    def test_begin_and_rebind_share_revision_authority(self):
        self.claim();snapshot=self.store.read()
        pin2=deployment('cas_audit','synthetic/native/newtask',generation=2)
        def begin():
            try:return 'begin',self.coordinator.transition('begin',self.begin_args(),'begin1',snapshot=snapshot)
            except CASConflict:return 'conflict',None
        def rebind():
            try:return 'rebind',self.coordinator.transition('rebind',{'deployment':pin2.value},'rebind1',snapshot=snapshot)
            except CASConflict:return 'conflict',None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f=pool.submit(begin);g=pool.submit(rebind);out=[f.result(),g.result()]
        winners=[kind for kind,result in out if kind!='conflict'];self.assertEqual(len(winners),1)
        state=self.store.read().state
        self.assertEqual(state['phase'],'DISPATCH_INTENT' if winners[0]=='begin' else 'IDLE')
        self.assertEqual(state['binding']['generation'],1 if winners[0]=='begin' else 2)
    def test_commit_with_lost_reply_never_recovers_permit(self):
        self.claim();self.doc.commit_then_timeout=True
        with self.assertRaises(CASUnknown):self.coordinator.transition('begin',self.begin_args(),'begin1')
        out=self.coordinator.transition('begin',self.begin_args(),'begin1')
        self.assertEqual(out['status'],'already_applied');self.assertIsNone(out['permit'])
        with self.assertRaises(ProtocolError):self.coordinator.transition('begin',self.begin_args(),'new_begin')
    def test_missing_count_is_unknown_even_with_new_revision(self):
        self.claim();self.doc.omit_count=True
        with self.assertRaises(CASUnknown):self.coordinator.transition('begin',self.begin_args(),'begin1')
        self.assertEqual(self.store.read().state['phase'],'DISPATCH_INTENT')
    def test_noninteger_one_count_is_not_success(self):
        self.claim();self.doc.reply_count=True
        with self.assertRaises(CASUnknown):self.coordinator.transition('begin',self.begin_args(),'begin1')
    def test_multiple_replacements_are_not_success(self):
        self.claim();self.doc.reply_count=2
        with self.assertRaises(CASUnknown):self.coordinator.transition('begin',self.begin_args(),'begin1')
    def test_required_revision_and_pinned_literal_tab_are_sent(self):
        snapshot=self.store.read();self.coordinator.transition('admit',{'request':self.ref(self.request)},'admit1',snapshot=snapshot)
        docid,requests,control,principal=self.doc.calls[-1]
        self.assertEqual(control,{'requiredRevisionId':snapshot.revision_id})
        self.assertEqual(len(requests),1)
        self.assertEqual(requests[0]['replaceAllText']['tabsCriteria'],{'tabIds':['tab0']})
        self.assertEqual(requests[0]['replaceAllText']['containsText'],{'text':snapshot.block[:-1],'matchCase':True,'searchByRegex':False})
        self.assertFalse(requests[0]['replaceAllText']['replaceText'].endswith('\n'))
    def test_provider_terminal_newline_reproduces_old_api_bug(self):
        snapshot=self.store.read();plan=self.coordinator.plan('admit',{'request':self.ref(self.request)},'admit1',snapshot=snapshot)
        args=self.store.prepare_update(snapshot,plan['state']);r=args['requests'][0]['replaceAllText']
        r['containsText']['text']=snapshot.block;r['replaceText']=block_for(plan['state'])
        response=self.doc.client('principalA').batch_update_document(args['document_id'],args['requests'],args['write_control'])
        self.assertEqual(response['replies'][0]['replaceAllText']['occurrencesChanged'],1)
        self.assertEqual(self.doc.text,block_for(plan['state'])+'\n')
        with self.assertRaisesRegex(ProtocolError,'invalid_control_block'):self.store.read()
    def test_multiple_transitions_preserve_exactly_one_terminal_newline(self):
        self.claim();out=self.coordinator.transition('begin',self.begin_args(),'begin1')
        self.assertIsNotNone(out['permit'])
        self.assertEqual(self.doc.text,block_for(out['state']))
        self.assertTrue(self.doc.text.endswith('\n'));self.assertFalse(self.doc.text.endswith('\n\n'))
        self.assertEqual(self.store.read().state,out['state'])
    def test_extra_terminal_newline_is_not_silently_normalized(self):
        self.doc.text+='\n'
        with self.assertRaisesRegex(ProtocolError,'invalid_control_block'):self.store.read()
        self.assertFalse(self.doc.calls)
    def test_snapshot_from_other_principal_rejected(self):
        other=GoogleDocsCASControlStore(self.doc.client('principalB'),'document0','tab0','control0','cas_audit','principalB')
        snapshot=other.read();before=len(self.doc.calls)
        with self.assertRaisesRegex(ProtocolError,'snapshot_writer_mismatch'):
            self.coordinator.transition('admit',{'request':self.ref(self.request)},'admit1',snapshot=snapshot)
        self.assertEqual(len(self.doc.calls),before)
    def test_missing_revision_rejected(self):
        self.doc.document_extra={'revisionId':None}
        with self.assertRaisesRegex(ProtocolError,'editable_revision_required'):self.store.read()
    def test_expired_snapshot_rejected_before_write(self):
        snapshot=replace(self.store.read(),acquired_at=0)
        with self.assertRaisesRegex(ProtocolError,'control_snapshot_expired'):
            self.coordinator.transition('admit',{'request':self.ref(self.request)},'admit1',snapshot=snapshot)
        self.assertFalse(self.doc.calls)
    def test_unknown_dispatch_prevents_rebind(self):
        self.claim();self.coordinator.transition('begin',self.begin_args(),'begin1')
        args={'binding':binding_for(self.pin),'dispatch_id':self.dispatch_id()}
        self.coordinator.transition('ambiguous',args,'unknown1')
        pin2=deployment('cas_audit','synthetic/native/newtask',generation=2)
        with self.assertRaisesRegex(ProtocolError,'control_rebind_after_dispatch_forbidden'):
            self.coordinator.transition('rebind',{'deployment':pin2.value},'rebind1')
    def finish_result(self):
        self.claim();self.coordinator.transition('begin',self.begin_args(),'begin1')
        ident=self.pin.body['identity'];req=self.request
        claim=Object.make(ident,'claim',1,self.pin.oid,{'status':'claimed'},{'request':req.oid})
        started=Object.make(ident,'started',1,self.pin.oid,{'status':'dispatch_intent','dispatch_id':self.dispatch_id()},
                            {'request':req.oid,'claim':claim.oid})
        result=Object.make(ident,'result',1,self.pin.oid,{'text':'synthetic answer'},
                           {'request':req.oid,'started':started.oid})
        args={'binding':binding_for(self.pin),'dispatch_id':self.dispatch_id(),'request':self.ref(req),
              'claim':self.ref(claim),'started':self.ref(started),'result':self.ref(result)}
        self.coordinator.transition('result',args,'result1')
        return result,args
    def test_complete_result_and_receipt_graph(self):
        result,args=self.finish_result()
        receipt=Object.make(self.pin.body['identity'],'receipt',1,self.pin.oid,
                            {'status':'delivered','evidence':'synthetic client observation'},
                            {'request':self.request.oid,'result':result.oid})
        out=self.coordinator.transition('receipt',{'binding':binding_for(self.pin),'receipt':self.ref(receipt)},'receipt1')
        self.assertEqual(out['state']['phase'],'DELIVERED');self.assertIsNone(out['permit'])
        req2=Object.make(self.pin.body['identity'],'request',2,self.pin.oid,{'text':'second'},
                         {'previous_receipt':receipt.oid})
        out=self.coordinator.transition('admit',{'request':self.ref(req2)},'admit2')
        self.assertEqual(out['state']['admissions'],2)
    def test_first_request_cannot_skip_sequence(self):
        req=Object.make(self.pin.body['identity'],'request',3,self.pin.oid,{'text':'skip'},
                        {'previous_receipt':'0'*64})
        with self.assertRaises(ProtocolError):self.coordinator.transition('admit',{'request':self.ref(req)},'admit1')
        self.assertFalse(self.doc.calls)
    def test_full_deployment_identity_is_checked(self):
        ident=dict(self.pin.body['identity'],assignment_epoch=2)
        req=Object.make(ident,'request',1,self.pin.oid,{'text':'wrong assignment'})
        with self.assertRaisesRegex(ProtocolError,'control_message_scope_mismatch'):
            self.coordinator.transition('admit',{'request':self.ref(req)},'admit1')
        self.assertFalse(self.doc.calls)
    def test_receipt_for_wrong_result_is_rejected(self):
        result,args=self.finish_result()
        receipt=Object.make(self.pin.body['identity'],'receipt',1,self.pin.oid,
                            {'status':'delivered','evidence':'not matching result'},
                            {'request':self.request.oid,'result':'0'*64})
        with self.assertRaisesRegex(ProtocolError,'control_receipt_mismatch'):
            self.coordinator.transition('receipt',{'binding':binding_for(self.pin),'receipt':self.ref(receipt)},'receipt1')
    def test_result_without_full_graph_is_rejected(self):
        self.claim();self.coordinator.transition('begin',self.begin_args(),'begin1')
        with self.assertRaisesRegex(ProtocolError,'invalid_control_arguments'):
            self.coordinator.transition('result',{'binding':binding_for(self.pin),'dispatch_id':self.dispatch_id(),'result':{}},'result1')
    def test_stale_begin_after_rebind_cannot_dispatch(self):
        self.claim();snapshot=self.store.read()
        pin2=deployment('cas_audit','synthetic/native/newtask',generation=2)
        self.coordinator.transition('rebind',{'deployment':pin2.value},'rebind1',snapshot=snapshot)
        with self.assertRaises(CASConflict):self.coordinator.transition('begin',self.begin_args(),'begin1',snapshot=snapshot)
        with self.assertRaisesRegex(ProtocolError,'control_stale_worker_binding'):
            self.coordinator.transition('begin',self.begin_args(),'begin2')
    def test_missing_response_document_id_is_unknown(self):
        self.claim();self.doc.omit_document=True
        with self.assertRaises(CASUnknown):self.coordinator.transition('begin',self.begin_args(),'begin1')
    def test_zero_match_success_cannot_grant_ownership(self):
        snapshot=self.store.read()
        altered=snapshot.state;altered['binding']['native_task_id']='synthetic/other'
        altered['binding']['identity']['native_task_id']='synthetic/other'
        fake=replace(snapshot,block=block_for(altered))
        # Correct revision, but literal old block is absent; Docs can still return 200.
        new=copy.deepcopy(altered);new['control_epoch']=1
        new['operations']=[{'id':'noop1','kind':'rebind','arguments_hash':'0'*64}]
        with self.assertRaises(CASUnknown):self.store.compare_and_swap(fake,new)
        self.assertEqual(self.store.read().state['control_epoch'],0)
    def test_extra_tab_and_suggested_text_rejected(self):
        def extra(doc):doc['tabs'].append(copy.deepcopy(doc['tabs'][0]));return doc
        self.doc.transform=extra
        with self.assertRaises(ProtocolError):self.store.read()
        def suggested(doc):doc['tabs'][0]['documentTab']['body']['content'][1]['paragraph']['elements'][0]['suggestedInsertionIds']=['suggestion'];return doc
        self.doc.transform=suggested
        with self.assertRaisesRegex(ProtocolError,'suggested_control_edit'):self.store.read()
    def test_preview_suggestions_view_is_rejected(self):
        self.doc.document_extra={'suggestionsViewMode':'PREVIEW_WITHOUT_SUGGESTIONS'}
        with self.assertRaises(ProtocolError):self.store.read()
    def test_expired_deployment_cannot_begin(self):
        self.claim()
        with patch('remote_transport.control.time.time',return_value=self.pin.body['payload']['expires']+1):
            with self.assertRaisesRegex(ProtocolError,'control_deployment_expired'):
                self.coordinator.transition('begin',self.begin_args(),'begin1')
    def test_begin_commit_past_expiry_does_not_return_permit(self):
        self.claim();now=[self.pin.body['payload']['expires']-1]
        self.doc.on_commit=lambda:now.__setitem__(0,self.pin.body['payload']['expires']+1)
        with patch('remote_transport.control.time.time',side_effect=lambda:now[0]):
            with self.assertRaises(ProtocolError):
                self.coordinator.transition('begin',self.begin_args(),'begin1')
        self.assertEqual(self.store.read().state['phase'],'DISPATCH_INTENT')
    def test_changed_operation_args_cannot_reuse_id(self):
        self.admit()
        with self.assertRaisesRegex(ProtocolError,'control_operation_conflict'):
            self.coordinator.transition('claim',{'binding':binding_for(self.pin),'claim_id':'claim1'},'admit1')


if __name__=='__main__':unittest.main(verbosity=2)
