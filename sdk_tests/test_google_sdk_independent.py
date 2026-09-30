"""Offline audit with the actual optional Google SDK packages and synthetic data."""
import datetime
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
from examples import google_clients
from remote_transport import ProtocolError


class SDKIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'authorized-user.json'
        self.info={'type':'authorized_user','client_id':'synthetic-client-id',
                   'client_secret':'synthetic-only','refresh_token':'synthetic-only',
                   'token':'synthetic-access-token','expiry':'2099-01-01T00:00:00Z',
                   'scopes':['https://www.googleapis.com/auth/drive.file']}
        self.path.write_text(json.dumps(self.info));self.path.chmod(0o600)
        self.env=patch.dict(os.environ,{'DOTS_GOOGLE_AUTHORIZED_USER_FILE':str(self.path)})
        self.env.start();self.addCleanup(self.env.stop)
        self.net=patch('socket.socket',side_effect=AssertionError('network forbidden by offline audit'))
        self.net.start();self.addCleanup(self.net.stop)

    def test_real_sdk_static_factory_has_no_network_or_credential_write(self):
        before=self.path.read_bytes()
        client=google_clients.create_docs_client()
        self.assertFalse(client.service._http.http.follow_redirects)
        self.assertEqual(client.service._http._max_refresh_attempts,0)
        self.assertEqual(client.service._http.credentials.scopes,self.info['scopes'])
        self.assertEqual(self.path.read_bytes(),before)

    def test_real_sdk_request_preserves_exact_cas_and_raw_shape(self):
        import httplib2
        client=google_clients.create_docs_client();captured=[]
        def request(uri,method='GET',body=None,headers=None,**kwargs):
            captured.append((uri,method,body,headers,kwargs))
            return httplib2.Response({'status':'200'}),b'{"documentId":"synthetic-document","replies":[{"replaceAllText":{"occurrencesChanged":1}}],"writeControl":{"requiredRevisionId":"new-revision"}}'
        with patch.object(client.service._http.http,'request',side_effect=request):
            requests=[{'replaceAllText':{'containsText':{'text':'old','matchCase':True,'searchByRegex':False},'replaceText':'new','tabsCriteria':{'tabIds':['tab']}}}]
            out=client.batch_update_document('synthetic-document',requests,{'requiredRevisionId':'old-revision'})
        self.assertEqual(len(captured),1)
        uri,method,body,headers,kwargs=captured[0]
        self.assertEqual(method,'POST');self.assertTrue(uri.startswith('https://docs.googleapis.com/v1/documents/synthetic-document:batchUpdate'))
        self.assertEqual(json.loads(body),{'requests':requests,'writeControl':{'requiredRevisionId':'old-revision'}})
        self.assertEqual(out['writeControl']['requiredRevisionId'],'new-revision')

    def test_underlying_lost_reply_retries_same_cas_then_is_unknown(self):
        import httplib2
        client=google_clients.create_docs_client();sent=[]
        class Response(http.client.HTTPResponse):
            status=400;reason='Bad Request';version=11
            def __init__(self):pass
            def getheaders(self):return [('content-type','application/json')]
            def read(self):return b'{"error":{"code":400,"message":"private-stale-revision"}}'
        class Connection:
            sock=object();host='docs.googleapis.com'
            def connect(self):self.sock=object()
            def close(self):self.sock=None
            def request(self,method,uri,body,headers):sent.append((method,uri,body))
            def getresponse(self):
                if len(sent)==1:raise http.client.BadStatusLine('lost first reply')
                return Response()
        conn=Connection();transport=client.service._http.http
        def route(uri,method='GET',body=None,headers=None,**kwargs):
            return transport._conn_request(conn,uri,method,body,headers)
        with patch.object(transport,'request',side_effect=route):
            with self.assertRaisesRegex(ProtocolError,'^docs_write_outcome_unknown$'):
                client.batch_update_document('synthetic-document',[{'replaceAllText':{'synthetic':'request'}}],{'requiredRevisionId':'old-revision'})
        self.assertEqual(len(sent),2)
        self.assertEqual(sent[0],sent[1])


if __name__=='__main__':unittest.main(verbosity=2)
