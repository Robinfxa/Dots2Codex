"""Read-only shared Router preflight. No discovery outside explicit candidate paths.

A valid recorded scope is not a live grant or cross-app access proof. This module
never opens OAuth, copies tokens, creates folders, or changes sharing.
"""
from __future__ import annotations
import contextlib
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit
from .backend import read_private_file
from .model import require

SCOPES = frozenset({'https://www.googleapis.com/auth/drive.file',
                    'https://www.googleapis.com/auth/drive.readonly'})
FOLDER_MIME = 'application/vnd.google-apps.folder'


def absolute_path(value):
    return Path(os.path.abspath(os.path.expanduser(str(value))))


def no_symlinks(path):
    path = absolute_path(path)
    for node in [path, *path.parents]:
        require(not node.is_symlink(), 'launcher_symlink_path_refused')
    return path


def private_directory(path, *, create=False):
    path = no_symlinks(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = path.stat()
    require(stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077,
            'launcher_private_directory_required')
    return path


def credential_candidates(config=None, *, environ=None, home=None):
    env = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    candidates = [(config or {}).get('authorized_user_file'), env.get('DOTS_GOOGLE_AUTHORIZED_USER_FILE'),
                  str(home / '.config/dots2codex/authorized-user-bidir.json')]
    result = []
    for value in candidates:
        if not value: continue
        path = absolute_path(value)
        # Do not open credential contents while merely discovering paths.
        if (path.exists() or path.is_symlink()) and path not in result: result.append(path)
    return result


def validate_credential(path):
    path = no_symlinks(path)
    try: value = json.loads(read_private_file(path, 32768))
    except Exception: raise ValueError('launcher_credential_unreadable_or_unsafe') from None
    require(isinstance(value, dict) and value.get('type', 'authorized_user') == 'authorized_user',
            'authorized_user_credentials_required')
    require(all(isinstance(value.get(k), str) and value[k] for k in
                ('client_id', 'client_secret', 'refresh_token')), 'authorized_user_credentials_incomplete')
    require(value.get('token_uri', 'https://oauth2.googleapis.com/token') == 'https://oauth2.googleapis.com/token',
            'unexpected_token_endpoint')
    require(value.get('universe_domain', 'googleapis.com') == 'googleapis.com', 'unexpected_google_universe')
    scopes = value.get('scopes', [])
    if isinstance(scopes, str): scopes = scopes.split()
    require(isinstance(scopes, list) and all(isinstance(x, str) for x in scopes) and SCOPES <= set(scopes),
            'router_requires_drive_file_and_drive_readonly_scopes')
    # Never return the secret-bearing object to UI/state/log consumers.
    return {'path': str(path), 'recorded_scopes_valid': True}


def folder_id(value):
    value = str(value).strip()
    if value.startswith('https://'):
        url = urlsplit(value)
        require(url.scheme == 'https' and url.netloc == 'drive.google.com' and not url.fragment,
                'launcher_invalid_folder_url')
        match = re.fullmatch(r'/drive/(?:u/[0-9]+/)?folders/([A-Za-z0-9_-]{1,256})/?', url.path)
        require(match is not None and not url.query, 'launcher_invalid_folder_url')
        value = match.group(1)
    require(re.fullmatch(r'[A-Za-z0-9_-]{1,256}', value) is not None, 'invalid_folder_id')
    return value


def validate_folder_metadata(metadata, expected):
    require(isinstance(metadata, dict) and metadata.get('id') == expected and
            metadata.get('trashed') is False and metadata.get('mimeType') == FOLDER_MIME,
            'router_folder_access_or_type_not_verified')
    return {'folder_verified': True, 'bidirectional_access_verified': False}


@contextlib.contextmanager
def credential_environment(path):
    key = 'DOTS_GOOGLE_AUTHORIZED_USER_FILE'; prior = os.environ.get(key)
    os.environ[key] = str(path)
    try: yield
    finally:
        if prior is None: os.environ.pop(key, None)
        else: os.environ[key] = prior


def validate_local(config):
    from .router_mac import _validate_config
    _validate_config(config)
    folder_id(config['folder_id'])
    validate_credential(config['authorized_user_file'])
    workdir = no_symlinks(config['workdir'])
    require(workdir.is_dir(), 'launcher_existing_workspace_required')
    require(os.access(workdir, os.R_OK | os.W_OK | os.X_OK), 'launcher_workspace_not_accessible')
    return {'local_preflight': True}


def validate_google(config, *, drive_factory=None):
    """Caller must confirm credential reuse before invoking this network read."""
    validate_local(config)
    if drive_factory is None:
        from examples.google_clients import create_drive_client
        drive_factory = create_drive_client
    with credential_environment(config['authorized_user_file']):
        return validate_folder_metadata(drive_factory().get_metadata(config['folder_id']), config['folder_id'])
