import copy
import json
from pathlib import Path
import tempfile
import unittest
from remote_transport import deployment,Object,ProtocolError
from remote_transport.backend import filename
from remote_transport.model import canonical,hash_bytes
from remote_transport.control import initial_state,binding_for
from remote_transport.connector_smoke import ConnectorEvidenceMessages,prepare,verify,consume_begin
from test_docs_cas import FakeDocs


class ConnectorSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.pin=deployment('connector-test','synthetic/native')
        self.write(self.root/'pin.json',self.pin.raw)
        self.docs=FakeDocs(initial_state(self.pin,'control'));self.docs.flat=True
        self.config={'document_id':'doc','tab_id':'tab','control_id':'control','writer_identity':'writer','folder_id':'folder'}
        self.request=Object.make(self.pin.body['identity'],'request',1,self.pin.oid,{'text':'synthetic'})
        self.ref={'object_id':self.request.oid,'locator':{'backend':'drive','folder_id':'folder','file_id':'file'}}
        self.file=self.root/'request.json';self.meta=self.root/'metadata.json'
        self.write(self.file,self.request.raw)
        self.write(self.meta,canonical({'structuredContent':{'id':'file','title':filename(self.request),
            'mime_type':'application/json','parent_ids':['folder']}}))
        self.messages=ConnectorEvidenceMessages('folder',[{'reference':self.ref,'file':str(self.file),'metadata':str(self.meta)}])
    def tearDown(self):self.tmp.cleanup()
    def write(self,path,data):path.write_bytes(data);path.chmod(0o600)
    def plan(self,kind,args,op):
        return prepare({'structuredContent':self.docs.get_document('doc')},self.pin,self.config,self.messages,kind,args,op)
    def execute(self,plan):
        a=plan['tool_arguments'];response=self.docs.batch_update_document(a['document_id'],a['requests'],a['write_control'])
        return {'structuredContent':response},{'structuredContent':self.docs.get_document('doc')}

    def test_prepare_is_pure_and_direct_tool_arguments_exact(self):
        plan=self.plan('admit',{'request':self.ref},'admit')
        self.assertEqual(self.docs.requests,[]);self.assertIsNone(plan['native_permit'])
        self.assertFalse(plan['trash_state_verified'])
        self.assertEqual(set(plan['tool_arguments']),{'document_id','requests','write_control'})
        response,readback=self.execute(plan)
        self.assertEqual(verify(plan,response,readback)['phase'],'REQUESTED')

    def test_normalized_metadata_hash_scope_guard(self):
        self.assertEqual(self.messages.fetch(self.ref).oid,self.request.oid)
        value=json.loads(self.meta.read_text());value['structuredContent']['parent_ids']='folder'
        self.write(self.meta,canonical(value))
        with self.assertRaisesRegex(ProtocolError,'normalized_metadata_scope_mismatch'):self.messages.fetch(self.ref)

    def test_missing_count_not_verified(self):
        plan=self.plan('admit',{'request':self.ref},'admit');response,readback=self.execute(plan)
        response['structuredContent']['replies']=[{'replaceAllText':{}}]
        with self.assertRaisesRegex(ProtocolError,'cas_fresh_exact_match_required'):verify(plan,response,readback)

    def test_fresh_begin_consumed_once(self):
        plan=self.plan('admit',{'request':self.ref},'admit');self.execute(plan)
        binding=binding_for(self.pin)
        plan=self.plan('claim',{'binding':binding,'claim_id':'claim'},'claim');self.execute(plan)
        dispatch=hash_bytes(canonical({'request':self.request.oid,'incarnation':binding['journal_id']}))
        plan=self.plan('begin',{'binding':binding,'claim_id':'claim','dispatch_id':dispatch},'begin')
        response,readback=self.execute(plan)
        permit=consume_begin(plan,response,readback,self.root)
        self.assertEqual(permit['request_reference'],self.ref)
        with self.assertRaises(FileExistsError):consume_begin(plan,response,readback,self.root)

    def test_wrong_readback_cannot_confirm(self):
        plan=self.plan('admit',{'request':self.ref},'admit');response,readback=self.execute(plan)
        other=FakeDocs(initial_state(self.pin,'control')).get_document('doc')
        with self.assertRaisesRegex(ProtocolError,'cas_response_readback_revision_mismatch'):verify(plan,response,other)

    def test_earlier_success_cannot_confirm_new_begin(self):
        admit=self.plan('admit',{'request':self.ref},'admit');old_response,_=self.execute(admit)
        binding=binding_for(self.pin)
        claim=self.plan('claim',{'binding':binding,'claim_id':'claim'},'claim');self.execute(claim)
        dispatch=hash_bytes(canonical({'request':self.request.oid,'incarnation':binding['journal_id']}))
        begin=self.plan('begin',{'binding':binding,'claim_id':'claim','dispatch_id':dispatch},'begin')
        _,readback=self.execute(begin)
        with self.assertRaisesRegex(ProtocolError,'cas_response_readback_revision_mismatch'):
            verify(begin,old_response,readback)
