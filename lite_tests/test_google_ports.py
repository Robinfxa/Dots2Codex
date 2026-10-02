import json
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from dots_lite.google_ports import DocsSDKClient, DriveHTTPClient
from dots_lite.protocol import ProtocolError

class FakeSDK:
    def __init__(self): self.active=0; self.peak=0; self.calls=[]; self.fail=False
    def documents(self):return self
    def get(self,**kwargs):self.calls.append(('get',kwargs));return self
    def batchUpdate(self,**kwargs):self.calls.append(('write',kwargs));return self
    def execute(self,**kwargs):
        self.calls.append(('execute',kwargs));self.active+=1;self.peak=max(self.peak,self.active)
        try:
            time.sleep(.005)
            if self.fail:raise RuntimeError('SECRET MUST NOT APPEAR')
            return {'documentId':'doc','replies':[{},{}],'writeControl':{'requiredRevisionId':'new','targetRevisionId':None}}
        finally:self.active-=1

class GooglePortsTests(unittest.TestCase):
    def test_sdk_serial_no_automatic_write_retry(self):
        sdk=FakeSDK();port=DocsSDKClient(sdk)
        threads=[threading.Thread(target=port.get_document,args=('doc',)) for _ in range(3)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(sdk.peak,1)
        self.assertTrue(all(c[1]=={'num_retries':0} for c in sdk.calls if c[0]=='execute'))
        sdk.fail=True; before=len(sdk.calls)
        with self.assertRaisesRegex(ProtocolError,'docs_write_outcome_unknown') as caught:
            port.batch_update_document('doc',[{},{}],{'requiredRevisionId':'old'})
        self.assertNotIn('SECRET',str(caught.exception));self.assertEqual(len(sdk.calls)-before,2)
    def test_sdk_get_shape_and_required_revision(self):
        sdk=FakeSDK();port=DocsSDKClient(sdk)
        port.get_document('doc')
        self.assertEqual(sdk.calls[0],('get',{'documentId':'doc','includeTabsContent':True,'suggestionsViewMode':'SUGGESTIONS_INLINE'}))
        with self.assertRaisesRegex(ProtocolError,'exact_required_revision'):
            port.batch_update_document('doc',[],{'targetRevisionId':'old'})
    def test_raw_drive_create_has_one_post_and_no_redirect_follow(self):
        connections=[]
        class Connection:
            def __init__(self,host,timeout):connections.append(self);self.host=host;self.sock=None;self.requests=[];self.status=302
            def connect(self):pass
            def request(self,*args,**kwargs):self.requests.append((args,kwargs))
            def getresponse(self):return self
            def read(self,limit):return b'{"id":"file"}'
            def close(self):pass
        with mock.patch('dots_lite.google_ports.http.client.HTTPSConnection',Connection):
            port=DriveHTTPClient(lambda:'authorized-test-token')
            with self.assertRaisesRegex(ProtocolError,'drive_http_302'):port.create_bytes('folder','name',b'{}')
        self.assertEqual(len(connections),1);self.assertEqual(len(connections[0].requests),1)
        self.assertEqual(connections[0].host,'www.googleapis.com')
        self.assertEqual(connections[0].requests[0][0][0],'POST')
        self.assertEqual(connections[0].auto_open,0)
    def test_import_graph_excludes_legacy(self):
        program="import sys; import dots_lite.gateway,dots_lite.google_ports; assert not any(n=='remote_transport' or n.startswith('remote_transport.') for n in sys.modules)"
        subprocess.run([sys.executable,'-c',program],check=True,cwd=Path(__file__).resolve().parents[1])

if __name__=='__main__':unittest.main()
