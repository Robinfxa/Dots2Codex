import io
import json
import urllib.error
import urllib.parse
import unittest
from unittest.mock import patch
from remote_transport.drive_http import DriveHTTPClient
from remote_transport.backend import AlreadyExists
from remote_transport.model import ProtocolError


class Response(io.BytesIO):
    pass


class Opener:
    def __init__(self):
        self.calls=[];self.body=b'{}';self.error=None
    def open(self, request, timeout):
        self.calls.append((request,timeout))
        if self.error:raise self.error
        return Response(self.body)


class HTTPContractTests(unittest.TestCase):
    def setUp(self):
        self.opener=Opener()
        self.patch=patch('urllib.request.build_opener',return_value=self.opener)
        self.patch.start()
        self.client=DriveHTTPClient(lambda:'synthetic-test-token')
    def tearDown(self):self.patch.stop()

    def test_generated_ids_request(self):
        self.opener.body=b'{"ids":["preallocated"]}'
        self.assertEqual(self.client.generate_id(),'preallocated')
        req,timeout=self.opener.calls[0]
        parsed=urllib.parse.urlparse(req.full_url)
        self.assertEqual(parsed.netloc,'www.googleapis.com')
        self.assertEqual(parsed.path,'/drive/v3/files/generateIds')
        self.assertEqual(req.get_header('Authorization'),'Bearer synthetic-test-token')
        self.assertEqual(timeout,20)

    def test_raw_upload_includes_persisted_id(self):
        self.opener.body=b'{"id":"preallocated"}'
        self.assertEqual(self.client.create_bytes('folder','object.json',b'raw','preallocated'),'preallocated')
        req,_=self.opener.calls[0]
        self.assertIn(b'"id": "preallocated"',req.data)
        self.assertIn(b'"parents": ["folder"]',req.data)
        self.assertIn(b'Content-Type: application/json',req.data)
        self.assertEqual(req.get_method(),'POST')

    def test_pagination_fields_and_token(self):
        self.client.list_page('folder','token',100)
        req,_=self.opener.calls[0]
        q=urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
        self.assertEqual(q['pageToken'],['token'])
        self.assertEqual(q['q'],["'folder' in parents and trashed = false"])
        self.assertIn('incompleteSearch',q['fields'][0])
        self.assertIn('parents',q['fields'][0])

    def test_409_is_not_success(self):
        self.opener.error=urllib.error.HTTPError('https://www.googleapis.com',409,'conflict',{},None)
        with self.assertRaises(AlreadyExists):self.client.create_bytes('folder','name',b'raw','id')

    def test_429_and_redirect_are_not_retried(self):
        for code in [429,302,403]:
            self.opener.error=urllib.error.HTTPError('https://www.googleapis.com',code,'synthetic',{},None)
            with self.assertRaisesRegex(ProtocolError,'drive_http_'+str(code)):
                self.client.get_bytes('id',20)
        self.assertEqual(len(self.opener.calls),3)

    def test_response_size_bound(self):
        self.opener.body=b'0123456789'
        with self.assertRaisesRegex(ProtocolError,'drive_response_too_large'):
            self.client.get_bytes('id',3)

    def test_token_provider_explicit_no_calls_on_init(self):
        self.assertEqual(self.opener.calls,[])
        with self.assertRaisesRegex(ProtocolError,'explicit_token_provider_required'):
            DriveHTTPClient(None)
