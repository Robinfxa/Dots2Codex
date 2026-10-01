"""Optional Google API factories. Explicit pre-authorized user file; no login flow.

Offline-tested integration examples, not live-certified standalone clients.
The operator must approve and provision this endpoint's own credential file.
Never point this at connector, browser, another app's, or another host's secrets.
"""
import json
import os
from pathlib import Path
from remote_transport.backend import read_private_file
from remote_transport.drive_http import DriveHTTPClient
from remote_transport.model import ProtocolError, require


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


class DocsSDKClient:
    """Docs port using official SDK requests and unchanged required-revision fencing.

    Application/SDK status retries and authorization replay are disabled, but
    httplib2 can resend an identical request after a socket failure. The same
    requiredRevisionId fences that resend; a lost reply stays unknown.
    All errors are conservatively unknown. This example does not guess whether
    an HTTP 400 was a proven stale revision. Reconciliation is an operator step.
    """
    def __init__(self, service):
        self.service = service

    def get_document(self, document_id):
        try:
            return self.service.documents().get(documentId=document_id,
                includeTabsContent=True, suggestionsViewMode="SUGGESTIONS_INLINE").execute(num_retries=0)
        except Exception:
            raise ProtocolError("docs_read_failed") from None

    def batch_update_document(self, document_id, requests, write_control):
        require(isinstance(write_control, dict) and set(write_control) == {"requiredRevisionId"}
                and isinstance(write_control["requiredRevisionId"], str)
                and write_control["requiredRevisionId"], "exact_required_revision_needed")
        try:
            return self.service.documents().batchUpdate(documentId=document_id,
                body={"requests": requests, "writeControl": write_control}).execute(num_retries=0)
        except Exception:
            # No remote error text, document content, credential data, or URL in logs.
            raise ProtocolError("docs_write_outcome_unknown") from None


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
