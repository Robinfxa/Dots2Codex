#!/usr/bin/env python3
"""Optional operator-run setup aid. Never runs when the Router starts.

Commands: authorize (Mac browser + new credential file), check (read only),
create-folder (one explicitly approved new folder + durable local receipt).
No token logging, credential discovery, sharing changes, or inference.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import stat
import sys
import tempfile
import urllib.request

SCOPES = (
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
)
MAX_CONFIG_BYTES = 32768
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
AUTH_PROMPT = "请在自动打开的本机浏览器完成 Google 授权；不要复制授权或回调 URL。"
AUTH_SUCCESS = "浏览器授权步骤完成，请回到 Terminal 核对文件保存结果。"


class SetupError(Exception):
    pass


def require(ok: bool, code: str) -> None:
    if not ok:
        raise SetupError(code)


def absolute_path(value: str) -> Path:
    p = Path(value).expanduser()
    require(p.is_absolute(), "ABSOLUTE_PATH_REQUIRED")
    return p


def read_private_json(path: Path) -> dict:
    # Do not follow a final symlink, inspect browser caches, or find other tokens.
    private_parent(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and (info.st_mode & 0o077) == 0
                and info.st_size <= MAX_CONFIG_BYTES, "PRIVATE_FILE_REQUIRED")
        with os.fdopen(fd, "rb", closefd=False) as f:
            raw = f.read(MAX_CONFIG_BYTES + 1)
        require(len(raw) <= MAX_CONFIG_BYTES, "CONFIG_TOO_LARGE")
        value = json.loads(raw)
        require(isinstance(value, dict), "CONFIG_OBJECT_REQUIRED")
        return value
    finally:
        os.close(fd)


def private_parent(path: Path) -> None:
    info = path.parent.stat()
    require(not path.parent.is_symlink() and stat.S_ISDIR(info.st_mode)
            and info.st_uid == os.getuid() and (info.st_mode & 0o077) == 0,
            "EXISTING_0700_PARENT_REQUIRED")


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path: Path, value: dict, *, replace: bool = False) -> None:
    private_parent(path)
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, tmp_name = tempfile.mkstemp(prefix=".dots-setup-", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        if replace:
            # Only replace the receipt reserved by this invocation, not credentials.
            read_private_json(path)
            os.replace(tmp, path)
        else:
            os.link(tmp, path, follow_symlinks=False)  # fail rather than overwrite
        fsync_dir(path.parent)
    finally:
        if tmp.exists():
            tmp.unlink()


def validate_credential_info(value: dict) -> dict:
    require(isinstance(value, dict), "CONFIG_OBJECT_REQUIRED")
    require(value.get("type", "authorized_user") == "authorized_user", "AUTHORIZED_USER_REQUIRED")
    require(all(isinstance(value.get(k), str) and value[k]
                for k in ("client_id", "client_secret", "refresh_token")), "CREDENTIAL_FIELDS_MISSING")
    require(value.get("token_uri", "https://oauth2.googleapis.com/token") ==
            "https://oauth2.googleapis.com/token", "UNEXPECTED_TOKEN_ENDPOINT")
    require(value.get("universe_domain", "googleapis.com") == "googleapis.com", "UNEXPECTED_GOOGLE_UNIVERSE")
    recorded = value.get("scopes", [])
    require(isinstance(recorded, list) and all(isinstance(x, str) for x in recorded)
            and set(SCOPES) <= set(recorded), "REAUTHORIZE_REQUIRED_SCOPES")
    return value


def credential_info(path: Path) -> dict:
    return validate_credential_info(read_private_json(path))


def desktop_client_info(value: dict) -> dict:
    # The SDK gives "web" precedence. Reject dual configs and pass only the
    # validated Desktop block, never the caller's whole configuration.
    require("web" not in value, "DESKTOP_CLIENT_REQUIRED")
    installed = value.get("installed")
    require(isinstance(installed, dict), "DESKTOP_CLIENT_REQUIRED")
    require(installed.get("auth_uri") == AUTH_ENDPOINT
            and installed.get("token_uri") == TOKEN_ENDPOINT,
            "UNEXPECTED_OAUTH_ENDPOINTS")
    require(all(isinstance(installed.get(k), str) and installed[k].strip()
                for k in ("client_id", "client_secret")), "CLIENT_FIELDS_MISSING")
    return {"installed": {k: installed[k] for k in
            ("client_id", "client_secret", "auth_uri", "token_uri")}}


@contextmanager
def quiet_sdk_logs():
    # SDK DEBUG/INFO output can contain authorization/callback URLs and tokens.
    # Suppress it only for the explicit operation, preserving caller settings.
    previous = logging.root.manager.disable
    logging.disable(max(previous, logging.CRITICAL))
    try:
        yield
    finally:
        logging.disable(previous)


def credentials(path: Path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    value = credential_info(path)
    creds = Credentials.from_authorized_user_info(value)
    if not creds.valid:
        creds.refresh(Request())
    return creds


def sdk_service(name: str, version: str, creds):
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    transport = httplib2.Http(timeout=20)
    transport.follow_redirects = False
    return build(name, version, http=AuthorizedHttp(creds, http=transport, max_refresh_attempts=0),
                 cache_discovery=False, static_discovery=True, num_retries=0)


def authorize(args) -> dict:
    require(args.ack_drive_readonly, "EXPLICIT_BROAD_READONLY_APPROVAL_REQUIRED")
    require(sys.stdin.isatty(), "MAC_INTERACTIVE_TERMINAL_REQUIRED")
    source, output = absolute_path(args.client), absolute_path(args.output)
    private_parent(output)
    require(not output.exists() and not output.is_symlink(), "OUTPUT_EXISTS_USE_NEW_FILENAME")
    config = desktop_client_info(read_private_json(source))
    from google_auth_oauthlib.flow import InstalledAppFlow
    with quiet_sdk_logs():
        flow = InstalledAppFlow.from_client_config(
            config, scopes=list(SCOPES), autogenerate_code_verifier=True)
        creds = flow.run_local_server(
            host="127.0.0.1", port=0, access_type="offline", prompt="consent",
            authorization_prompt_message=AUTH_PROMPT,
            success_message=AUTH_SUCCESS, open_browser=True, timeout_seconds=300,
        )
    require(bool(creds.refresh_token), "REFRESH_TOKEN_MISSING")
    granted = getattr(creds, "granted_scopes", None)
    if granted is not None:
        require(set(SCOPES) <= set(granted), "REQUESTED_SCOPES_NOT_GRANTED")
    value = validate_credential_info(json.loads(creds.to_json()))
    write_json(output, value)
    return {"status": "OAUTH_FILE_SAVED", "refresh_token_present": True,
            "file_mode": "0600", "prior_credentials_overwritten": False,
            "next": "Run check on the exact approved folder; local scope metadata is not an API access test."}


def check(args) -> dict:
    path = absolute_path(args.credentials)
    value = credential_info(path)
    out = {"credential_file_private": True, "refresh_token_present": bool(value.get("refresh_token")),
           "scope_record_contains_required": True, "cloud_access_tested": False}
    if args.folder_id:
        drive = sdk_service("drive", "v3", credentials(path))
        m = drive.files().get(fileId=args.folder_id, supportsAllDrives=True,
                             fields="id,mimeType,trashed").execute(num_retries=0)
        require(m.get("id") == args.folder_id and m.get("trashed") is False and
                m.get("mimeType") == "application/vnd.google-apps.folder", "FOLDER_NOT_VERIFIED")
        out.update(cloud_access_tested=True, exact_folder_metadata_readable=True,
                   reverse_connector_probe_tested=False)
    return out


def create_folder_once(creds, name: str) -> dict:
    """One raw POST: no SDK socket retry, auth replay or redirect following."""
    token = creds.token
    require(isinstance(token, str) and token and "\r" not in token and "\n" not in token,
            "ACCESS_TOKEN_MISSING_OR_INVALID")
    body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    request = urllib.request.Request(
        "https://www.googleapis.com/drive/v3/files?fields=id%2Cname%2CmimeType",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    with quiet_sdk_logs(), opener.open(request, timeout=20) as response:
        raw = response.read(MAX_CONFIG_BYTES + 1)
    require(len(raw) <= MAX_CONFIG_BYTES, "FOLDER_RESPONSE_TOO_LARGE")
    value = json.loads(raw)
    require(isinstance(value, dict) and isinstance(value.get("id"), str)
            and bool(value["id"]) and value.get("name") == name
            and value.get("mimeType") == "application/vnd.google-apps.folder",
            "FOLDER_CREATE_UNVERIFIED")
    return value


def create_folder(args) -> dict:
    require(args.confirm_create, "EXPLICIT_FOLDER_CREATE_APPROVAL_REQUIRED")
    path, receipt = absolute_path(args.credentials), absolute_path(args.receipt)
    private_parent(receipt)
    require(not receipt.exists() and not receipt.is_symlink(), "RECEIPT_EXISTS_PRESERVE_AND_RECONCILE")
    require(isinstance(args.name, str) and bool(args.name.strip()), "FOLDER_NAME_REQUIRED")
    with quiet_sdk_logs():
        creds = credentials(path)
    state = {"status": "CREATE_INTENT", "name": args.name, "folder_id": None}
    write_json(receipt, state)
    try:
        value = create_folder_once(creds, args.name)
        state.update(status="CREATED", folder_id=value["id"])
        write_json(receipt, state, replace=True)
    except Exception:
        state["status"] = "OUTCOME_UNKNOWN"
        try:
            write_json(receipt, state, replace=True)
        except Exception:
            pass
        raise SetupError("FOLDER_OUTCOME_UNKNOWN_PRESERVE_RECEIPT_DO_NOT_RETRY") from None
    return {"status": "FOLDER_CREATED", "folder_id": state["folder_id"],
            "sharing_changed": False, "receipt_saved": True,
            "next": "Use this exact folder ID in INSTALL_ROUTER; do not rerun create-folder."}


def main() -> dict:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    p = sub.add_parser("authorize")
    p.add_argument("--client", required=True); p.add_argument("--output", required=True)
    p.add_argument("--ack-drive-readonly", action="store_true")
    p = sub.add_parser("check")
    p.add_argument("--credentials", required=True); p.add_argument("--folder-id")
    p = sub.add_parser("create-folder")
    p.add_argument("--credentials", required=True); p.add_argument("--receipt", required=True)
    p.add_argument("--name", default="Dots2Codex Transport"); p.add_argument("--confirm-create", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    return {"authorize": authorize, "check": check, "create-folder": create_folder}[args.operation](args)


def cli() -> int:
    try:
        with quiet_sdk_logs():
            result = main()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        # Raw SDK errors can contain URLs, account information, or remote data.
        print(json.dumps({"error": str(exc) if isinstance(exc, SetupError) else type(exc).__name__,
                          "action": "Stop and inspect locally. Do not paste tokens or full OAuth URLs."}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
