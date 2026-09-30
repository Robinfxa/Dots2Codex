"""Known-ID Drive message tests without listing or network."""
import copy
from pathlib import Path
import sys
import unittest
HERE=Path(__file__).resolve()
REPO=next(p for p in HERE.parents if (p/'remote_transport'/'control.py').is_file())
sys.path.insert(0,str(REPO))
from remote_transport import deployment, Object, ProtocolError
from remote_transport.backend import GoogleDriveBackend, Capabilities, filename

class MetadataDrive:
    capabilities=Capabilities(complete_listing=False,create_by_id=False,direct_metadata_read=True)
    def __init__(self,obj):
        self.raw=obj.raw;self.calls=[]
        self.meta={'id':'file0','name':filename(obj),'parents':['folder0'],'trashed':False}
    def get_metadata(self,file_id):self.calls.append('metadata');return copy.deepcopy(self.meta)
    def get_bytes(self,file_id,limit):self.calls.append('media');return self.raw
    def list_page(self,*args):raise AssertionError('known-ID mode must never list')

class IndependentReferenceTests(unittest.TestCase):
    def setUp(self):
        self.obj=deployment('refs','synthetic/native/task')
        self.api=MetadataDrive(self.obj)
        self.backend=GoogleDriveBackend(self.api,'folder0',mode='duplicate_tolerant',discovery='control_refs')
        self.ref=self.backend.reference(self.obj,'file0')
    def test_known_id_fetch_checks_metadata_first_without_listing(self):
        self.assertEqual(self.backend.fetch(self.ref).oid,self.obj.oid)
        self.assertEqual(self.api.calls,['metadata','media'])
        with self.assertRaisesRegex(ProtocolError,'listing_disabled_for_control_refs'):
            self.backend.scan(self.obj.body['identity']['deployment_id'])
    def test_unapproved_locator_folder_rejected_before_api_calls(self):
        self.ref['locator']['folder_id']='other'
        with self.assertRaises(ProtocolError):self.backend.fetch(self.ref)
        self.assertEqual(self.api.calls,[])
    def test_actual_parent_scope_rejected_before_media(self):
        self.api.meta['parents']=['other']
        with self.assertRaises(ProtocolError):self.backend.fetch(self.ref)
        self.assertEqual(self.api.calls,['metadata'])
    def test_malformed_parent_string_is_not_a_folder_list(self):
        self.api.meta['parents']='prefix_folder0_suffix'
        with self.assertRaises(ProtocolError):self.backend.fetch(self.ref)
        self.assertEqual(self.api.calls,['metadata'])
    def test_returned_metadata_id_must_match_locator(self):
        self.api.meta['id']='wrong'
        with self.assertRaises(ProtocolError):self.backend.fetch(self.ref)
        self.assertEqual(self.api.calls,['metadata'])
    def test_exact_hash_and_filename_are_both_checked(self):
        self.ref['object_id']='0'*64
        with self.assertRaises(ProtocolError):self.backend.fetch(self.ref)
        self.ref=self.backend.reference(self.obj,'file0');self.api.meta['name']='renamed.json'
        with self.assertRaises(ProtocolError):self.backend.fetch(self.ref)
    def test_capability_missing_does_not_fall_back_to_listing(self):
        self.api.capabilities=Capabilities(complete_listing=True,create_by_id=False,direct_metadata_read=False)
        with self.assertRaisesRegex(ProtocolError,'direct_metadata_capability_required'):
            GoogleDriveBackend(self.api,'folder0',mode='duplicate_tolerant',discovery='control_refs')
    def test_locator_cannot_be_url_or_path(self):
        self.ref['locator']['file_id']='https://foreign.example/private'
        with self.assertRaises(ProtocolError):self.backend.fetch(self.ref)
        self.assertEqual(self.api.calls,[])

if __name__=='__main__':unittest.main(verbosity=2)
