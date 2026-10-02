"""Explicit opt-in v3 Mac launcher. No legacy runtime enters the v3 hot path.

Filesystem/config/process primitives are extracted from the MIT-licensed
Dots2Codex frozen launcher. Status never imports Google or a legacy backend.
"""
from __future__ import annotations
import argparse
import contextlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from .protocol import (PROTOCOL, JOIN_MARKER, DEFAULT_LIMITS, ProtocolError, canonical,
                       hash_bytes, require, grant_hash, validate_grant)
from .private_io import (private_dir, private_write, private_lock, no_symlinks,
                         read_private_file, read, save, strict_json)
from .client_catalog import load_catalog, select, validate_selection, write_catalog, validate_catalog
from . import config_transaction as config_tx
from .package import package_identity
from .ui import UI, Cancelled
from .authorization import (CONTRACT as AUTHORIZATION_CONTRACT, authorization_scope,
                            TRANSPORT_PERMISSIONS)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = Path.home() / '.config/dots2codex-launcher/lightweight-v3'
DEFAULT_LEGACY_STATE = Path.home() / '.config/dots2codex-launcher'
DEFAULT_ROUTER_CONFIG = Path.home() / '.config/dots2codex/router.json'
DEFAULT_ROUTER_ACTIVE = Path.home() / '.config/dots2codex/router-active.json'
CONTRACT = 'dots-lite-launcher/3'
SCOPES = {'https://www.googleapis.com/auth/drive.file',
          'https://www.googleapis.com/auth/drive.readonly'}


def safe_error(exc):
    value = str(exc)
    return value if re.fullmatch('[a-z][a-z0-9_]{1,120}', value) else type(exc).__name__


def alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process_identity(pid):
    """Start-time plus command fingerprint; a numeric PID alone grants no signal."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart=', '-o', 'command='],
                                capture_output=True, text=True, timeout=5, check=False)
        if result.returncode or not result.stdout.strip():
            return None
        return hash_bytes(result.stdout.strip().encode())
    except Exception:
        return None


def owned(record):
    return bool(record.get('process_identity') and
                process_identity(record.get('pid')) == record['process_identity'])


def stop_owned(record, *, timeout=3):
    if not alive(record.get('pid')):
        return True
    require(owned(record), 'refuse_to_signal_unowned_process')
    for sig in (signal.SIGINT, signal.SIGTERM):
        require(owned(record), 'refuse_to_signal_unowned_process')
        try:
            os.kill(record['pid'], sig)
        except ProcessLookupError:
            return True
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if not alive(record['pid']):
                return True
            time.sleep(.1)
    return not alive(record['pid'])


def credential_check(path):
    path = no_symlinks(path)
    value = read(path, 32768)
    require(isinstance(value, dict) and value.get('type', 'authorized_user') == 'authorized_user'
            and all(isinstance(value.get(k), str) and value[k] for k in
                    ('client_id', 'client_secret', 'refresh_token')), 'existing_authorized_user_file_required')
    require(value.get('token_uri', 'https://oauth2.googleapis.com/token') == 'https://oauth2.googleapis.com/token'
            and value.get('universe_domain', 'googleapis.com') == 'googleapis.com', 'unexpected_google_auth_endpoint')
    scopes = value.get('scopes', [])
    if isinstance(scopes, str): scopes = scopes.split()
    require(isinstance(scopes, list) and all(isinstance(x, str) for x in scopes)
            and SCOPES <= set(scopes), 'existing_drive_scopes_required_no_oauth_expansion')
    info = path.stat()
    return {'path': str(path), 'identity': [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]}


def resolve_codex_home(explicit=None, saved=None, *, environ=None, home=None, ui=None):
    env = os.environ if environ is None else environ
    default = (Path.home() if home is None else Path(home)) / '.codex'
    if explicit:
        return config_tx.known_home(explicit)
    saved_path = None
    if saved:
        try: saved_path = config_tx.known_home(saved)
        except FileNotFoundError: pass
    env_path = None
    if env.get('CODEX_HOME'):
        require(Path(env['CODEX_HOME']).expanduser().is_absolute(), 'codex_home_environment_must_be_absolute')
        env_path = config_tx.known_home(env['CODEX_HOME'])
    if saved_path is not None and (env_path is None or saved_path == env_path):
        return saved_path
    candidates = []
    for value in (env_path, default):
        if value is None: continue
        try: path = config_tx.known_home(value)
        except FileNotFoundError: continue
        if path not in candidates: candidates.append(path)
    if saved_path is not None and saved_path not in candidates: candidates.insert(0, saved_path)
    require(candidates, 'codex_home_missing_start_codex_once')
    if len(candidates) == 1: return candidates[0]
    require(ui is not None, 'codex_home_conflict_requires_selection')
    choice = ui.choose('Choose the exact CODEX_HOME used by the client for this reversible trial',
                       [str(p) for p in candidates])
    return config_tx.known_home(choice)


def legacy_inspection(legacy_state, router_active, codex_home=None):
    """Read legacy evidence only; never resume/rewrite old roots or journals."""
    legacy_state, router_active = no_symlinks(legacy_state), no_symlinks(router_active)
    blockers, saved_home = [], None
    if router_active.exists():
        old = read(router_active)
        stopped = old.get('process_stopped') is True and (
            old.get('closed') is True or old.get('stage') == 'ABORTED')
        if alive(old.get('facade_pid')) or not stopped:
            blockers.append('legacy_single_session_not_verified_stopped')
    current = legacy_state / 'global/current.json'
    if current.exists():
        record = read(current)
        runtime = no_symlinks(record['runtime'])
        require(runtime.parent == legacy_state / 'global/runs', 'legacy_runtime_path_mismatch')
        spec = read(runtime / 'spec.json')
        saved_home = spec.get('codex_home')
        status = read(runtime / 'status.json') if (runtime / 'status.json').exists() else {}
        owned_file = runtime / 'supervisor.json'
        owner = read(owned_file) if owned_file.exists() else {}
        if alive(record.get('facade_pid')) or alive(owner.get('facade_pid')) or status.get('stage') != 'STOPPED':
            blockers.append('legacy_global_not_verified_stopped')
    runs = legacy_state / 'global/runs'
    if runs.exists():
        for path in runs.glob('*/gateway/config-transactions/*.json'):
            value = read(path)
            if value.get('phase') not in ('restored', 'aborted'):
                blockers.append('legacy_config_requires_restore_or_reconciliation')
                break
    if codex_home is not None:
        snap = config_tx.snapshot(config_tx.known_home(codex_home) / 'config.toml')
        if snap['exists']:
            import tomllib
            parsed = tomllib.loads(snap['raw'].decode())
            if parsed.get('model_provider') == 'dots2codex_global':
                blockers.append('legacy_provider_still_selected')
    return {'safe_to_activate': not blockers, 'blockers': blockers,
            'saved_codex_home': saved_home, 'native_children_stopped': False,
            'old_requests_migrated': False}


def join_text(grant, key):
    require(isinstance(key, str) and re.fullmatch('[0-9a-f]{64}', key), 'invalid_join_key')
    return JOIN_MARKER + ' ' + canonical({'activation_id': grant['activation_id'],
        'inbox_id': grant['inbox_id'], 'grant_sha256': grant_hash(grant), 'join_code': key,
        'transport_authorization': authorization_scope(grant)}).decode()


def health(base_url):
    parsed = urlsplit(base_url)
    require(parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.port
            and not parsed.query and not parsed.fragment, 'invalid_loopback_endpoint')
    connection = http.client.HTTPConnection('127.0.0.1', parsed.port, timeout=2)
    try:
        connection.request('GET', parsed.path.removesuffix('/v1') + '/health')
        response = connection.getresponse()
        raw = response.read(65537)
        require(response.status == 200 and len(raw) <= 65536, 'local_listener_not_ready')
        return json.loads(raw)
    finally:
        connection.close()


def retrieve_result(base_url, activation_id, request_id, token, *, max_bytes, timeout=30):
    """Only contact the recorded loopback owner. Never retry or POST a request."""
    parsed = urlsplit(base_url)
    require(parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.port
            and parsed.netloc == f'127.0.0.1:{parsed.port}'
            and parsed.path == f'/activations/{activation_id}/v1'
            and not parsed.query and not parsed.fragment, 'invalid_recovery_endpoint')
    require(isinstance(request_id, str) and re.fullmatch('[0-9a-f]{32}', request_id), 'invalid_local_request_id')
    require(isinstance(token, str) and re.fullmatch('[0-9a-f]{64}', token), 'invalid_recovery_token')
    connection = http.client.HTTPConnection('127.0.0.1', parsed.port, timeout=timeout)
    try:
        connection.request('GET', parsed.path + '/responses/' + request_id,
                           headers={'X-Dots-Recovery-Token': token})
        response = connection.getresponse()
        raw = response.read(max_bytes + 16385)
        require(len(raw) <= max_bytes + 16384, 'recovery_response_too_large')
        value = strict_json(raw)
        require(isinstance(value, dict), 'invalid_recovery_response')
        if response.status not in (200, 202):
            error = value.get('error')
            code = error.get('code') if isinstance(error, dict) else None
            require(isinstance(code, str) and re.fullmatch('[a-z][a-z0-9_]{1,120}', code),
                    'recovery_http_error')
            raise ProtocolError(code)
        return value
    finally:
        connection.close()


class Ports:
    def copy(self, text):
        require(sys.platform == 'darwin', 'clipboard_requires_macos')
        result = subprocess.run(['/usr/bin/pbcopy'], input=text.encode(), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=5, check=False)
        require(result.returncode == 0, 'clipboard_copy_failed')

    def spawn(self, root, runtime):
        env = {k:v for k,v in os.environ.items() if k not in {'PYTHONPATH', 'PYTHONHOME'}}
        env['PYTHONNOUSERSITE'] = '1'
        # Suppress third-party HTTP/OAuth tracebacks, tokens, user prompts and JOIN.
        with open(os.devnull, 'wb') as output:
            proc = subprocess.Popen([sys.executable, '-B', '-m', 'dots_lite.launcher', 'serve',
                '--runtime', str(runtime)], cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                stdout=output, stderr=output, start_new_session=True)
        return {'pid': proc.pid, 'process_identity': process_identity(proc.pid)}

    def health(self, base_url): return health(base_url)
    def recover(self, *args, **kwargs): return retrieve_result(*args, **kwargs)
    def package(self, root): return package_identity(root)
    def wait(self, seconds): time.sleep(seconds)
    def now(self): return time.time()


class Launcher:
    def __init__(self, *, root=ROOT, state=DEFAULT_STATE, legacy_state=DEFAULT_LEGACY_STATE,
                 router_config=DEFAULT_ROUTER_CONFIG, router_active=DEFAULT_ROUTER_ACTIVE,
                 ui=None, ports=None):
        self.root, self.state = no_symlinks(root), no_symlinks(state)
        self.legacy_state, self.router_config = no_symlinks(legacy_state), no_symlinks(router_config)
        self.router_active = no_symlinks(router_active)
        self.ui, self.ports = ui or UI(), ports or Ports()

    def active(self):
        path = self.state / 'current.json'
        if not path.exists(): return None
        value = read(path)
        require(value.get('contract') == CONTRACT, 'invalid_v3_launcher_state')
        runtime = no_symlinks(value['runtime'])
        require(runtime.parent == self.state / 'runs' and runtime.name == value['run_id'], 'v3_runtime_binding_mismatch')
        owner_path = runtime / 'supervisor.json'
        if owner_path.exists():
            owner = read(owner_path)
            require(owner['run_id'] == value['run_id'], 'v3_supervisor_identity_mismatch')
            if value.get('pid') in (None, owner.get('pid')) and owned(owner):
                value.update(pid=owner['pid'], process_identity=owner['process_identity'])
        return value

    def transactions(self, runtime):
        directory = Path(runtime) / 'config-transactions'
        if not directory.exists(): return []
        return [read(p) for p in directory.glob('*.json')]

    def status(self):
        active = self.active()
        if active is None:
            return {'protocol': PROTOCOL, 'stage': 'NOT_STARTED', 'config_changed': False}
        runtime = Path(active['runtime'])
        info = read(runtime / 'status.json') if (runtime / 'status.json').exists() else {'stage': 'STARTING'}
        # Status remains useful offline and without optional dependencies.
        txs = self.transactions(runtime)
        result = {**info, 'protocol': PROTOCOL, 'activation_id': active['run_id'],
                  'supervisor_alive': alive(active.get('pid')), 'supervisor_owned': owned(active),
                  'config_changed': any(t['phase'] not in ('restored', 'aborted') for t in txs),
                  'underlying_model_verified': False, 'client_compatibility_verified': False,
                  'native_children_stopped': False}
        if not result['supervisor_alive']:
            result['listener_ready'] = False
        if (runtime / 'stop.json').exists():
            result['stop_requested'] = True
            result['native_stop_notification_required'] = True
        return result

    def settings(self, args):
        original = read(self.router_config, 131072) if self.router_config.exists() else {}
        sidecar = read(self.state / 'settings.json') if (self.state / 'settings.json').exists() else {}
        require(not sidecar or sidecar.get('contract') == CONTRACT, 'invalid_v3_settings')
        before = legacy_inspection(self.legacy_state, self.router_active)
        pair = sidecar.get('selection', original.get('model_selection'))
        model, effort = getattr(args, 'model', None), getattr(args, 'effort', None)
        require((model is None) == (effort is None), 'model_and_effort_required_together')
        if model:
            pair = select(load_catalog(), model, effort)
        elif pair is not None:
            pair = validate_selection(pair)  # No coercion of retired max or catalog.
        else:
            model = self.ui.choose('Native model for this v3 activation', load_catalog()['models'])
            effort = self.ui.choose('Exact native effort', load_catalog()['models'][model]['bridge_efforts'], default='xhigh')
            pair = select(load_catalog(), model, effort)
        credential = getattr(args, 'credentials', None) or sidecar.get('authorized_user_file') or original.get('authorized_user_file')
        if credential is None: credential = self.ui.file('Select your existing authorized-user Google credential file')
        credential = no_symlinks(credential)
        folder = getattr(args, 'folder_id', None) or sidecar.get('folder_id') or original.get('folder_id')
        if folder is None: folder = self.ui.text('Dedicated existing Google Drive folder ID')
        require(isinstance(folder, str) and re.fullmatch('[A-Za-z0-9_-]{1,256}', folder), 'invalid_folder_id')
        saved_home = sidecar.get('codex_home') or before.get('saved_codex_home') or original.get('codex_home')
        home = resolve_codex_home(getattr(args, 'codex_home', None), saved_home, ui=self.ui)
        snapshot = config_tx.snapshot(home / 'config.toml')
        if snapshot['exists']:
            import tomllib
            require(tomllib.loads(snapshot['raw'].decode()).get('model_provider') != config_tx.PROVIDER,
                    'selected_v3_provider_requires_recorded_restore')
        inspected = legacy_inspection(self.legacy_state, self.router_active, home)
        require(inspected['safe_to_activate'], (inspected['blockers'] or ['legacy_state_unverified'])[0])
        return {'contract': CONTRACT, 'protocol': PROTOCOL, 'folder_id': folder,
                'authorized_user_file': str(credential), 'codex_home': str(home),
                'selection': pair, 'limits': dict(DEFAULT_LIMITS), 'seconds': 14400,
                'wait_seconds': 900, 'port': 0,
                'source_router_sha256': hash_bytes(read_private_file(self.router_config, 131072)) if original else None}

    def _ready(self, active):
        require(owned(active) and alive(active.get('pid')), 'v3_supervisor_not_owned_or_running')
        runtime = Path(active['runtime'])
        require(not (runtime / 'stop.json').exists(), 'v3_stop_requested')
        grant = validate_grant(read(runtime / 'grant.json'))
        require(grant['activation_id'] == active['run_id'] and
                grant['created_at'] <= self.ports.now() < grant['expires_at'],
                'grant_expired_or_clock_unproven')
        info = read(runtime / 'config-info.json')
        require(info['generation'] == active['run_id'], 'v3_activation_mismatch')
        validate_catalog(info['catalog_path'], info['selection'])
        observed = self.ports.health(info['base_url'])
        require(observed.get('activation_id') == active['run_id']
                and observed.get('protocol') == PROTOCOL and observed.get('listener_ready') is True
                and observed.get('join_configured') is True and observed.get('stopped') is False,
                'local_listener_activation_mismatch')
        return info

    def start(self, args):
        prior = self.active()
        original_stop_epoch = read(self.state / 'stop-intent.json') if (self.state / 'stop-intent.json').exists() else None
        if prior is not None:
            status = self.status()
            require(not status['supervisor_alive'] and status.get('stage') == 'STOPPED'
                    and not status['config_changed'], 'v3_stop_and_restore_before_fresh_activation')
        config_tx.parser()  # Fail before Google mutation if TOML support is missing.
        settings = self.settings(args)
        package_hash = self.ports.package(self.root)
        message = ('Start the explicitly selected lightweight v3 first-use trial?\n'
            f"Google folder: {settings['folder_id']}\nCODEX_HOME: {settings['codex_home']}\n"
            f"Model/effort: {settings['selection']['model']} / {settings['selection']['reasoning_effort']}\n"
            f"Reviewed package SHA-256: {package_hash}\n"
            f"Reuse existing credential file: {settings['authorized_user_file']}\n"
            'Uses the existing drive.file and drive.readonly authorization; read access can extend beyond this folder. '
            'No new OAuth or scopes. The local launcher creates/updates one fresh Inbox Doc and uploads actual request files. '
            + TRANSPORT_PERMISSIONS +
            'The separately copied JOIN will state the exact activation, Inbox, folder, grant/package hashes, expiry and limits. '
            'Up to 4 hours, 3 independent project routes/children, 128 requests per route, 1 MiB each input/output. '
            'No synthetic probe. Expiry prevents new BEGIN; already-begun work may publish its result. '
            'Old roots and unresolved requests are preserved, never replayed. Native tasks may still require an explicit stop in dot. '
            'A separate exact-patch approval is required before changing your client config.')
        if not self.ui.confirm(message): raise Cancelled()
        credential = credential_check(settings['authorized_user_file'])
        private_dir(self.state, create=True)
        with private_lock(self.state / 'lifecycle.lock'):
            latest = self.active()
            require((latest or {}).get('run_id') == (prior or {}).get('run_id'), 'v3_activation_changed')
            stop_epoch = read(self.state / 'stop-intent.json') if (self.state / 'stop-intent.json').exists() else None
            require(stop_epoch == original_stop_epoch, 'v3_start_cancelled_by_stop')
            run_id = secrets.token_hex(16)
            runtime = private_dir(self.state / 'runs' / run_id, create=True)
            settings = {**settings, 'credential_evidence': credential}
            spec = {'contract': CONTRACT, 'run_id': run_id, 'runtime': str(runtime),
                    'created_at': int(self.ports.now()), 'package_sha256': package_hash,
                    'transport_authorization_contract': AUTHORIZATION_CONTRACT,
                    'settings': settings, 'stop_epoch': stop_epoch}
            save(runtime / 'spec.json', spec)
            private_write(runtime / 'join-key', secrets.token_hex(32).encode())
            save(self.state / 'settings.json', settings)
            record = {'contract': CONTRACT, 'run_id': run_id, 'runtime': str(runtime),
                      'pid': None, 'process_identity': None}
            save(self.state / 'current.json', record)  # Durable process launch intent.
            record.update(self.ports.spawn(self.root, runtime))
            save(self.state / 'current.json', record)
        until = time.monotonic() + 45
        while time.monotonic() < until:
            status = self.status()
            if status.get('listener_ready') or status.get('stage') in ('FAILED', 'STOPPED'): break
            if not status['supervisor_alive']: break
            self.ports.wait(.2)
        status = self.status()
        if status.get('listener_ready'):
            self.copy_join()
            return self.apply_config()
        return status

    def resume(self):
        active = self.active()
        require(active is not None, 'v3_not_started')
        runtime = Path(active['runtime'])
        require(not (runtime / 'stop.json').exists(), 'stopped_activation_cannot_resume')
        require(not alive(active.get('pid')), 'v3_supervisor_still_running')
        require((runtime / 'supervisor.json').exists() and (runtime / 'grant.json').exists()
                and (runtime / 'gateway/mac').is_dir(), 'activation_incomplete_requires_explicit_recovery')
        previous = read(runtime / 'status.json') if (runtime / 'status.json').exists() else {}
        require(previous.get('stage') in {'LOCAL_READY', 'FAILED'}, 'process_launch_outcome_unknown_no_automatic_retry')
        spec = read(runtime / 'spec.json')
        require(self.ports.package(self.root) == spec['package_sha256'], 'lightweight_package_changed_since_start')
        if not self.ui.confirm('Resume only this same stopped local process and its existing v3 activation?\n'
            'The original expiry, grant, JOIN, model/effort and request fences remain unchanged. '
            'No new native child or inference is authorized. Unresolved remote writes are only reconciled. '
            'The same local port must still be available.'):
            raise Cancelled()
        with private_lock(self.state / 'lifecycle.lock'):
            current = self.active()
            require(current is not None and current['run_id'] == active['run_id']
                    and not alive(current.get('pid')) and not (runtime / 'stop.json').exists(),
                    'v3_resume_raced_or_stopped')
            owner = read(runtime / 'supervisor.json')
            require(not alive(owner.get('pid')), 'v3_supervisor_still_running')
            current.update(pid=None, process_identity=None)
            save(self.state / 'current.json', current)
            save(runtime / 'status.json', {'protocol': PROTOCOL, 'stage': 'STARTING',
                'listener_ready': False, 'native_children_stopped': False})
            current.update(self.ports.spawn(self.root, runtime))
            save(self.state / 'current.json', current)
        return {**self.status(), 'resume_requested': True, 'fresh_join_created': False}

    def recover(self, request_id, *, print_result=False):
        """Read one existing result via the living owner, including after expiry.

        This process never opens MacGateway or its journal and cannot replay a
        request, emit a tool, create a child, or restore an old client connection.
        """
        require(isinstance(request_id, str) and re.fullmatch('[0-9a-f]{32}', request_id), 'invalid_local_request_id')
        require(type(print_result) is bool, 'invalid_recovery_print_option')
        active = self.active()
        require(active is not None, 'v3_not_started')
        require(owned(active) and alive(active.get('pid')),
                'recovery_requires_running_owner_resume_same_activation_with_matching_package')
        runtime = private_dir(active['runtime'])
        grant = validate_grant(read(runtime / 'grant.json'))
        info = read(runtime / 'config-info.json')
        require(grant['activation_id'] == active['run_id'] == info.get('generation')
                and info.get('protocol') == PROTOCOL, 'v3_activation_mismatch')
        selected = validate_selection(info['selection'])
        require({'model': selected['model'], 'reasoning_effort': selected['reasoning_effort']}
                in grant['allowed_pairs'], 'recovery_selection_mismatch')
        observed = self.ports.health(info['base_url'])
        require(observed.get('activation_id') == active['run_id']
                and observed.get('protocol') == PROTOCOL and observed.get('listener_ready') is True,
                'local_listener_activation_mismatch')
        from .gateway import recovery_token
        token = recovery_token(read_private_file(runtime / 'join-key', 64).decode(), active['run_id'], request_id)
        # Expiry blocks new admission, not retrieval of an already begun result.
        value = self.ports.recover(info['base_url'], active['run_id'], request_id, token,
                                  max_bytes=grant['limits']['max_result_bytes'])
        require(owned(active) and alive(active.get('pid')), 'recovery_owner_changed')
        require(isinstance(value, dict) and value.get('protocol') == PROTOCOL
                and value.get('activation_id') == active['run_id'] and value.get('read_only') is True
                and value.get('sse_emitted') is False and value.get('client_connection_resumed') is False,
                'invalid_recovery_response')
        ticket = value.get('ticket')
        require(isinstance(ticket, dict) and ticket.get('request_id') == request_id
                and isinstance(ticket.get('route_id'), str) and re.fullmatch('[0-9a-f]{32}', ticket['route_id'])
                and type(ticket.get('seq')) is int and ticket['seq'] > 0
                and isinstance(ticket.get('request_sha256'), str) and re.fullmatch('[0-9a-f]{64}', ticket['request_sha256']),
                'recovery_ticket_mismatch')
        require(ticket.get('state') in {'reserved', 'upload_unknown', 'uploaded', 'publish_unknown',
                                       'published', 'verified', 'acknowledged'}, 'invalid_recovery_state')
        require(all(type(value.get(key)) is bool for key in
                ('result_available', 'delivery_started', 'tool_output_withheld')), 'invalid_recovery_response')
        require(not value['result_available'] or ticket['state'] == 'verified', 'invalid_recovery_state')
        result = value.get('result')
        output = {key: value[key] for key in ('protocol', 'activation_id', 'ticket', 'result_available',
                  'delivery_started', 'tool_output_withheld', 'read_only', 'sse_emitted', 'client_connection_resumed')}
        output.update(verified_result_saved=False, automatic_retry=False, native_fallback=False)
        if result is not None:
            require(value['result_available'] and ticket.get('state') == 'verified'
                    and not value['tool_output_withheld'], 'unverified_recovery_result')
            from .wire import validate_response
            # The owner has verified signed remote bindings. Validate the text
            # Responses object again before saving or showing it; tools are never
            # available through this inspection command.
            result = validate_response(result, {'model': selected['model'], 'input': [], 'tools': []},
                                       max_bytes=grant['limits']['max_result_bytes'])
            raw = canonical({'protocol': PROTOCOL, 'activation_id': active['run_id'],
                             'ticket': {k: ticket[k] for k in ('request_id', 'route_id', 'seq', 'request_sha256')},
                             'result': result, 'result_sha256': hash_bytes(canonical(result))})
            directory = private_dir(runtime / 'recovered', create=True)
            path = directory / (request_id + '.json')
            from .storage import private_write as immutable_write
            immutable_write(path, raw, immutable=True)
            output.update(verified_result_saved=True, result_path=str(path),
                          response_sha256=hash_bytes(canonical(result)))
            if print_result:
                output['output_text'] = '\n'.join(part['text'] for item in result['output']
                    for part in item['content'])
        else:
            require(not value['result_available'] or value['tool_output_withheld'], 'recovery_result_missing')
        return output

    def copy_join(self):
        active = self.active()
        require(active is not None, 'v3_not_started')
        self._ready(active)
        runtime = Path(active['runtime'])
        grant = validate_grant(read(runtime / 'grant.json'))
        spec = read(runtime / 'spec.json')
        require(spec.get('transport_authorization_contract') == AUTHORIZATION_CONTRACT,
                'fresh_transport_authorization_activation_required')
        require(grant['package_sha256'] == spec['package_sha256'] == self.ports.package(self.root),
                'lightweight_package_changed_since_start')
        require(self.ports.now() < grant['expires_at'], 'grant_expired')
        scope = authorization_scope(grant)
        if not self.ui.confirm('Copy this explicit authorization and private JOIN to the clipboard?\n'
            + scope['statement'] + '\n\n'
            'Paste it once into your current dot conversation only if you approve BOTH result uploads and control-Doc writes. '
            'Copying alone does not send it or prove owner approval to dot. '
            'The private code is not printed or logged. Clipboard managers may retain it. Continue?'):
            raise Cancelled()
        # Recheck the activation/expiry after the dialog, before exposing its key.
        current = self.active()
        require(current is not None and current['run_id'] == active['run_id'], 'v3_activation_changed')
        self._ready(current)
        require(read(runtime / 'grant.json') == grant, 'immutable_grant_mismatch')
        require(self.ports.package(self.root) == grant['package_sha256'],
                'lightweight_package_changed_since_start')
        self.ports.copy(join_text(grant, read_private_file(runtime / 'join-key', 64).decode()))
        return {'copied': True, 'join_sent': False, 'native_admission_verified': False}

    def apply_config(self):
        active = self.active()
        require(active is not None, 'v3_not_started')
        runtime = Path(active['runtime']); spec = read(runtime / 'spec.json')
        info = self._ready(active)
        home = spec['settings']['codex_home']
        outstanding = [t for t in self.transactions(runtime) if t['phase'] not in ('restored', 'aborted')]
        if outstanding:
            return {**self.status(), 'config_transaction_id': outstanding[-1]['id']}
        plan = config_tx.preview(runtime, home, info)
        message = ('Apply this UNVERIFIED FIRST-USE TRIAL patch?\n'
            f"Target: {plan['config_path']}\nBefore SHA-256: {plan['before_hash']}\nAfter SHA-256: {plan['after_hash']}\n"
            + plan['diff'] + '\n\n' + '\n'.join(plan['warnings']) + '\n'
            'Listener is ready; JOIN acceptance, native admission, real response and tool compatibility are separate facts. '
            'After pasting JOIN, fully restart your actual client and begin a fresh thread. '
            'Restore returns only owned fields and keeps unrelated edits; conflicts stop rather than overwrite. Proceed?')
        if not self.ui.confirm(message): raise Cancelled()
        def recheck():
            fresh = self.active()
            require(fresh is not None and fresh['run_id'] == active['run_id'], 'v3_activation_changed')
            require(self._ready(fresh) == info, 'v3_config_info_changed')
            inspected = legacy_inspection(self.legacy_state, self.router_active, home)
            require(inspected['safe_to_activate'], (inspected['blockers'] or ['legacy_state_unverified'])[0])
        result = config_tx.apply(runtime, home, info, expected_before_hash=plan['before_hash'],
            expected_after_hash=plan['after_hash'], confirm=True, check_ready=recheck)
        save(runtime / 'config-applied.json', result)
        return {**self.status(), **result, 'next': 'Paste the copied JOIN into dot, restart the local client, then use a fresh thread.'}

    def stop(self):
        private_dir(self.state, create=True)
        save(self.state / 'stop-intent.json', {'request_id': secrets.token_hex(16), 'requested_at': int(self.ports.now())})
        active = self.active()
        if active is None: return {'stage': 'NOT_STARTED', 'native_children_stopped': False}
        runtime = Path(active['runtime'])
        # Private intent is immediately visible to the server; remote Google work
        # happens in the supervisor and cannot block this local stop request.
        intent = {'activation_id': active['run_id'], 'requested_at': int(self.ports.now())}
        save(runtime / 'stop.json', intent)
        if (runtime / 'gateway').is_dir():
            save(runtime / 'gateway/stop.json', intent)
        until = time.monotonic() + 3
        while alive(active.get('pid')) and time.monotonic() < until:
            self.ports.wait(.1)
        if not alive(active.get('pid')):
            previous = read(runtime / 'status.json') if (runtime / 'status.json').exists() else {}
            save(runtime / 'status.json', {**previous, 'stage': 'STOPPED', 'listener_ready': False,
                'local_admission_stopped': True, 'remote_stop_requires_reconciliation': not previous.get('inbox_stop_published', False)})
        result = self.status()
        self.ui.notify('New local requests are stopped or stopping. Inbox stop publication may still be pending. '
            'Tell the current dot parent to stop this v3 activation and interrupt its recorded native children. '
            'A stopped Mac process or an Inbox stop record does not confirm platform children stopped. '
            'Restore client config separately, including when Google is unavailable.')
        return result

    def copy_stop(self):
        active = self.active()
        require(active is not None, 'v3_not_started')
        require((Path(active['runtime']) / 'stop.json').exists(), 'request_stop_first')
        message = (f"Stop Dots2Codex lightweight v3 activation {active['run_id']}. "
                   'Stop admitting new requests, observe its Inbox stop, and interrupt every actual recorded native child '
                   'using your native interrupt tool. Preserve unresolved request fences and report actual interrupt outcomes. '
                   'Do not replace or replay any request.')
        if not self.ui.confirm('Copy the stop request for this activation to the clipboard? Paste it into the same dot conversation. '
                               'Copying does not send it or confirm native shutdown.'):
            raise Cancelled()
        self.ports.copy(message)
        return {'copied': True, 'native_notification_sent': False, 'native_children_stopped': False}

    def restore(self):
        active = self.active()
        if active is None: return {'config_changed': False, 'write_performed': False}
        runtime = Path(active['runtime'])
        txs = [t for t in self.transactions(runtime) if t['phase'] not in ('restored', 'aborted')]
        require(len(txs) <= 1, 'multiple_transactions_require_reconciliation')
        if not txs: return {'config_changed': False, 'write_performed': False}
        tx = txs[0]
        if not self.ui.confirm('Restore only the v3-owned settings in ' + tx['config_path'] + '?\n'
            'Unrelated edits stay. An owned-field conflict stops the restore. This does not stop native work. '
            'Restart local clients afterward. No Google access is needed.'):
            raise Cancelled()
        if tx['phase'] in ('prepared', 'restore_prepared'):
            result = config_tx.reconcile(runtime, tx['id'])
            if result['phase'] in ('restored', 'aborted'): return result
        return config_tx.restore(runtime, tx['id'], confirm=True)


def supervise(runtime, *, docs=None, drive=None):
    """Private owned process; no stdout, no inference, no automatic re-admission."""
    runtime = private_dir(runtime)
    spec = read(runtime / 'spec.json'); run_id = spec['run_id']
    require(spec.get('contract') == CONTRACT and spec['runtime'] == str(runtime)
            and runtime.name == run_id, 'v3_runtime_binding_mismatch')
    stop_event = threading.Event()
    gateway = server = None
    server_started = False
    def request_stop(*_):
        stop_event.set()
        save(runtime / 'stop.json', {'activation_id': run_id, 'requested_at': int(time.time())})
        if gateway is not None: gateway.request_stop()
    for name in (signal.SIGINT, signal.SIGTERM): signal.signal(name, request_stop)
    with private_lock(runtime / 'supervisor.lock'):
        save(runtime / 'supervisor.json', {'run_id': run_id, 'pid': os.getpid(),
                                          'process_identity': process_identity(os.getpid())})
        try:
            require(package_identity(ROOT) == spec['package_sha256'], 'lightweight_package_changed_since_start')
            require(not (runtime / 'stop.json').exists(), 'v3_stop_requested')
            settings = spec['settings']
            require(credential_check(settings['authorized_user_file']) == settings['credential_evidence'],
                    'credential_file_changed_since_approval')
            os.environ['DOTS_GOOGLE_AUTHORIZED_USER_FILE'] = settings['authorized_user_file']
            if docs is None or drive is None:
                from .google_ports import create_docs_client, create_drive_client
                docs, drive = create_docs_client(), create_drive_client()
            grant_path = runtime / 'grant.json'
            if not grant_path.exists():
                pending = runtime / 'inbox-creation.json'
                require(not pending.exists(), 'inbox_creation_outcome_unknown_no_automatic_retry')
                folder = drive.get_metadata(settings['folder_id'])
                require(folder.get('id') == settings['folder_id'] and folder.get('trashed') is False
                        and folder.get('mimeType') == 'application/vnd.google-apps.folder', 'google_folder_not_verified')
                save(pending, {'activation_id': run_id, 'phase': 'creation_intent'})
                inbox = drive.create_document_once(settings['folder_id'], 'Dots2Codex lightweight v3 Inbox ' + run_id)
                save(pending, {'activation_id': run_id, 'phase': 'created', 'inbox_id': inbox})
                pair = settings['selection']
                grant = {'protocol': PROTOCOL, 'activation_id': run_id, 'folder_id': settings['folder_id'],
                    'inbox_id': inbox, 'created_at': spec['created_at'],
                    'expires_at': spec['created_at'] + settings['seconds'],
                    'allowed_pairs': [{'model': pair['model'], 'reasoning_effort': pair['reasoning_effort']}],
                    'limits': settings['limits'], 'package_sha256': spec['package_sha256']}
                save(grant_path, validate_grant(grant))
            grant = validate_grant(read(grant_path))
            require(grant['activation_id'] == run_id and grant['package_sha256'] == spec['package_sha256'],
                    'v3_activation_mismatch')
            key = read_private_file(runtime / 'join-key', 64).decode()
            from .gateway import MacGateway, ResponsesServer
            gateway_path = runtime / 'gateway'
            gateway = MacGateway(gateway_path, grant, key, docs, drive,
                create=not gateway_path.exists(), wait_seconds=settings['wait_seconds'])
            gateway.initialize()
            require(not (runtime / 'stop.json').exists(), 'v3_stop_requested')
            port_path = runtime / 'bound-port.json'
            port = read(port_path)['port'] if port_path.exists() else settings['port']
            server = ResponsesServer(gateway, port=port)
            save(port_path, {'port': server.server_port})
            catalog = write_catalog(runtime / 'codex-models.json', settings['selection'])
            save(runtime / 'config-info.json', {'protocol': PROTOCOL, 'generation': run_id,
                'base_url': server.base_url, 'catalog_path': catalog, 'selection': settings['selection']})
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start(); server_started = True
            while not stop_event.is_set() and not (runtime / 'stop.json').exists():
                save(runtime / 'status.json', {**gateway.status(), 'stage': 'LOCAL_READY',
                    'listener_ready': True, 'limits': grant['limits'], 'expires_at': grant['expires_at'],
                    'last_local_observation': int(time.time()), 'native_children_stopped': False})
                stop_event.wait(.5)
            gateway.request_stop()
            server.shutdown(); server.server_close(); server = None
            save(runtime / 'status.json', {'protocol': PROTOCOL, 'stage': 'STOPPING', 'listener_ready': False,
                'local_admission_stopped': True, 'inbox_stop_published': False,
                'native_notification_sent': False, 'native_children_stopped': False})
            outcome = {}
            def publish_stop():
                try:
                    gateway.stop(deadline=time.monotonic() + 15)
                    outcome['inbox_stop_published'] = True
                except Exception as error:
                    outcome.update(inbox_stop_published=False, stop_error=safe_error(error))
            worker = threading.Thread(target=publish_stop, daemon=True)
            worker.start(); worker.join(16)
            save(runtime / 'status.json', {'protocol': PROTOCOL, 'stage': 'STOPPED', 'listener_ready': False,
                'local_admission_stopped': True, 'inbox_stop_published': outcome.get('inbox_stop_published', False),
                'remote_stop_requires_reconciliation': not outcome.get('inbox_stop_published', False),
                'native_notification_sent': False, 'native_children_stopped': False,
                **({'stop_error': outcome['stop_error']} if 'stop_error' in outcome else {})})
        except Exception as error:
            save(runtime / 'status.json', {'protocol': PROTOCOL, 'stage': 'FAILED', 'listener_ready': False,
                'error': safe_error(error), 'native_children_stopped': False,
                'remote_stop_requires_reconciliation': True})
        finally:
            if server is not None:
                if server_started: server.shutdown()
                server.server_close()
            if gateway is not None:
                # Avoid waiting forever on a failed remote stop holding its lock.
                try: gateway.close()
                except Exception: pass


def legacy_action(operation, launcher):
    """Explicit recovery-only edge; never imported in the v3 serve path."""
    from remote_transport.global_desktop import DesktopGlobal
    backend = DesktopGlobal(root=launcher.root, state=launcher.legacy_state / 'global',
                            config=launcher.router_config, ui=launcher.ui)
    return getattr(backend, operation)(None)


def parser():
    result = argparse.ArgumentParser(description='Explicit opt-in lightweight v3, separate from START/v2')
    result.add_argument('operation', nargs='?', default='menu', choices=(
        'menu', 'start', 'resume', 'recover', 'status', 'stop', 'copy-join', 'copy-stop', 'apply-config', 'restore',
        'legacy-status', 'legacy-stop', 'legacy-restore', 'serve'))
    result.add_argument('--state', default=str(DEFAULT_STATE))
    result.add_argument('--legacy-state', default=str(DEFAULT_LEGACY_STATE))
    result.add_argument('--router-config', default=str(DEFAULT_ROUTER_CONFIG))
    result.add_argument('--router-active', default=str(DEFAULT_ROUTER_ACTIVE))
    result.add_argument('--codex-home')
    result.add_argument('--credentials')
    result.add_argument('--folder-id')
    result.add_argument('--model')
    result.add_argument('--effort')
    result.add_argument('--runtime')
    result.add_argument('--request-id', help='Existing 32-hex request ID; recover never creates or replays a request')
    result.add_argument('--print-result', action='store_true', help='Show recovered assistant text in this command output')
    return result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = parser().parse_args(argv)
    os.umask(0o077)
    require(sys.version_info >= (3, 11), 'python_311_required')
    if args.operation == 'serve':
        require(args.runtime is not None, 'runtime_required')
        supervise(args.runtime)
        return 0
    ui = UI()
    launcher = Launcher(state=args.state, legacy_state=args.legacy_state,
        router_config=args.router_config, router_active=args.router_active, ui=ui)
    if args.operation == 'menu':
        choices = {'Start lightweight v3 trial': 'start', 'Status': 'status', 'Resume same activation': 'resume',
            'Recover existing request': 'recover',
            'Copy private JOIN': 'copy-join', 'Apply first-use config trial': 'apply-config',
            'Stop local v3': 'stop', 'Copy native stop request': 'copy-stop',
            'Restore v3 config': 'restore', 'Legacy Global status': 'legacy-status',
            'Stop legacy Global': 'legacy-stop', 'Restore legacy Global config': 'legacy-restore', 'Quit': 'quit'}
        args.operation = choices[ui.choose('Dots2Codex lightweight v3 (opt-in)', choices)]
        if args.operation == 'quit': return 0
        if args.operation == 'recover' and args.request_id is None:
            args.request_id = ui.text('Existing request ID (32 lowercase hex characters); save verified text without replay')
    if args.operation in {'resume', 'restore', 'apply-config', 'legacy-status', 'legacy-stop', 'legacy-restore'}:
        # Reuse a healthy private interpreter when available, never install just
        # to stop/status/restore an existing activation.
        from .environment import Environment
        current = Environment(ROOT, args.state, include_global=True).current()
        if current is not None and Path(sys.prefix).absolute() != current:
            forwarded = [args.operation, '--state', args.state, '--legacy-state', args.legacy_state,
                         '--router-config', args.router_config, '--router-active', args.router_active]
            env = {k:v for k,v in os.environ.items() if k not in {'PYTHONPATH', 'PYTHONHOME'}}
            env['PYTHONNOUSERSITE'] = '1'
            python = current / 'bin/python3'
            os.execve(str(python), [str(python), '-B', '-m', 'dots_lite.launcher', *forwarded], env)
    if args.operation == 'start':
        # Dependency setup is separately approved, versioned and private. Recovery
        # and status never force installation or require Google access.
        from .environment import Environment
        python = Environment(ROOT, args.state, include_global=True).ensure(ui)
        if Path(sys.prefix).absolute() != python.parent.parent:
            forwarded = [args.operation]
            for key in ('state', 'legacy_state', 'router_config', 'router_active', 'codex_home',
                        'credentials', 'folder_id', 'model', 'effort'):
                value = getattr(args, key)
                if value is not None: forwarded += ['--' + key.replace('_', '-'), value]
            env = {k:v for k,v in os.environ.items() if k not in {'PYTHONPATH', 'PYTHONHOME'}}
            env['PYTHONNOUSERSITE'] = '1'
            os.execve(str(python), [str(python), '-B', '-m', 'dots_lite.launcher', *forwarded], env)
        result = launcher.start(args)
    elif args.operation == 'recover':
        require(args.request_id is not None, 'recovery_request_id_required')
        result = launcher.recover(args.request_id, print_result=args.print_result)
    elif args.operation.startswith('legacy-'):
        result = legacy_action(args.operation.removeprefix('legacy-'), launcher)
    else:
        result = getattr(launcher, args.operation.replace('-', '_'))()
    ui.notify(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cli():
    try:
        return main()
    except Cancelled:
        return 130
    except Exception as error:
        # No traceback or provider exception string can leak a private JOIN/token.
        print('Lightweight v3: ' + safe_error(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(cli())
