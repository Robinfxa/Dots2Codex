"""Optional Google API factories. Explicit pre-authorized user file; no login flow.

Offline-tested integration examples, not live-certified standalone clients.
The operator must approve and provision this endpoint's own credential file.
Never point this at connector, browser, another app's, or another host's secrets.
"""
import errno
from contextlib import contextmanager
import copy
import http.client
import json
import math
import os
from pathlib import Path
import socket
import ssl
import sys
import threading
import time
from remote_transport.backend import read_private_file
from remote_transport.drive_http import DriveHTTPClient
from remote_transport.model import ProtocolError, require
from remote_transport.global_response import current_response_deadline


def _credential_info():
    path = os.environ.get("DOTS_GOOGLE_AUTHORIZED_USER_FILE")
    require(path is not None and Path(path).is_absolute(), "explicit_absolute_authorized_user_file_required")
    try:
        info = json.loads(read_private_file(path, 32768))
    except Exception:
        raise ProtocolError("authorized_user_file_unreadable") from None
    require(isinstance(info, dict), "invalid_authorized_user_file")
    require(info.get("type", "authorized_user") == "authorized_user", "authorized_user_credentials_required")
    require(all(isinstance(info.get(k), str) and info[k] for k in
                ("client_id", "client_secret", "refresh_token")), "authorized_user_credentials_incomplete")
    # Do not allow credential-file endpoint overrides or external credential types.
    require(info.get("token_uri", "https://oauth2.googleapis.com/token") ==
            "https://oauth2.googleapis.com/token", "unexpected_token_endpoint")
    require(info.get("universe_domain", "googleapis.com") == "googleapis.com", "unexpected_google_universe")
    return info


def _credentials():
    info = _credential_info()
    from google.oauth2.credentials import Credentials
    try:
        # No new scopes: preserve only the existing, operator-approved grant.
        return Credentials.from_authorized_user_info(info)
    except Exception:
        raise ProtocolError("authorized_user_credentials_invalid") from None



def authorized_scopes():
    """Recorded existing grant only; never requests new OAuth scopes."""
    value = _credential_info().get("scopes", [])
    if isinstance(value, str): value = value.split()
    require(type(value) is list and all(isinstance(x, str) and x for x in value),
            "authorized_user_scopes_missing")
    return frozenset(value)


def authorized_credentials():
    """Explicit endpoint credentials only; no login/discovery."""
    return _credentials()


def create_drive_sdk_service():
    """Read-compatible official SDK factory; Router writes use raw no-replay port.

    httplib2 may resend a socket-failed POST. Do not use this factory for Router
    non-idempotent document creation; use DriveHTTPClient.create_document_once.
    """
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    try:
        http = httplib2.Http(timeout=20); http.follow_redirects = False
        authorized = AuthorizedHttp(_credentials(), http=http, max_refresh_attempts=0)
        return build("drive", "v3", http=authorized, cache_discovery=False,
                     static_discovery=True, num_retries=0)
    except Exception:
        raise ProtocolError("drive_sdk_initialization_failed") from None

def create_drive_client():
    credentials = _credentials()
    from google.auth.transport.requests import Request
    def token():
        try:
            if not credentials.valid:
                credentials.refresh(Request())
            require(isinstance(credentials.token, str) and credentials.token, "missing_google_access_token")
            return credentials.token
        except Exception:
            raise ProtocolError("google_authorization_refresh_failed") from None
    return DriveHTTPClient(token, timeout=20)


READ_RETRY_DELAYS = (1.0, 2.0)


class DocsReadError(ProtocolError):
    """Sanitized read-only failure; retryability never authorizes a write replay."""
    def __init__(self, *, category, retryable, attempts, http_status=None):
        super().__init__("docs_read_transient_exhausted" if retryable else "docs_read_failed")
        self.retryable = retryable
        self.diagnostics = {"category": category, "attempts": attempts}
        if http_status is not None:
            self.diagnostics["http_status"] = http_status


