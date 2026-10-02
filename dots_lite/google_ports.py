"""Explicitly authorized Google ports, independent of the legacy runtime.

No login or network at import; no credential discovery or scope enlargement.
Drive mutations are single raw HTTPS sends with redirects and replay disabled.
Docs SDK calls serialize request construction, credential refresh and transport.
"""
from __future__ import annotations
from contextlib import contextmanager
import copy
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import stat
import threading
import time
import urllib.parse
from .protocol import ProtocolError, require
from .wire import strict_json


def _remaining(deadline, timeout=20):
    remaining = timeout if deadline is None else min(timeout, deadline - time.monotonic())
    require(remaining > 0, 'transport_wait_budget_expired')
    return remaining


def _credential_info():
    name = os.environ.get('DOTS_GOOGLE_AUTHORIZED_USER_FILE')
    require(isinstance(name, str) and Path(name).is_absolute(), 'explicit_absolute_authorized_user_file_required')
    path = Path(name)
    require(not any(p.is_symlink() for p in [path, *path.parents]), 'credential_symlink_rejected')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as f:
            info = os.fstat(f.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
                    'private_authorized_user_file_required')
            raw = f.read(32769)
        require(len(raw) <= 32768, 'authorized_user_file_too_large')
        value = strict_json(raw)
    except ProtocolError:
        raise
    except Exception:
        raise ProtocolError('authorized_user_file_unreadable') from None
    require(isinstance(value, dict) and value.get('type', 'authorized_user') == 'authorized_user', 'authorized_user_credentials_required')
    require(all(isinstance(value.get(k), str) and value[k] for k in ('client_id', 'client_secret', 'refresh_token')),
            'authorized_user_credentials_incomplete')
    require(value.get('token_uri', 'https://oauth2.googleapis.com/token') == 'https://oauth2.googleapis.com/token',
            'unexpected_token_endpoint')
    require(value.get('universe_domain', 'googleapis.com') == 'googleapis.com', 'unexpected_google_universe')
    return value


def authorized_credentials():
    from google.oauth2.credentials import Credentials
    info = _credential_info()
    try:
        return Credentials.from_authorized_user_info(info)
    except Exception:
        raise ProtocolError('authorized_user_credentials_invalid') from None


def authorized_scopes():
    value = _credential_info().get('scopes', [])
    if isinstance(value, str): value = value.split()
    require(isinstance(value, list) and all(isinstance(s, str) for s in value), 'invalid_authorized_scopes')
    return frozenset(value)


class ReadFailure(ProtocolError):
    def __init__(self, code, *, retryable=False, status=None):
        super().__init__(code)
        self.retryable, self.http_status = retryable, status


def _read_failure(exc):
    # Only numeric provider status and known exception types, never error prose.
    status = getattr(getattr(exc, 'resp', None), 'status', None)
    if type(status) is int:
        return ReadFailure('google_read_http_failure', retryable=status in (408, 429) or 500 <= status <= 599, status=status)
    return ReadFailure('google_read_transport_failure', retryable=isinstance(exc, (TimeoutError, ConnectionError)) and
                       not isinstance(exc, ssl.SSLError))


