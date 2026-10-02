import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dots_lite import docs,protocol as p
from dots_lite.storage import private_write,Journal
from dots_lite.worker import ParentController,Worker

KEY='11'*32
NOW=1800000000

def grant():
    return {'protocol':p.PROTOCOL,'activation_id':'activation','folder_id':'folder','inbox_id':'inbox','created_at':NOW-1,
            'expires_at':NOW+14400,'allowed_pairs':[{'model':'gpt-6.1-sol','reasoning_effort':'xhigh'}],
            'limits':copy.deepcopy(p.DEFAULT_LIMITS),'package_sha256':'a'*64}

def route():
    return {'route_id':'route','identity_sha256':'b'*64,'model':'gpt-6.1-sol','reasoning_effort':'xhigh','outbox_id':'outbox','request':None,'stop':False}

def resource(text='\n',revision='rev0',document_id='outbox',flat=True):
    cursor=1;body=[]
    for line in text.splitlines(keepends=True):
        end=cursor+len(line.encode('utf-16-le'))//2
        body.append({'startIndex':cursor,'endIndex':end,'paragraph':{'elements':[{'startIndex':cursor,'endIndex':end,'textRun':{'content':line}}]}});cursor=end
    tab={'tabId':'tab','nestingLevel':None,'body':{'content':body}}
    if not flat:tab={'tabProperties':{'tabId':'tab'},'documentTab':{'body':{'content':body}}}
    return {'documentId':document_id,'revisionId':revision,'suggestionsViewMode':'SUGGESTIONS_INLINE','body':None,'tabs':[tab]}

def ack(plan,revision):
    return {'documentId':plan['document_id'],'replies':[{} for _ in plan['body']['requests']],
            'writeControl':{'requiredRevisionId':revision,'targetRevisionId':None}}