def _optional_exception(exc, module, names):
    # Exceptions from an optional SDK have already loaded their defining module.
    # Do not import Google dependencies merely to classify an unrelated failure.
    namespace = sys.modules.get(module)
    return any(isinstance(cls, type) and isinstance(exc, cls)
               for cls in (getattr(namespace, name, None) for name in names))


def _safe_exception_class(exc):
    """Fixed labels only: custom exception names can themselves contain secrets."""
    known = (
        ("builtins", ("Exception", "RuntimeError", "ValueError", "TypeError", "OSError",
                      "TimeoutError", "ConnectionError", "ConnectionResetError",
                      "ConnectionAbortedError", "ConnectionRefusedError", "BrokenPipeError",
                      "PermissionError", "FileNotFoundError")),
        ("http.client", ("HTTPException", "ResponseNotReady", "CannotSendRequest",
                         "CannotSendHeader", "BadStatusLine", "RemoteDisconnected", "IncompleteRead")),
        ("ssl", ("SSLError", "SSLCertVerificationError", "SSLEOFError", "SSLZeroReturnError")),
        ("socket", ("gaierror", "herror")),
        ("json.decoder", ("JSONDecodeError",)),
        ("remote_transport.model", ("ProtocolError",)),
        ("httplib2", ("HttpLib2Error", "ServerNotFoundError")),
        ("google.auth.exceptions", ("TransportError", "RefreshError", "DefaultCredentialsError")),
        ("googleapiclient.errors", ("HttpError",)),
        ("requests.exceptions", ("ConnectionError", "Timeout", "ConnectTimeout", "ReadTimeout", "SSLError")),
    )
    for module, names in known:
        namespace = sys.modules.get(module)
        for name in names:
            if type(exc) is getattr(namespace, name, None):
                return module + "." + name
    return "unrecognized"


def _exception_chain(exc):
    """Inspect a bounded causal chain without formatting provider error text."""
    pending, seen, chain = [exc], {id(exc)}, []
    while pending:
        current = pending.pop()
        chain.append(current)
        for value in (
            current.__cause__, current.__context__, getattr(current, "reason", None),
            *current.args):
            if not isinstance(value, BaseException) or id(value) in seen:
                continue
            if len(seen) >= 8:
                # An unexamined cause could be an auth/security failure.
                return None
            seen.add(id(value)); pending.append(value)
    return chain


def _read_failure(exc, attempts):
    chain = _exception_chain(exc)
    def failure(*, category, retryable, http_status=None):
        result = DocsReadError(category=category, retryable=retryable,
                               attempts=attempts, http_status=http_status)
        result.diagnostics["exception_class"] = _safe_exception_class(exc)
        result.diagnostics["exception_chain"] = [
            _safe_exception_class(value) for value in (chain or [exc])]
        if chain is None:
            result.diagnostics["exception_chain_truncated"] = True
        return result
    if chain is None:
        return failure(category="unexpected", retryable=False)
    statuses = []
    for current in chain:
        # Security/validation/auth failures stay terminal, even inside a transport
        # wrapper. Never infer retryability from error messages, bodies, or URLs.
        if isinstance(current, ProtocolError):
            return failure(category="protocol", retryable=False)
        if isinstance(current, ssl.SSLError) or _optional_exception(
                current, "requests.exceptions", ("SSLError",)):
            return failure(category="tls", retryable=False)
        if _optional_exception(current, "google.auth.exceptions",
                               ("RefreshError", "DefaultCredentialsError")):
            return failure(category="authorization", retryable=False)
        status = getattr(getattr(current, "resp", None), "status", None)
        # HttpError uses resp.status. Only a real HTTP-range integer is retained.
        if type(status) is int and 100 <= status <= 599:
            statuses.append(status)
    for status in statuses:
        if status not in (408, 429) and not 500 <= status <= 599:
            category = "authorization" if status in (401, 403) else "http_permanent"
            return failure(category=category, retryable=False, http_status=status)
    if statuses:
        status = statuses[0]
        category = "rate_limited" if status == 429 else "http_transient"
        return failure(category=category, retryable=True, http_status=status)
    for current in chain:
        if isinstance(current, TimeoutError):
            return failure(category="timeout", retryable=True)
        if (isinstance(current, (ConnectionError, http.client.IncompleteRead))
                or isinstance(current, OSError) and current.errno in {
                    errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNREFUSED,
                    errno.ECONNABORTED, errno.ENETDOWN, errno.ENETRESET,
                    errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EPIPE}
                or isinstance(current, socket.gaierror) and current.errno == socket.EAI_AGAIN
                or _optional_exception(current, "httplib2", ("ServerNotFoundError",))
                or _optional_exception(current, "google.auth.exceptions", ("TransportError",))
                or _optional_exception(current, "requests.exceptions", ("ConnectionError", "Timeout"))):
            return failure(category="network", retryable=True)
    return failure(category="unexpected", retryable=False)


