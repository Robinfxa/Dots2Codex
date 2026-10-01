"""Optional raw Drive REST port. Caller supplies separately authorized token provider.

No credential file/env/browser inspection, OAuth provisioning, or network calls at
import. Do not pass native connector secrets. This adapter is not live-certified.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from .backend import AlreadyExists, Capabilities
from .model import ProtocolError, require


class DriveHTTPClient:
    capabilities = Capabilities(complete_listing=True, create_by_id=True, direct_metadata_read=True)

    def __init__(self, access_token_provider, *, timeout=20):
        require(callable(access_token_provider), 'explicit_token_provider_required')
        require(type(timeout) in (int, float) and 1 <= timeout <= 30, 'invalid_timeout')
        self._token_provider, self.timeout = access_token_provider, timeout

    def _call(self, path, *, query=None, data=None, content_type=None, limit=262144):
        # Token kept only in this authorized request. Never persist/log it.
        token = self._token_provider()
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
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(request, timeout=self.timeout) as response:
                raw = response.read(limit + 1)
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
