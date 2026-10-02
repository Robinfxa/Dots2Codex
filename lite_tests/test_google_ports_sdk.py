"""Real Google SDK request construction, offline transport, no credentials."""
import json
import unittest
import urllib.parse
try:
    import httplib2
    from googleapiclient.discovery import build
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False
from dots_lite.google_ports import DocsSDKClient
from dots_lite import docs
from dots_lite.protocol import make_inbox
from lite_tests.gateway_fixtures import FakeGoogle, grant, KEY


class RecordedHTTP:
    def __init__(self, value):
        self.value=value;self.calls=[];self.timeout=20;self.connections={}
    def request(self, uri, method='GET', body=None, headers=None, **kwargs):
        self.calls.append({'uri':uri,'method':method,'body':body,'headers':headers,'kwargs':kwargs})
        return httplib2.Response({'status':'200','content-type':'application/json'}), json.dumps(self.value).encode()


@unittest.skipUnless(SDK_AVAILABLE, 'run this module with the pinned Google SDK interpreter')
class RealSDKConstruction(unittest.TestCase):
    def make(self, value):
        transport=RecordedHTTP(value)
        service=build('docs','v1',http=transport,static_discovery=True,cache_discovery=False,num_retries=0)
        return DocsSDKClient(service,read_retries=0),transport
    def test_real_sdk_get_builds_exact_inline_tabs_request(self):
        fixture=FakeGoogle().get_document('inbox-test')
        port,transport=self.make(fixture)
        value=port.get_document('inbox-test')
        self.assertEqual(docs.snapshot(value,'inbox-test')['text'],'\n')
        call=transport.calls[0];self.assertEqual(call['method'],'GET')
        parsed=urllib.parse.urlsplit(call['uri'])
        self.assertEqual(parsed.scheme,'https');self.assertEqual(parsed.netloc,'docs.googleapis.com')
        self.assertEqual(parsed.path,'/v1/documents/inbox-test')
        query=urllib.parse.parse_qs(parsed.query)
        self.assertEqual(query['includeTabsContent'],['true'])
        self.assertEqual(query['suggestionsViewMode'],['SUGGESTIONS_INLINE'])
    def test_real_sdk_required_revision_write_receipt_no_extra_get(self):
        provider=FakeGoogle();pinned=grant()
        first=make_inbox(pinned,[],KEY,'first')
        blank=docs.snapshot(provider.get_document('inbox-test'),'inbox-test')
        initial=docs.plan_write(blank,first,'first')
        ack=provider.batch_update_document(initial['document_id'],initial['body']['requests'],initial['body']['writeControl'])
        known=docs.accept_write(initial,ack)['snapshot']
        second=make_inbox(pinned,[],KEY,'second')
        plan=docs.plan_write(known,second,'second')
        response={'documentId':'inbox-test','replies':[{},{}],
                  'writeControl':{'requiredRevisionId':'actual-provider-rev3','targetRevisionId':None}}
        port,transport=self.make(response)
        actual=port.batch_update_document(plan['document_id'],plan['body']['requests'],plan['body']['writeControl'])
        self.assertEqual(docs.accept_write(plan,actual)['status'],'accepted')
        self.assertEqual(len(transport.calls),1)
        call=transport.calls[0];self.assertEqual(call['method'],'POST')
        self.assertEqual(json.loads(call['body']),plan['body'])
        self.assertEqual(urllib.parse.urlsplit(call['uri']).path,'/v1/documents/inbox-test:batchUpdate')

if __name__=='__main__':unittest.main()