def _check_budget(deadline, check, operation="read"):
    budget=current_response_deadline()
    if budget is not None:budget.remaining("remote_wait_budget_expired")
    # Caller liveness/stop/integrity checks are not provider errors and must not
    # be wrapped into retryable transport failures.
    if check is not None:
        check()
    if deadline is not None:
        require(time.monotonic() < deadline, "docs_" + operation + "_deadline_exceeded")


@contextmanager
def _response_transport(service):
    """Temporarily clamp this SDK transport while its existing lock is held."""
    budget=current_response_deadline()
    if budget is None:
        yield
        return
    remaining=budget.remaining('remote_wait_budget_expired')
    transports=[];current=getattr(service,'_http',None);seen=set()
    while current is not None and id(current) not in seen:
        seen.add(id(current));transports.append(current);current=getattr(current,'http',None)
    changed=[];sockets=[];known_connections={}
    try:
        for transport in transports:
            known_connections[id(transport)]={id(value) for value in getattr(transport,'connections',{}).values()}
            if hasattr(transport,'timeout'):
                original=transport.timeout
                timeout=min(original,remaining) if isinstance(original,(int,float)) and original>0 else remaining
                transport.timeout=timeout;changed.append((transport,original))
            for connection in getattr(transport,'connections',{}).values():
                sock=getattr(connection,'sock',None)
                if sock is not None:
                    original=sock.gettimeout();sock.settimeout(min(original,remaining) if original else remaining)
                    sockets.append((sock,original))
        class ActiveSockets:
            def shutdown(self,how):
                for transport in transports:
                    for connection in list(getattr(transport,'connections',{}).values()):
                        sock=getattr(connection,'sock',None)
                        if sock is not None:
                            try:sock.shutdown(how)
                            except OSError:pass
        with budget.watch_socket(ActiveSockets()):
            yield
    finally:
        for transport,original in changed:
            transport.timeout=original
            # Connections opened while clamped must not retain that request's
            # shorter timeout for later read-only recovery or another route.
            for connection in getattr(transport,'connections',{}).values():
                if id(connection) in known_connections[id(transport)]:continue
                if hasattr(connection,'timeout'):connection.timeout=original
                sock=getattr(connection,'sock',None)
                if sock is not None:
                    try:sock.settimeout(original)
                    except OSError:pass
        for sock,original in sockets:
            try:sock.settimeout(original)
            except OSError:pass


