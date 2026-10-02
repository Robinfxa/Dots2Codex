"""Optional raw Drive REST port. Caller supplies separately authorized token provider.

No credential file/env/browser inspection, OAuth provisioning, or network calls at
import. Do not pass native connector secrets. This adapter is not live-certified.
"""
import contextlib
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from .backend import AlreadyExists, Capabilities
from .model import ProtocolError, require
from .global_response import current_response_deadline


class DriveHTTPClient:
    capabilities = Capabilities(complete_listing=True, create_by_id=True, direct_metadata_read=True)

    def __init__(self, access_token_provider, *, timeout=20):
        require(callable(access_token_provider), 'explicit_token_provider_required')
        require(type(timeout) in (int, float) and 1 <= timeout <= 30, 'invalid_timeout')
        self._token_provider, self.timeout = access_token_provider, timeout

    def _call(self, path, *, query=None, data=None, content_type=None, limit=262144):
        budget=current_response_deadline()
        if budget is not None:budget.remaining("remote_wait_budget_expired")
        # Token kept only in this authorized request. Never persist/log it.
        token = self._token_provider()
        if budget is not None:budget.remaining("remote_wait_budget_expired")
        require(isinstance(token, str) and token and '\r' not in token and '\n' not in token,
                'invalid_access_token')
        base = 'https://www.googleapis.com/'
        url = base + path + ('?' + urllib.parse.urlencode(query) if query else '')
        request = urllib.request.Request(url, data=data, headers={'Authorization': 'Bearer ' + token})
        if content_type:
            request.add_header('Content-Type', content_type)
        # Explicitly refuse redirects so credentials cannot follow another origin.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        # A guard captures the exact TLS socket. urllib's read timeout alone
        # cannot stop an indefinitely trickling header/body within one call.
        guards=contextlib.ExitStack()
        class BoundedConnection(http.client.HTTPSConnection):
            def connect(inner):
                inner.timeout=min(inner.timeout,budget.remaining('remote_wait_budget_expired'))
                super().connect();inner.auto_open=0
                guards.enter_context(budget.watch_socket(inner.sock))
                inner.sock.settimeout(budget.remaining('remote_wait_budget_expired'))
            def request(inner,*args,**kwargs):
                inner.timeout=min(inner.timeout,budget.remaining('remote_wait_budget_expired'))
                if inner.sock is not None:inner.sock.settimeout(inner.timeout)
                super().request(*args,**kwargs)
                budget.remaining('remote_wait_budget_expired')
            def getresponse(inner):
                inner.sock.settimeout(min(inner.timeout,budget.remaining('remote_wait_budget_expired')))
                result=super().getresponse();budget.remaining('remote_wait_budget_expired')
                return result
        class BoundedHTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(inner,request):
                return inner.do_open(BoundedConnection,request,context=inner._context)
        opener = urllib.request.build_opener(NoRedirect,BoundedHTTPSHandler()) if budget is not None else urllib.request.build_opener(NoRedirect)
        try:
            with guards, opener.open(request, timeout=(min(self.timeout,budget.remaining('remote_wait_budget_expired'))
                    if budget is not None else self.timeout)) as response:
                if budget is not None:budget.remaining('remote_wait_budget_expired')
                raw = response.read(limit + 1)
                if budget is not None:budget.remaining('remote_wait_budget_expired')
                require(len(raw) <= limit, 'drive_response_too_large')
                return raw
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                raise AlreadyExists() from None
            raise ProtocolError('drive_http_' + str(exc.code)) from None
        except (OSError, urllib.error.URLError):
            raise ProtocolError('drive_network_outcome_unknown') from None

    def create_document_once(self, folder, name):
        """Single POST, no SDK/auth/status replay; lost response remains unknown."""
        body = {'name': name, 'parents': [folder], 'mimeType': 'application/vnd.google-apps.document'}
        result = json.loads(self._call('drive/v3/files', query={'fields': 'id,name,parents',
            'supportsAllDrives': 'true'}, data=json.dumps(body).encode(), content_type='application/json'))
        require(isinstance(result, dict) and isinstance(result.get('id'), str) and result['id'] and
                result.get('name') == name and result.get('parents') == [folder], 'drive_document_create_unverified')
        return result['id']

    def generate_id(self):
        data = json.loads(self._call('drive/v3/files/generateIds', query={'count': 1, 'space': 'drive', 'type': 'files'}))
        require(type(data.get('ids')) is list and len(data['ids']) == 1, 'invalid_generated_ids')
        return data['ids'][0]

    def create_bytes(self, folder, name, raw, file_id):
        metadata = {'name': name, 'parents': [folder], 'mimeType': 'application/json'}
        if file_id is not None:
            metadata['id'] = file_id
        boundary = 'dots' + uuid.uuid4().hex
        data = ('--' + boundary + '\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n').encode()
        data += json.dumps(metadata).encode() + ('\r\n--' + boundary + '\r\nContent-Type: application/json\r\n\r\n').encode()
        data += raw + ('\r\n--' + boundary + '--\r\n').encode()
        out = json.loads(self._call('upload/drive/v3/files', query={'uploadType': 'multipart',
            'fields': 'id', 'supportsAllDrives': 'true'}, data=data,
            content_type='multipart/related; boundary=' + boundary))
        return out.get('id')

    def get_bytes(self, file_id, limit):
        return self._call('drive/v3/files/' + urllib.parse.quote(file_id, safe=''),
                          query={'alt': 'media', 'supportsAllDrives': 'true'}, limit=limit)

    def list_page(self, folder, token, size):
        query = {'q': "'" + folder + "' in parents and trashed = false", 'pageSize': size,
                 'spaces': 'drive', 'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true',
                 'fields': 'files(id,name,parents,trashed),nextPageToken,incompleteSearch'}
        if token is not None:
            query['pageToken'] = token
        return json.loads(self._call('drive/v3/files', query=query))

    def get_metadata(self,file_id):
        return json.loads(self._call('drive/v3/files/'+urllib.parse.quote(file_id,safe=''),
            query={'fields':'id,name,mimeType,parents,trashed','supportsAllDrives':'true'}))