class DocsSDKClient:
    def __init__(self, service, *, read_retries=2):
        require(type(read_retries) is int and 0 <= read_retries <= 2, 'invalid_read_retry_limit')
        self.service, self.read_retries = service, read_retries
        self._lock = threading.RLock()

    @contextmanager
    def _transport(self, deadline=None, check=None):
        while not self._lock.acquire(timeout=min(.1, _remaining(deadline))):
            if check: check()
        changed, timer = [], None
        try:
            if check: check()
            remaining = _remaining(deadline)
            current, seen, transports = getattr(self.service, '_http', None), set(), []
            while current is not None and id(current) not in seen:
                seen.add(id(current)); transports.append(current)
                if hasattr(current, 'timeout'):
                    changed.append((current, current.timeout)); current.timeout = remaining
                current = getattr(current, 'http', None)
            def interrupt():
                for transport in transports:
                    for conn in list(getattr(transport, 'connections', {}).values()):
                        sock = getattr(conn, 'sock', None)
                        if sock is not None:
                            try: sock.shutdown(socket.SHUT_RDWR)
                            except OSError: pass
            if deadline is not None:
                timer = threading.Timer(_remaining(deadline), interrupt); timer.daemon = True; timer.start()
            yield
        finally:
            if timer: timer.cancel()
            for transport, original in changed:
                transport.timeout = original
                for conn in list(getattr(transport, 'connections', {}).values()):
                    if hasattr(conn, 'timeout'): conn.timeout = original
                    sock = getattr(conn, 'sock', None)
                    if sock is not None:
                        try: sock.settimeout(original)
                        except OSError: pass
            self._lock.release()

    def get_document(self, document_id, *, deadline=None, check=None):
        for attempt in range(self.read_retries + 1):
            with self._transport(deadline, check):
                try:
                    value = self.service.documents().get(documentId=document_id, includeTabsContent=True,
                        suggestionsViewMode='SUGGESTIONS_INLINE').execute(num_retries=0)
                except Exception as exc:
                    failure = _read_failure(exc)
                else:
                    _remaining(deadline)
                    return value
            if not failure.retryable or attempt == self.read_retries:
                raise failure from None
            time.sleep(min(2 ** attempt, _remaining(deadline)))

    def batch_update_document(self, document_id, requests, write_control, *, deadline=None, check=None):
        require(isinstance(write_control, dict) and set(write_control) == {'requiredRevisionId'} and
                isinstance(write_control['requiredRevisionId'], str) and write_control['requiredRevisionId'],
                'exact_required_revision_needed')
        requests, write_control = copy.deepcopy(requests), copy.deepcopy(write_control)
        with self._transport(deadline, check):
            try:
                return self.service.documents().batchUpdate(documentId=document_id,
                    body={'requests': requests, 'writeControl': write_control}).execute(num_retries=0)
            except Exception:
                raise ProtocolError('docs_write_outcome_unknown') from None


def _drive_ssl_context():
    # Project-local trust only; never replace or supplement an explicit override.
    try:
        cafile, capath = os.environ.get('SSL_CERT_FILE'), os.environ.get('SSL_CERT_DIR')
        if cafile is not None or capath is not None:
            if cafile == '' or capath == '':
                raise ValueError('empty_trust_override')
            return ssl.create_default_context(cafile=cafile, capath=capath)
        try:
            import certifi
        except ModuleNotFoundError as exc:
            if exc.name != 'certifi':
                raise
            # Keep standalone use possible without the optional Google dependencies.
            return ssl.create_default_context()
        bundle = certifi.where()
        if not isinstance(bundle, str) or not bundle:
            raise ValueError('invalid_certifi_bundle')
        return ssl.create_default_context(cafile=bundle)
    except Exception:
        raise ProtocolError('drive_transport_outcome_unknown') from None