class DocsSDKClient:
    """Docs port using official SDK requests and unchanged required-revision fencing.

    Reads get at most three application attempts, with 1s/2s backoff, only for
    classified transient failures. SDK status retries and authorization replay
    remain disabled. Writes get no application retries, but
    httplib2 can resend an identical request after a socket failure. The same
    requiredRevisionId fences that resend; a lost reply stays unknown.
    All write errors are conservatively unknown. This example does not guess whether
    an HTTP 400 was a proven stale revision. Reconciliation is an operator step.
    """
    def __init__(self, service):
        self.service = service
        # Supervisor and facade threads share this client. The SDK's httplib2
        # connection cache and AuthorizedHttp credential refresh are not safe to
        # run concurrently. Include request construction and refresh in the same
        # gate as execution; never retain it across read retry backoff.
        self._transport_lock = threading.RLock()

    @contextmanager
    def _transport(self, deadline, check, operation="read"):
        budget=current_response_deadline()
        if budget is not None:budget.remaining("remote_wait_budget_expired")
        _check_budget(deadline, check, operation)
        if deadline is None and check is None:
            self._transport_lock.acquire()
        else:
            while True:
                delay = .1 if deadline is None else min(.1, max(0, deadline - time.monotonic()))
                if self._transport_lock.acquire(timeout=delay):
                    break
                _check_budget(deadline, check, operation)
        try:
            # A read can expire or be cancelled while waiting behind another
            # facade. Keep this check outside the provider-error catch so it
            # cannot become a retryable failure or permit a stale dispatch.
            _check_budget(deadline, check, operation)
            with _response_transport(self.service):yield
        finally:
            self._transport_lock.release()

    def get_document(self, document_id, *, deadline=None, check=None):
        """Retry only this GET, rejecting results after an optional caller budget.

        deadline is an absolute monotonic deadline. A provider call already in
        flight cannot be cancelled here; its late result is never returned.
        """
        require(deadline is None or type(deadline) in (int, float) and math.isfinite(deadline),
                "invalid_docs_read_deadline")
        require(check is None or callable(check), "invalid_docs_read_check")
        budget=current_response_deadline()
        if budget is not None:
            budget.remaining("remote_wait_budget_expired")
            deadline=min(deadline,budget.until) if deadline is not None else budget.until
        for attempt in range(1, len(READ_RETRY_DELAYS) + 2):
            with self._transport(deadline, check):
                try:
                    result = self.service.documents().get(documentId=document_id,
                        includeTabsContent=True, suggestionsViewMode="SUGGESTIONS_INLINE").execute(num_retries=0)
                except Exception as exc:
                    failure = _read_failure(exc, attempt)
                else:
                    failure = None
            if failure is None:
                _check_budget(deadline, check)
                return result
            _check_budget(deadline, check)
            if not failure.retryable or attempt > len(READ_RETRY_DELAYS):
                raise failure from None
            delay = READ_RETRY_DELAYS[attempt - 1]
            if deadline is not None:
                delay = min(delay, max(0, deadline - time.monotonic()))
            time.sleep(delay)

    def batch_update_document(self, document_id, requests, write_control, *, deadline=None, check=None):
        # Keep the exact queued request/revision even if its caller later edits
        # a mutable plan. Copy before the transport wait and validate that copy.
        if isinstance(write_control, dict): write_control = dict(write_control)
        require(isinstance(write_control, dict) and set(write_control) == {"requiredRevisionId"}
                and isinstance(write_control["requiredRevisionId"], str)
                and write_control["requiredRevisionId"], "exact_required_revision_needed")
        require(deadline is None or type(deadline) in (int, float) and math.isfinite(deadline),
                "invalid_docs_write_deadline")
        require(check is None or callable(check), "invalid_docs_write_check")
        requests = copy.deepcopy(requests)
        # A failed dispatch guard proves this call never entered the SDK. Do not
        # turn cancellation/expiry into an unknown provider outcome here.
        with self._transport(deadline, check, "write"):
            try:
                return self.service.documents().batchUpdate(documentId=document_id,
                    body={"requests": requests, "writeControl": write_control}).execute(num_retries=0)
            except Exception:
                # No remote error text, document content, credential data, or URL in logs.
                raise ProtocolError("docs_write_outcome_unknown") from None

    def batch_update_document_guarded(self, document_id, requests, write_control, *, deadline=None, check=None):
        """Explicit optional port capability; checks also run after lock waits."""
        return self.batch_update_document(document_id, requests, write_control, deadline=deadline, check=check)


def create_docs_client():
    credentials = _credentials()
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    try:
        http = httplib2.Http(timeout=20)
        http.follow_redirects = False
        authorized = AuthorizedHttp(credentials, http=http, max_refresh_attempts=0)
        service = build("docs", "v1", http=authorized, cache_discovery=False,
                        static_discovery=True, num_retries=0)
    except Exception:
        raise ProtocolError("docs_client_initialization_failed") from None
    return DocsSDKClient(service)