class Core(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        self.g=grant();self.r=route();self.clock=lambda:(NOW,100,'boot')
    def parent(self):
        snap=docs.snapshot(resource(),'outbox',max_bytes=p.OUTBOX_MAX_BYTES)
        parent=ParentController.create(self.base/'route',self.g,KEY,self.r,'/root',snap,self.clock)
        args={'task_name':'child_route','message':'Wait for authenticated handoff. No request body.','model':self.r['model'],'reasoning_effort':'xhigh','fork_turns':'none'}
        plan=parent.reserve_spawn(args)
        return parent,args,plan
    def worker(self):
        parent,args,plan=self.parent()
        self.assertEqual(parent.accept_spawn_reserved_and_issue(ack(plan,'rev1')),args)
        plan=parent.record_actual_admission(args,{'task_name':'/root/child_route','agent_id':'actual-id'})
        handoff=parent.accept_admission(ack(plan,'rev2'))
        worker=Worker(self.base/'route',KEY,'/root/child_route',self.clock)
        worker.takeover(handoff)
        return worker
    def request(self,worker,seq=1,previous=None):
        raw=p.canonical({'model':self.r['model'],'reasoning':{'effort':'xhigh'},'stream':True,'input':[{'role':'user','content':'Hi 😀'}]})
        path=self.base/'raw.json';private_write(path,raw)
        desc={'seq':seq,'request_id':'request-'+str(seq),'request_sha256':p.sha256(raw),'byte_length':len(raw),'file_id':'request-file',
              'folder_id':'folder','previous_result_ack':previous,'begin_before':self.g['expires_at']}
        route={**self.r,'request':desc}
        return p.make_inbox(self.g,[route],KEY,'inbox-op-'+str(seq)),path,desc
    def output(self):return {'id':'resp_exact','status':'completed','output':[{'id':'msg_exact','type':'message','role':'assistant','content':[{'type':'output_text','text':'Hello'}]}]}
    def test_docs_utf16_and_nullable_ack(self):
        snap=docs.snapshot(resource('😀\n'),'outbox',max_bytes=p.OUTBOX_MAX_BYTES)
        plan=docs.plan_write(snap,{'operation_id':'op','value':'😀'},'op')
        self.assertEqual(plan['body']['requests'][0]['deleteContentRange']['range']['endIndex'],3)
        self.assertEqual(len(plan['body']['requests']),2)
        self.assertEqual(docs.accept_write(plan,{'structuredContent':ack(plan,'r2')})['status'],'accepted')
        bad=ack(plan,'r2');bad['revisionId']='different'
        self.assertEqual(docs.accept_write(plan,bad)['status'],'unknown')
        self.assertEqual(docs.reconcile_write(plan,resource('😀\n','r3'))['status'],'unknown')
    def test_normal_full_lifecycle_one_exposure_and_next_ack(self):
        worker=self.worker();inbox,path,desc=self.request(worker)
        plan=worker.prepare_begin(inbox,path)
        request=worker.accept_begin_and_expose(ack(plan,'rev3'))
        self.assertEqual(request['input'][0]['content'],'Hi 😀')
        with self.assertRaises(p.ProtocolError):worker.accept_begin_and_expose(ack(plan,'rev3'))
        artifact=worker.save_actual_result(desc['request_id'],self.output())
        for _ in range(3):self.assertEqual(worker.record_upload_attempt(),artifact)
        with self.assertRaises(p.ProtocolError):worker.record_upload_attempt()
        plan=worker.publish_result({'file_id':'result-file','folder_id':'folder','byte_length':artifact['byte_length']})
        self.assertEqual(worker.accept_result(ack(plan,'rev4'))['status'],'accepted')
        outbox=p.parse_outbox(worker.state['outbox'],self.r,KEY,self.g)
        result=p.strict_json(Path(artifact['path']).read_bytes());p.validate_result_envelope(result,self.g,self.r,outbox)
        next_inbox,path,_=self.request(worker,2,{k:artifact[k] for k in ('result_id','result_sha256')})
        plan=worker.prepare_begin(next_inbox,path)
        self.assertFalse(Path(artifact['path']).exists())
        worker.accept_begin_and_expose(ack(plan,'rev5'))
        self.assertEqual(len(worker.state['history']),1)
    def test_unknown_begin_exact_reconcile_only_first_exposure(self):
        worker=self.worker();inbox,path,desc=self.request(worker);plan=worker.prepare_begin(inbox,path)
        self.assertEqual(worker.accept_begin_and_expose({'text':'success'})['status'],'unknown')
        self.assertEqual(worker.state['current']['exposure'],'INPUT_NOT_EXPOSED')
        old=resource(plan['source']['text'],'rev2')
        self.assertEqual(worker.accept_begin_and_expose(readback=old)['status'],'unknown')
        fresh=resource(plan['text'],'rev3')
        worker.accept_begin_and_expose(readback=fresh)
        worker2=Worker(self.base/'route',KEY,'/root/child_route',self.clock)
        with self.assertRaises(p.ProtocolError):worker2.accept_begin_and_expose(readback=fresh)
    def test_spawn_unknown_never_reissues_and_parent_loses_write_ownership(self):
        parent,args,plan=self.parent()
        self.assertEqual(parent.accept_spawn_reserved_and_issue(None)['status'],'unknown')
        self.assertEqual(parent.accept_spawn_reserved_and_issue(readback=resource(plan['text'],'rev1')),args)
        with self.assertRaises(p.ProtocolError):parent.accept_spawn_reserved_and_issue(ack(plan,'rev1'))
        plan=parent.record_actual_admission(args,{'task_name':'/root/child_route'})
        self.assertEqual(parent.accept_admission(None)['status'],'unknown')
        parent.accept_admission(readback=resource(plan['text'],'rev2'))
        with self.assertRaises(p.ProtocolError):parent.reserve_spawn(args)
    def test_expiry_blocks_new_exposure_but_saved_result_after_expiry_allowed(self):
        worker=self.worker();inbox,path,desc=self.request(worker);plan=worker.prepare_begin(inbox,path)
        worker.accept_begin_and_expose(ack(plan,'rev3'))
        worker.clock=lambda:(NOW+20000,20100,'boot')
        artifact=worker.save_actual_result(desc['request_id'],self.output());worker.record_upload_attempt()
        plan=worker.publish_result({'file_id':'result-file','folder_id':'folder','byte_length':artifact['byte_length']})
        self.assertEqual(worker.accept_result(ack(plan,'rev4'))['status'],'accepted')
        inbox,path,_=self.request(worker,2,{k:artifact[k] for k in ('result_id','result_sha256')})
        with self.assertRaisesRegex(p.ProtocolError,'authorization_expired'):worker.prepare_begin(inbox,path)
    def test_mutable_journal_rollback_cannot_restore_spawn_or_input_permit(self):
        parent,args,plan=self.parent()
        journal=self.base/'route'/'journal.json';before=journal.read_bytes()
        parent.accept_spawn_reserved_and_issue(ack(plan,'rev1'))
        private_write(journal,before)
        with self.assertRaisesRegex(p.ProtocolError,'fence_already_burned'):
            parent.accept_spawn_reserved_and_issue(ack(plan,'rev1'))
        # Use another isolated route to verify the independent input marker.
        original=self.base;self.base=self.base/'other';self.base.mkdir()
        worker=self.worker();inbox,path,_=self.request(worker);plan=worker.prepare_begin(inbox,path)
        journal=self.base/'route'/'journal.json';before=journal.read_bytes()
        worker.accept_begin_and_expose(ack(plan,'rev3'))
        private_write(journal,before)
        with self.assertRaisesRegex(p.ProtocolError,'fence_already_burned'):
            worker.accept_begin_and_expose(readback=resource(plan['text'],'rev3'))
        self.base=original

    def test_missing_journal_never_reconstructs_exposure(self):
        worker=self.worker();inbox,path,_=self.request(worker);worker.prepare_begin(inbox,path)
        (self.base/'route'/'journal.json').unlink()
        with self.assertRaisesRegex(p.ProtocolError,'journal_missing'):Worker(self.base/'route',KEY,'/root/child_route',self.clock)
    def test_tampered_inbox_fails_and_wrong_hash_never_begin(self):
        worker=self.worker();inbox,path,_=self.request(worker)
        bad=copy.deepcopy(inbox);bad['routes'][0]['model']='other'
        with self.assertRaises(p.ProtocolError):worker.prepare_begin(bad,path)
        private_write(path,b'{}')
        with self.assertRaisesRegex(p.ProtocolError,'request_bytes_mismatch'):worker.prepare_begin(inbox,path)
        self.assertEqual(worker.state['phase'],'ADMITTED')
    def test_fixed_control_size_and_payload_cache_through_128_turns(self):
        worker=self.worker();previous=None;maximum=0
        for seq in range(1,129):
            inbox,path,desc=self.request(worker,seq,previous)
            plan=worker.prepare_begin(inbox,path)
            maximum=max(maximum,len(plan['text'].encode()))
            worker.accept_begin_and_expose(ack(plan,'begin-'+str(seq)))
            artifact=worker.save_actual_result(desc['request_id'],self.output());worker.record_upload_attempt()
            plan=worker.publish_result({'file_id':'result-'+str(seq),'folder_id':'folder','byte_length':artifact['byte_length']})
            maximum=max(maximum,len(plan['text'].encode()))
            worker.accept_result(ack(plan,'result-'+str(seq)))
            previous={k:artifact[k] for k in ('result_id','result_sha256')}
        self.assertLess(maximum,p.OUTBOX_MAX_BYTES)
        self.assertEqual(len(worker.state['history']),127)
        self.assertEqual(len(list((self.base/'route').glob('input-*.json'))),1)
        self.assertEqual(len(list((self.base/'route').glob('result-*.json'))),1)
        with self.assertRaises(p.ProtocolError):self.request(worker,129,previous)

    def test_clock_restart_uncertainty_blocks_only_new_begin(self):
        worker=self.worker();inbox,path,desc=self.request(worker)
        worker.clock=lambda:(NOW,1,'other-boot')
        with self.assertRaisesRegex(p.ProtocolError,'clock_uncertain'):worker.prepare_begin(inbox,path)
        self.assertEqual(worker.state['phase'],'ADMITTED')

    def test_oversize_and_duplicate_json_rejected(self):
        with self.assertRaises(p.ProtocolError):p.strict_json('{"a":1,"a":2}')
        g=grant();g['limits']['max_routes']=4
        with self.assertRaisesRegex(p.ProtocolError,'unsupported_limits'):p.validate_grant(g)
        g=grant();g['allowed_pairs'][0]['reasoning_effort']='max'
        with self.assertRaises(p.ProtocolError):p.validate_grant(g)

if __name__=='__main__':unittest.main()
