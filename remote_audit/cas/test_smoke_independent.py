"""Offline plan/evidence/one-use-consumption boundaries."""
import copy
from pathlib import Path
import tempfile
import unittest
from test_cas_independent import IndependentCASTests
from remote_transport import ProtocolError
from remote_transport.connector_smoke import prepare,verify,consume_begin

class IndependentSmokeTests(IndependentCASTests):
    # Avoid inheriting the base test methods as duplicated cases; helpers are borrowed below.
    pass

# Construct a fresh class using only the setup/helper methods.
class IndependentSmokeBoundaryTests(unittest.TestCase):
    setUp=IndependentCASTests.setUp
    ref=IndependentCASTests.ref
    admit=IndependentCASTests.admit
    claim=IndependentCASTests.claim
    dispatch_id=IndependentCASTests.dispatch_id
    begin_args=IndependentCASTests.begin_args
    def setup_begin_plan(self):
        self.claim();client=self.doc.client('principalA')
        config={'document_id':'document0','tab_id':'tab0','control_id':'control0',
                'writer_identity':'principalA','folder_id':'folder0'}
        plan=prepare(client.get_document('document0'),self.pin,config,self.messages,
                     'begin',self.begin_args(),'begin1')
        self.assertIsNone(plan['native_permit'])
        self.assertEqual(self.store.read().state['phase'],'CLAIMED')
        return plan,client
    def apply(self,plan,client):
        args=plan['tool_arguments']
        response=client.batch_update_document(args['document_id'],args['requests'],args['write_control'])
        return response,client.get_document('document0')
    def test_plan_cannot_dispatch_and_fresh_consume_is_one_use(self):
        plan,client=self.setup_begin_plan();response,readback=self.apply(plan,client)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'pin.json').write_bytes(self.pin.raw);(root/'pin.json').chmod(0o600)
            permit=consume_begin(plan,response,readback,root)
            self.assertEqual(permit['request_reference']['object_id'],self.request.oid)
            with self.assertRaises(FileExistsError):consume_begin(plan,response,readback,root)
    def test_unrelated_earlier_success_cannot_replace_lost_begin_response(self):
        plan,client=self.setup_begin_plan()
        stale={'documentId':'document0','replies':[{'replaceAllText':{'occurrencesChanged':1}}],
               'writeControl':{'requiredRevisionId':client.token()}}
        _,readback=self.apply(plan,client)
        with self.assertRaises(ProtocolError):verify(plan,stale,readback)
    def test_missing_response_count_never_consumes(self):
        plan,client=self.setup_begin_plan();response,readback=self.apply(plan,client)
        response['replies']=[{'replaceAllText':{}}]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'pin.json').write_bytes(self.pin.raw);(root/'pin.json').chmod(0o600)
            with self.assertRaises(ProtocolError):consume_begin(plan,response,readback,root)
            self.assertEqual(list(root.glob('consumed-*')),[])

# Do not duplicate the 26 core CAS tests from the imported class.
del IndependentSmokeTests
del IndependentCASTests
if __name__=='__main__':unittest.main(verbosity=2)