class DriveHTTPClient:
    def __init__(self, access_token_provider, *, timeout=20, token_accepts_deadline=False):
        require(callable(access_token_provider), 'explicit_token_provider_required')
        require(type(timeout) in (int, float) and 1 <= timeout <= 30, 'invalid_transport_timeout')
        self._token_provider, self.timeout = access_token_provider, timeout
        self._token_accepts_deadline = token_accepts_deadline
        self._ssl_context = _drive_ssl_context()
        self._lock = threading.RLock()

    def _call(self, path, *, query=None, data=None, content_type=None, limit=262144, deadline=None):
        require(isinstance(path, str) and not path.startswith('/') and '://' not in path, 'invalid_google_path')
        with self._lock:
            _remaining(deadline, self.timeout)
            token = self._token_provider(deadline=deadline) if self._token_accepts_deadline else self._token_provider()
            require(isinstance(token, str) and token and '\r' not in token and '\n' not in token, 'invalid_access_token')
            url = '/' + path + ('?' + urllib.parse.urlencode(query) if query else '')
            conn = http.client.HTTPSConnection('www.googleapis.com', timeout=_remaining(deadline, self.timeout),
                                               context=self._ssl_context)
            timer = None
            try:
                conn.connect(); conn.auto_open = 0
                def interrupt():
                    if conn.sock:
                        try: conn.sock.shutdown(socket.SHUT_RDWR)
                        except OSError: pass
                if deadline is not None:
                    timer = threading.Timer(_remaining(deadline, self.timeout), interrupt); timer.daemon = True; timer.start()
                headers = {'Authorization': 'Bearer ' + token}
                if content_type: headers['Content-Type'] = content_type
                conn.request('POST' if data is not None else 'GET', url, body=data, headers=headers)
                response = conn.getresponse()
                require(200 <= response.status < 300, 'drive_http_' + str(response.status))
                raw = response.read(limit + 1)
                require(len(raw) <= limit, 'drive_response_too_large')
                # A successful write receipt remains valid after wait expiration.
                if data is None: _remaining(deadline, self.timeout)
                return raw
            except ProtocolError: raise
            except Exception:
                raise ProtocolError('drive_transport_outcome_unknown') from None
            finally:
                if timer: timer.cancel()
                conn.close()

    def create_document_once(self, folder, name, *, deadline=None):
        body = {'name': name, 'parents': [folder], 'mimeType': 'application/vnd.google-apps.document'}
        value = strict_json(self._call('drive/v3/files', query={'fields': 'id,name,parents', 'supportsAllDrives': 'true'},
            data=json.dumps(body).encode(), content_type='application/json', deadline=deadline))
        require(isinstance(value, dict) and isinstance(value.get('id'), str) and value['id'] and
                value.get('name') == name and value.get('parents') == [folder], 'document_creation_outcome_unknown')
        return value['id']

    def create_bytes(self, folder, name, raw, file_id=None, *, deadline=None):
        require(isinstance(raw, bytes), 'raw_bytes_required')
        metadata = {'name': name, 'parents': [folder], 'mimeType': 'application/json'}
        if file_id is not None: metadata['id'] = file_id
        boundary = 'dotslite' + secrets.token_hex(16)
        body = ('--' + boundary + '\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n').encode()
        body += json.dumps(metadata).encode() + ('\r\n--' + boundary + '\r\nContent-Type: application/json\r\n\r\n').encode()
        body += raw + ('\r\n--' + boundary + '--\r\n').encode()
        value = strict_json(self._call('upload/drive/v3/files', query={'uploadType': 'multipart', 'fields': 'id',
            'supportsAllDrives': 'true'}, data=body, content_type='multipart/related; boundary=' + boundary, deadline=deadline))
        require(isinstance(value, dict) and isinstance(value.get('id'), str) and value['id'], 'upload_outcome_unknown')
        return value['id']

    def get_metadata(self, file_id, *, deadline=None):
        return strict_json(self._call('drive/v3/files/' + urllib.parse.quote(file_id, safe=''), query={
            'fields': 'id,name,mimeType,parents,trashed,size', 'supportsAllDrives': 'true'}, deadline=deadline))

    def get_bytes(self, file_id, limit, *, deadline=None):
        return self._call('drive/v3/files/' + urllib.parse.quote(file_id, safe=''), query={'alt': 'media',
            'supportsAllDrives': 'true'}, limit=limit, deadline=deadline)


def create_docs_client():
    credentials = authorized_credentials()
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    try:
        http = httplib2.Http(timeout=20); http.follow_redirects = False
        authorized = AuthorizedHttp(credentials, http=http, max_refresh_attempts=0)
        return DocsSDKClient(build('docs', 'v1', http=authorized, cache_discovery=False,
                                   static_discovery=True, num_retries=0))
    except Exception:
        raise ProtocolError('docs_client_initialization_failed') from None


def create_drive_client():
    credentials = authorized_credentials()
    from google.auth.transport.requests import Request
    import requests
    class NoRedirectSession(requests.Session):
        def request(self, method, url, **kwargs):
            kwargs['allow_redirects'] = False
            kwargs['timeout'] = min(20, kwargs.get('timeout') or 20)
            return super().request(method, url, **kwargs)
    session = NoRedirectSession()
    def token(*, deadline=None):
        class BoundedRequest(Request):
            def __call__(self, *args, **kwargs):
                kwargs['timeout'] = min(kwargs.get('timeout') or 20, _remaining(deadline))
                return super().__call__(*args, **kwargs)
        try:
            if not credentials.valid: credentials.refresh(BoundedRequest(session=session))
            require(isinstance(credentials.token, str) and credentials.token, 'missing_google_access_token')
            return credentials.token
        except Exception:
            raise ProtocolError('google_authorization_refresh_failed') from None
    return DriveHTTPClient(token, token_accepts_deadline=True)
