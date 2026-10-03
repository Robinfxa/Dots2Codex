"""Shared Direct service with session CLI and global client configuration modes.

Filesystem, exact-PID identity and reversible configuration patterns are adapted
from the repository's MIT-licensed main launcher. Tunnel output is deliberately
not captured: third-party logs can contain credentials and request payloads.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
BUNDLE_ROOT = ROOT.parent
if str(BUNDLE_ROOT) not in sys.path:
    sys.path.insert(0, str(BUNDLE_ROOT))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from dots_lite.private_io import private_dir, private_lock, read, save, no_symlinks, read_private_file
from dots_lite.protocol import ProtocolError, require

CONTRACT = 'dots-direct-global-launcher/1'
DEFAULT_STATE = Path.home() / 'Library/Application Support/Dots2Codex Direct Global'
DEFAULT_CREDENTIALS = Path.home() / 'Library/Application Support/Dots2Codex Direct'
GUIDE = 'https://developers.openai.com/api/docs/guides/secure-mcp-tunnels'
_SECRET_NAMES = ('CONTROL_PLANE_API_KEY', 'DOTS_BRIDGE_HTTP_BEARER')


class Cancelled(Exception):
    pass


class UI:
    def text(self, prompt, default=''):
        value = input(prompt + (f' [{default}]' if default else '') + ': ').strip()
        return value or default

    def confirm(self, text, word='YES'):
        print(text, flush=True)
        return input(f'输入 {word} 继续，直接回车取消: ').strip() == word

    def say(self, text):
        print(text, flush=True)


def safe_error(error):
    text = str(error)
    return text if re.fullmatch(r'[a-z][a-z0-9_]{1,100}', text) else type(error).__name__


def base_environment():
    allowed = {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'LC_CTYPE', 'SYSTEMROOT', 'CODEX_HOME',
               'CODEX_BIN', 'TERM', 'COLORTERM', 'TERM_PROGRAM'}
    result = {k: v for k, v in os.environ.items() if k in allowed}
    result.update(PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
    return result


def process_identity(pid):
    """Only inspect the exact recorded PID. PID reuse never authorizes a signal."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        if sys.platform.startswith('linux'):
            raw = Path(f'/proc/{pid}/stat').read_text()
            fields = raw[raw.rfind(')') + 2:].split()
            if fields[0] == 'Z':
                return None
            uid = Path(f'/proc/{pid}').stat().st_uid
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            value = f'{uid}:{boot}:{fields[19]}'
        else:
            result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'uid=', '-o', 'lstart=',
                                     '-o', 'stat=', '-o', 'command='],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    env=base_environment(), timeout=3, check=False)
            if result.returncode or not result.stdout.strip():
                return None
            fields = result.stdout.strip().split(None, 7)
            if len(fields) != 8 or not fields[0].isdecimal() or 'Z' in fields[6]:
                return None
            # STAT is sampled scheduler/terminal state, not process identity.
            # Keep it only for zombie detection; hash UID, start time and argv.
            value = '\0'.join(fields[:6] + [fields[7]])
        return hashlib.sha256(value.encode()).hexdigest()
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def alive(pid):
    return process_identity(pid) is not None


def owned(record):
    return bool(record and record.get('process_identity') and
                process_identity(record.get('pid')) == record['process_identity'])


def owner_record(role, run_id, pid=None):
    pid = os.getpid() if pid is None else pid
    identity = process_identity(pid)
    require(identity is not None, 'process_identity_unavailable')
    return {'contract': CONTRACT, 'role': role, 'run_id': run_id, 'pid': pid,
            'process_identity': identity}


def stop_owned(record, timeout=3):
    if not record:
        return True
    for sig in (signal.SIGINT, signal.SIGTERM):
        identity = process_identity(record.get('pid'))
        if identity is None:
            return True
        require(identity == record.get('process_identity'), 'refuse_to_signal_unowned_process')
        try:
            os.kill(record['pid'], sig)
        except ProcessLookupError:
            return True
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if not alive(record['pid']):
                return True
            time.sleep(.05)
    return not alive(record['pid'])


def register_child_owner():
    """Called by the real MCP entrypoint, after stripping the tunnel key."""
    directory = os.environ.pop('DOTS_DIRECT_OWNER_DIR', None)
    run_id = os.environ.get('DOTS_DIRECT_RUN_ID')
    if directory is None and run_id is None:
        return
    require(directory and re.fullmatch(r'[0-9a-f]{32}', run_id or ''), 'invalid_service_owner')
    directory = private_dir(directory)
    spec = read(directory / 'spec.json')
    require(spec.get('run_id') == run_id and directory.name == run_id, 'service_owner_mismatch')
    # Prevent a repeated tunnel child from replacing a still-live bridge owner.
    with private_lock(directory / 'bridge.lock'):
        path = directory / 'bridge.json'
        if path.exists():
            previous = read(path)
            require(not alive(previous.get('pid')), 'bridge_already_running')
        save(path, owner_record('bridge', run_id))


def port_available(port):
    try:
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', port))
        return True
    except OSError:
        return False


def loopback_get(url, *, bearer=None):
    parsed = urlsplit(url)
    require(parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.port
            and parsed.netloc == f'127.0.0.1:{parsed.port}' and not parsed.fragment
            and not parsed.query, 'invalid_loopback_endpoint')
    connection = http.client.HTTPConnection('127.0.0.1', parsed.port, timeout=1)
    try:
        headers = {'Authorization': 'Bearer ' + bearer} if bearer else {}
        connection.request('GET', parsed.path, headers=headers)
        response = connection.getresponse()
        data = response.read(65537)
        require(response.status == 200 and len(data) <= 65536, 'service_not_ready')
        return data
    finally:
        connection.close()


def settings_path(state):
    return state / 'settings.json'


def load_settings(state):
    value = read(settings_path(state), 65536)
    required = {'contract', 'python', 'http_port', 'admin_port', 'tunnel_binary', 'profile_dir',
                'profile', 'profile_sha256', 'codex_home', 'config_id', 'bundle_root', 'package_sha256', 'credential_dir'}
    require(type(value) is dict and set(value) == required and value.get('contract') == CONTRACT,
            'invalid_launcher_settings')
    require(all(type(value[k]) is int and 1024 <= value[k] <= 65535 for k in ('http_port', 'admin_port'))
            and value['http_port'] != value['admin_port'], 'invalid_service_ports')
    for key in ('python', 'tunnel_binary', 'codex_home', 'profile_dir', 'credential_dir'):
        require(type(value[key]) is str and Path(value[key]).is_absolute(), 'invalid_launcher_path')
        if key in ('codex_home', 'profile_dir', 'credential_dir'):
            no_symlinks(value[key])
    require(value['profile_dir'] == str(state / 'tunnel-profile') and value['profile'] == 'direct-global',
            'invalid_profile_binding')
    require(re.fullmatch(r'[0-9a-f]{32}', value['config_id']) and
            re.fullmatch(r'[0-9a-f]{64}', value['profile_sha256']), 'invalid_launcher_identity')
    return value


def tunnel_profile_path(settings):
    return Path(settings['profile_dir']) / (settings['profile'] + '.yaml')


def check_profile(settings):
    require(hashlib.sha256(read_private_file(tunnel_profile_path(settings), 65536)).hexdigest()
            == settings['profile_sha256'], 'reviewed_tunnel_profile_changed')


def mcp_command(state, python, port):
    return shlex.join(['/usr/bin/env', 'PYTHON_BIN=' + str(python), 'PYTHONDONTWRITEBYTECODE=1',
                       str(ROOT / 'scripts/start_mcp.sh'), '--config',
                       str(state / 'bridge/config.json'), '--http-port', str(port)])


def tunnel_argv(settings):
    return [settings['tunnel_binary'], 'run', '--profile', settings['profile'],
            '--profile-dir', settings['profile_dir']]


class Ports:
    def __init__(self):
        self.children = {}

    def spawn(self, state, runtime, settings, credentials):
        env = credentials.tunnel_env(base_environment())
        with open(os.devnull, 'wb') as output:
            proc = subprocess.Popen([settings['python'], '-B', str(ROOT / 'global_launcher.py'),
                                     '_serve', '--state-dir', str(state), '--runtime', str(runtime)],
                                    cwd=str(BUNDLE_ROOT), env=env, stdin=subprocess.DEVNULL,
                                    stdout=output, stderr=output, start_new_session=True)
        self.children[proc.pid] = proc
        # Its own owner file is authoritative if it exits before this read.
        return {'pid': proc.pid, 'process_identity': process_identity(proc.pid)}

    def ready(self, settings, run_id, bearer):
        data = json.loads(loopback_get(f"http://127.0.0.1:{settings['http_port']}/health", bearer=bearer))
        require(data.get('mode') == 'global' and data.get('listener_ready') is True
                and data.get('instance_id') == run_id and data.get('config_id') == settings['config_id'],
                'bridge_health_identity_mismatch')
        require(data.get('tool_contract') == 'dots-direct-tools/2', 'bridge_tool_contract_upgrade_required')
        # Official readiness, rather than mere occupied TCP ports or log text.
        loopback_get(f"http://127.0.0.1:{settings['admin_port']}/readyz")
        return data

    def wait(self, seconds):
        time.sleep(seconds)


class Launcher:
    def __init__(self, state=DEFAULT_STATE, *, ui=None, ports=None, transactions=None):
        self.state = no_symlinks(state)
        require(self.state != BUNDLE_ROOT and BUNDLE_ROOT not in self.state.parents,
                'state_must_be_outside_source_bundle')
        self.ui, self.ports = ui or UI(), ports or Ports()
        self._transactions = transactions

    @property
    def tx(self):
        if self._transactions is None:
            from direct_bridge import global_config
            self._transactions = global_config
        return self._transactions

    def current(self):
        path = self.state / 'current.json'
        if not path.exists():
            return None
        value = read(path)
        require(value.get('contract') == CONTRACT and re.fullmatch(r'[0-9a-f]{32}', value.get('run_id', '')),
                'invalid_current_run')
        return self.state / 'runs' / value['run_id']

    def records(self, runtime):
        records = {}
        if runtime is None:
            return records
        for role in ('owner', 'tunnel', 'bridge'):
            path = runtime / (role + '.json')
            if path.exists():
                value = read(path)
                require(value.get('contract') == CONTRACT and value.get('run_id') == runtime.name,
                        'process_owner_binding_mismatch')
                records[role] = value
        return records

    def status(self):
        runtime = self.current()
        records = self.records(runtime)
        value = read(runtime / 'status.json') if runtime and (runtime / 'status.json').exists() else {}
        processes = {role: {'running': alive(record.get('pid')), 'owned': owned(record)}
                     for role, record in records.items()}
        result = {'stage': value.get('stage', 'STOPPED'), 'processes': processes,
                  'ready': False, 'native_platform_verified': False,
                  'durable_state_preserved': True, 'configured': settings_path(self.state).exists()}
        if 'error' in value:
            result['error'] = value['error']
        result['configuration'] = self.tx.status(self.state)
        result['active_session_clients'] = len(self.active_session_clients(runtime))
        if runtime and all(processes.get(role, {}).get('owned') for role in ('owner', 'tunnel', 'bridge')):
            try:
                from direct_bridge.global_credentials import load_credentials
                settings = load_settings(self.state)
                credential = load_credentials(settings['credential_dir'])
                self.ports.ready(settings, runtime.name, credential.http_bearer)
                result['ready'] = True
            except Exception:
                pass
        if not any(record['running'] for record in processes.values()):
            result['stage'] = 'STOPPED' if result['stage'] != 'FAILED' else 'FAILED'
        return result

    def diagnostics(self, lines=80):
        """Read-only, payload-free snapshot; never uses credentials or contacts services."""
        from diagnostics import recent
        snapshot = {'stage': 'UNKNOWN', 'processes': {}, 'local_status': 'available'}
        try:
            runtime = self.current()
            records = self.records(runtime)
            value = read(runtime / 'status.json') if runtime and (runtime / 'status.json').exists() else {}
            stage = value.get('stage', 'STOPPED')
            if type(stage) is str and stage in {'STARTING', 'RUNNING', 'STOPPING', 'STOPPED', 'FAILED'}:
                snapshot['stage'] = stage
            snapshot['processes'] = {role: {'running': alive(record.get('pid')), 'owned': owned(record)}
                                     for role, record in records.items()}
        except Exception:
            snapshot['local_status'] = 'unavailable'
        snapshot.update(recent(self.state / 'bridge', lines))
        return snapshot

    def active_session_clients(self, runtime):
        """Inspect only exact recorded PIDs; stale/reused PIDs never block Start."""
        if runtime is None or not (runtime / 'sessions').exists():
            return []
        result = []
        for path in sorted((runtime / 'sessions').glob('*.json')):
            value = read(path)
            require(value.get('contract') == CONTRACT and value.get('run_id') == runtime.name
                    and value.get('session_id') == path.stem, 'session_owner_binding_mismatch')
            if value.get('stage') != 'EXITED' and any(owned(value.get(key)) for key in ('launcher', 'client')):
                result.append(value)
        return result

    def _reserve_session(self, runtime, session_id):
        if session_id is None:
            return
        require(re.fullmatch(r'[0-9a-f]{32}', session_id), 'invalid_session_identity')
        directory = private_dir(runtime / 'sessions', create=True)
        path = directory / (session_id + '.json')
        require(not path.exists(), 'session_identity_already_used')
        save(path, {'contract': CONTRACT, 'run_id': runtime.name, 'session_id': session_id,
                    'stage': 'LAUNCHING', 'launcher': owner_record('session-launcher', runtime.name)})

    def _ready(self, runtime, settings, bearer):
        require(not (runtime / 'stop.json').exists(), 'start_cancelled_by_stop')
        records = self.records(runtime)
        require(all(owned(records.get(role)) for role in ('owner', 'tunnel', 'bridge')),
                'owned_services_not_running')
        self.ports.ready(settings, runtime.name, bearer)
        return True

    def _stop_services(self, runtime):
        if runtime is None:
            return True
        save(runtime / 'stop.json', {'requested_at': time.time(), 'run_id': runtime.name})
        records = self.records(runtime)
        # Let the supervisor close its tunnel/MCP children first. No app is killed.
        supervisor = records.get('owner')
        if owned(supervisor):
            stop_owned(supervisor, timeout=4)
        okay = True
        records = self.records(runtime)
        for role in ('bridge', 'tunnel', 'owner'):
            record = records.get(role)
            if record and alive(record.get('pid')):
                okay = stop_owned(record, timeout=2) and okay
        require(okay, 'owned_service_still_stopping')
        for record in records.values():
            process = getattr(self.ports, 'children', {}).pop(record.get('pid'), None)
            if process is not None:
                process.wait(timeout=1)
        save(runtime / 'status.json', {'stage': 'STOPPED', 'durable_state_preserved': True})
        return True

    def _restore_config(self, *, confirm):
        state = self.tx.status(self.state)
        transaction = state.get('active_transaction')
        if transaction is None:
            return {'stage': 'already_restored'}
        if confirm and not self.ui.confirm('恢复本工具改过的全局 Codex 配置？会保留你的其他修改和所有请求记录。', 'RESTORE'):
            raise Cancelled()
        tid = transaction['transaction_id']
        reconciled = self.tx.reconcile(self.state, tid)
        if reconciled.get('phase') in ('aborted', 'restored'):
            return reconciled
        return self.tx.restore(self.state, tid, confirm=True)

    def restore(self):
        # No tunnel, credential, SDK, Google, model or live-client prerequisite.
        if not self.state.exists():
            return {'stage': 'already_restored'}
        with private_lock(self.state / 'lifecycle.lock'):
            return self._restore_config(confirm=True)

    def stop(self):
        if not self.state.exists():
            return {'stage': 'STOPPED', 'durable_state_preserved': True}
        # This intent is visible even while Start holds the lifecycle lock.
        save(self.state / 'stop-intent.json', {'nonce': secrets.token_hex(16), 'at': time.time()})
        runtime = self.current()
        if runtime:
            save(runtime / 'stop.json', {'requested_at': time.time(), 'run_id': runtime.name})
        # Stop may cancel Start even while that command is waiting for APPLY.
        # Config transactions carry their own home lock and repeat the stop/readiness
        # check immediately before commit, so no launcher UI lock is needed here.
        stop_error = None
        try:
            self._stop_services(runtime)
        except Exception as error:
            stop_error = error
        restored = self._restore_config(confirm=False)
        if stop_error is not None:
            raise stop_error
        return {'stage': 'STOPPED', 'configuration': restored, 'durable_state_preserved': True,
                'native_tasks_stopped': False}

    def _config_info(self, settings, credential):
        config = read(self.state / 'bridge/config.json')
        selection = self.tx.default_selection()
        encoded = json.dumps(self.tx.catalog_for_pairs(config['allowed_pairs'], selection),
                             sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        catalog = self.state / ('codex-models-' + hashlib.sha256(encoded).hexdigest() + '.json')
        self.tx.write_catalog(catalog, config['allowed_pairs'], selection)
        return {'base_url': f"http://127.0.0.1:{settings['http_port']}/v1",
                'config_id': settings['config_id'], 'local_bearer': credential.http_bearer,
                'catalog_path': str(catalog), 'selection': selection, 'allowed_pairs': config['allowed_pairs']}

    def _apply_config(self, runtime, settings, credential):
        status = self.tx.status(self.state, settings['codex_home'])
        previous = status.get('active_transaction')
        if previous:
            self.tx.reconcile(self.state, previous['transaction_id'])
            previous = self.tx.status(self.state, settings['codex_home']).get('active_transaction')
        require(previous is None or (previous.get('phase') == 'committed'
                and previous.get('config_id') == settings['config_id'] and previous.get('config_matches_applied')),
                'restore_existing_transaction_before_apply')
        info = self._config_info(settings, credential)
        preview = self.tx.preview(self.state, settings['codex_home'], info)
        consent_path = self.state / 'config-consent.json'
        consent = read(consent_path) if consent_path.exists() else None
        scope = {'config_id': settings['config_id'], 'codex_home': settings['codex_home'],
                 'base_url': info['base_url'], 'local_bearer_sha256': hashlib.sha256(credential.http_bearer.encode()).hexdigest()}
        if consent != scope:
            if not self.ui.confirm(f"MCP、本地路由和官方隧道已就绪。\n修改 {settings['codex_home']}/config.toml，"
                                   "让新开的 Codex CLI/Desktop 会话使用本地 Direct provider？\n"
                                   "只把本机专用 bearer 写入配置及私有恢复备份，不写入隧道 API key。\n"
                                   "现有配置权限会收紧至 0600；Restore 保留该隐私权限，不放宽。\n"
                                   "之后一键 Start 可重新启用同一配置；Stop/Restore 可恢复。现有 Codex 窗口需你正常退出并重新打开。",
                                   'APPLY'):
                raise Cancelled()
            save(consent_path, scope)
        self._ready(runtime, settings, credential.http_bearer)
        return self.tx.apply(self.state, settings['codex_home'], info,
                             expected_before_hash=preview['before_hash'], expected_after_hash=preview['after_hash'],
                             confirm=True, check_ready=lambda: self._ready(runtime, settings, credential.http_bearer),
                             approve_private_config=True)

    def start(self, credential=None, *, timeout=45, package_sha256=None, mode='global', session_id=None):
        require(mode in ('global', 'session'), 'invalid_start_mode')
        require(mode == 'session' or session_id is None, 'invalid_session_start_mode')
        apply_global = mode == 'global'
        private_dir(self.state)
        settings = load_settings(self.state)
        require(settings['bundle_root'] == str(BUNDLE_ROOT), 'bundle_moved_review_profile_before_start')
        if credential is None:
            from direct_bridge.global_credentials import load_credentials
            credential = load_credentials(settings['credential_dir'])
        epoch_path = self.state / 'stop-intent.json'
        stop_epoch = read(epoch_path) if epoch_path.exists() else None
        with private_lock(self.state / 'lifecycle.lock'):
            require((read(epoch_path) if epoch_path.exists() else None) == stop_epoch, 'start_cancelled_by_stop')
            runtime = self.current()
            if runtime:
                records = self.records(runtime)
                live = [record for record in records.values() if alive(record.get('pid'))]
                require(all(owned(record) for record in live), 'refuse_to_signal_unowned_process')
                if live:
                    try:
                        self._ready(runtime, settings, credential.http_bearer)
                        healthy = True
                    except (OSError, ValueError, ProtocolError, http.client.HTTPException):
                        healthy = False
                    changed_package = package_sha256 is not None and settings['package_sha256'] != package_sha256
                    if healthy and not changed_package:
                        if not apply_global:
                            check_profile(settings)
                        applied = self._apply_config(runtime, settings, credential) if apply_global else {'stage': 'unchanged'}
                        self._reserve_session(runtime, session_id)
                        return {'stage': 'RUNNING', 'already_running': True, 'configuration': applied,
                                'configuration_scope': mode, 'run_id': runtime.name}
                    # Session mode may borrow a healthy service, never repair or
                    # replace a live global/other-session service as a side effect.
                    require(apply_global, 'session_service_conflict_use_status_or_explicit_stop')
                    require(not self.active_session_clients(runtime), 'session_clients_active_exit_before_restart')
                    # A transport-only restart preserves the exact database and
                    # native request/action IDs. It never starts a native worker.
                    self._stop_services(runtime)
                    self._restore_config(confirm=False)
                require(not self.active_session_clients(runtime), 'session_clients_active_exit_before_restart')
            if package_sha256 is not None and settings['package_sha256'] != package_sha256:
                settings = dict(settings, package_sha256=package_sha256)
                save(settings_path(self.state), settings)
            check_profile(settings)
            require(port_available(settings['http_port']) and port_available(settings['admin_port']),
                    'service_port_occupied_nothing_started')
            require((read(epoch_path) if epoch_path.exists() else None) == stop_epoch, 'start_cancelled_by_stop')
            run_id = secrets.token_hex(16)
            runtime = private_dir(self.state / 'runs' / run_id, create=True)
            save(runtime / 'spec.json', {'contract': CONTRACT, 'run_id': run_id, 'state': str(self.state),
                                        'settings': settings})
            save(runtime / 'status.json', {'stage': 'STARTING'})
            save(self.state / 'current.json', {'contract': CONTRACT, 'run_id': run_id})
            started = False
            try:
                require((read(epoch_path) if epoch_path.exists() else None) == stop_epoch, 'start_cancelled_by_stop')
                record = self.ports.spawn(self.state, runtime, settings, credential)
                started = True
                if not (runtime / 'owner.json').exists() and record.get('process_identity'):
                    save(runtime / 'owner.json', {'contract': CONTRACT, 'role': 'supervisor', 'run_id': run_id, **record})
                until = time.monotonic() + timeout
                while True:
                    require(not (runtime / 'stop.json').exists(), 'start_cancelled_by_stop')
                    info = read(runtime / 'status.json')
                    require(info.get('stage') != 'FAILED', info.get('error', 'service_start_failed'))
                    try:
                        self._ready(runtime, settings, credential.http_bearer)
                        break
                    except (OSError, ValueError, ProtocolError, http.client.HTTPException):
                        require(time.monotonic() < until, 'service_readiness_timeout')
                        self.ports.wait(.1)
                applied = self._apply_config(runtime, settings, credential) if apply_global else {'stage': 'unchanged'}
                self._reserve_session(runtime, session_id)
                save(runtime / 'active.json', {'run_id': run_id, 'started_at': time.time(), 'configuration_scope': mode})
                return {'stage': 'RUNNING', 'already_running': False, 'configuration': applied,
                        'configuration_scope': mode, 'run_id': run_id, 'native_platform_verified': False}
            except BaseException as error:
                cleanup_error = None
                try:
                    if started:
                        self._stop_services(runtime)
                except Exception as failure:
                    cleanup_error = safe_error(failure)
                if apply_global:
                    try:
                        self._restore_config(confirm=False)
                    except Exception as failure:
                        cleanup_error = safe_error(failure)
                if session_id is not None:
                    session_path = runtime / 'sessions' / (session_id + '.json')
                    if session_path.exists():
                        record = read(session_path)
                        record.pop('launcher', None)
                        record['stage'] = 'EXITED'
                        save(session_path, record)
                save(runtime / 'status.json', {'stage': 'FAILED', 'error': safe_error(error),
                     'cleanup_error': cleanup_error, 'durable_state_preserved': True})
                if cleanup_error:
                    raise ProtocolError(cleanup_error) from None
                raise

    def session(self, credential, client, *, package_sha256=None, timeout=45):
        """Launch one fresh CLI. Shared backend persists until explicit Stop."""
        from direct_bridge.session_client import codex_argv
        settings = load_settings(self.state)
        info = self._config_info(settings, credential)
        session_id = secrets.token_hex(16)
        argv = codex_argv(*client, info, session_id)
        configuration = self.tx.status(self.state, settings['codex_home'])
        self.ui.say('单会话模式：只给本次新 CLI 临时覆盖 provider，不写入或恢复 config.toml。\n'
                    '原有用户/项目安全设置保留；共享服务会留在后台，直到明确 Stop。')
        if configuration.get('active_transaction'):
            self.ui.say('注意：所选 CODEX_HOME 已有本工具的全局配置/恢复记录。单会话不会撤销它；其他客户端仍可能使用 Direct。')
        else:
            self.ui.say('其他已有全局或项目 provider 设置保持原样；这不会隔离或切换已打开的 Desktop/CLI。')
        outcome = self.start(credential, timeout=timeout, package_sha256=package_sha256,
                             mode='session', session_id=session_id)
        runtime = self.state / 'runs' / outcome['run_id']
        path = runtime / 'sessions' / (session_id + '.json')
        process = None
        client_identity = None
        launch_error = None
        try:
            try:
                # LAUNCHING is already durable under Start's lifecycle lock.
                with private_lock(self.state / 'lifecycle.lock'):
                    require(self.current() == runtime, 'service_changed_before_session_launch')
                    self._ready(runtime, settings, credential.http_bearer)
                    env = credential.codex_env(os.environ)
                    env['CODEX_HOME'] = settings['codex_home']
                    print_status(outcome, self.ui)
                    process = subprocess.Popen(argv, env=env, cwd=str(client[1]))
                    client_identity = process_identity(process.pid)
                    require(client_identity is not None or process.poll() is not None,
                            'session_client_identity_unavailable')
                    record = read(path)
                    if client_identity is not None:
                        record['client'] = {'pid': process.pid, 'process_identity': client_identity}
                    record['stage'] = 'RUNNING'
                    save(path, record)
            except BaseException as error:
                if process is None:
                    raise
                # An interrupted/failed post-spawn write is recoverable without
                # returning to the menu while this CLI still owns the terminal.
                if process.poll() is None:
                    client_identity = client_identity or process_identity(process.pid)
                    if client_identity is not None:
                        record = {'contract': CONTRACT, 'run_id': runtime.name,
                                  'session_id': session_id, 'stage': 'RUNNING',
                                  'launcher': owner_record('session-launcher', runtime.name),
                                  'client': {'pid': process.pid, 'process_identity': client_identity}}
                        try:
                            save(path, record)
                        except BaseException:
                            # Only this just-spawned child may be rolled back.
                            # Existing clients/backend are never signalled here.
                            launch_error = error
                            self.ui.say('无法保存新 CLI 的拥有者记录，正在关闭本次刚启动的 CLI；共享服务和其他客户端保持原样。')
                            try:
                                require(stop_owned(record['client']), 'session_client_cleanup_incomplete')
                            except Exception:
                                self.ui.say('尚未确认新 CLI 退出；请在该 CLI 中正常退出。仍等待它，未返回菜单。')
                    else:
                        # ps can transiently fail on macOS. The original durable
                        # launcher reservation remains live; never mark a live
                        # child EXITED merely because identity lookup failed.
                        self.ui.say('暂时无法核验新 CLI 的进程身份；保留启动记录并等待该 CLI 正常退出。共享服务不重启。')
                elif not isinstance(error, KeyboardInterrupt):
                    launch_error = error
            while True:
                try:
                    code = process.wait()
                    break
                except KeyboardInterrupt:
                    # Terminal SIGINT is also delivered to the foreground CLI.
                    self.ui.say('已将终端中断交给 CLI；等待它退出。共享服务保持运行。')
            if launch_error is not None:
                raise launch_error
            return {'stage': 'SESSION_EXITED', 'exit_code': code if code >= 0 else 128 - code,
                    'configuration_scope': 'session', 'service_left_running': True}
        finally:
            if process is None or process.poll() is not None:
                # poll/wait of our own Popen is proof of exit even if ps failed.
                save(path, {'contract': CONTRACT, 'run_id': runtime.name,
                            'session_id': session_id, 'stage': 'EXITED'})
            # A wait failure with a live child keeps the durable reservation.
            # No process scan, false EXITED status, or shared-service cleanup.



def supervise(state, runtime):
    state, runtime = private_dir(state), private_dir(runtime)
    spec = read(runtime / 'spec.json')
    require(spec.get('contract') == CONTRACT and spec.get('state') == str(state)
            and spec.get('run_id') == runtime.name and runtime.parent == state / 'runs', 'invalid_service_spec')
    settings = load_settings(state)
    require(settings == spec['settings'], 'settings_changed_during_start')
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    tunnel = None
    with private_lock(runtime / 'supervisor.lock'):
        save(runtime / 'owner.json', owner_record('supervisor', runtime.name))
        try:
            require(not (runtime / 'stop.json').exists(), 'start_cancelled_by_stop')
            check_profile(settings)
            from direct_bridge.global_credentials import load_credentials
            credential = load_credentials(settings['credential_dir'])
            env = credential.tunnel_env(base_environment())
            env.update(DOTS_DIRECT_OWNER_DIR=str(runtime), DOTS_DIRECT_RUN_ID=runtime.name)
            with open(os.devnull, 'wb') as output:
                tunnel = subprocess.Popen(tunnel_argv(settings), cwd=str(ROOT), env=env,
                                          stdin=subprocess.DEVNULL, stdout=output, stderr=output)
            save(runtime / 'tunnel.json', owner_record('tunnel', runtime.name, tunnel.pid))
            while not stop.is_set() and not (runtime / 'stop.json').exists():
                require(tunnel.poll() is None, 'tunnel_exited')
                if (runtime / 'active.json').exists():
                    save(runtime / 'status.json', {'stage': 'RUNNING', 'durable_state_preserved': True})
                stop.wait(.25)
        except BaseException as error:
            save(runtime / 'status.json', {'stage': 'FAILED', 'error': safe_error(error), 'durable_state_preserved': True})
        finally:
            okay = True
            # Closing tunnel gracefully first normally closes its stdio/MCP child.
            for role in ('tunnel', 'bridge'):
                path = runtime / (role + '.json')
                if path.exists():
                    try:
                        okay = stop_owned(read(path), timeout=2) and okay
                    except Exception:
                        okay = False
            if tunnel is not None:
                try:
                    tunnel.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    okay = False
            prior = read(runtime / 'status.json')
            if prior.get('stage') != 'FAILED':
                save(runtime / 'status.json', {'stage': 'STOPPED' if okay else 'STOPPING',
                                              'durable_state_preserved': True})
    return 0


def verify_package(root=BUNDLE_ROOT):
    """Check the current artifact, not main's preserved historical manifests."""
    root = Path(root)
    manifest_path = root / 'GLOBAL_DIRECT_PACKAGE_MANIFEST.json'
    require(manifest_path.is_file() and not manifest_path.is_symlink(), 'global_package_manifest_missing')
    require(manifest_path.stat().st_size <= 4 * 1024 * 1024, 'package_manifest_too_large')
    manifest = json.loads(manifest_path.read_text())
    require(manifest.get('contract') == 'dots2codex-global-direct-package/1'
            and isinstance(manifest.get('files'), dict), 'invalid_package_manifest')
    canonical = json.dumps(manifest['files'], sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    require(hashlib.sha256(canonical).hexdigest() == manifest.get('source_tree_sha256'), 'package_manifest_digest_mismatch')
    actual = set()
    for path in root.rglob('*'):
        relative = path.relative_to(root)
        if relative.parts[0] == '.git':
            continue
        require(not path.is_symlink(), 'package_symlink_rejected')
        if '__pycache__' in relative.parts or path.suffix == '.pyc' or path == manifest_path:
            continue
        if path.is_file():
            actual.add(relative.as_posix())
    require(actual == set(manifest['files']), 'package_file_inventory_changed')
    for name, expected in manifest['files'].items():
        relative = Path(name)
        require(not relative.is_absolute() and '..' not in relative.parts, 'unsafe_manifest_path')
        path = root / relative
        require(path.is_file() and path.stat().st_size == expected['bytes'], 'package_file_changed')
        require(hashlib.sha256(path.read_bytes()).hexdigest() == expected['sha256'], 'package_file_changed')
        require(format(stat.S_IMODE(path.stat().st_mode), '04o') == expected['mode'], 'package_file_mode_changed')
    return manifest.get('source_tree_sha256')


def prerequisites(python):
    try:
        result = subprocess.run([str(python), '-c',
            "import sys,importlib.metadata; import mcp,anyio,jsonschema,tomlkit,PIL; "
            "raise SystemExit(sys.version_info < (3,11) or importlib.metadata.version('mcp') != '1.29.0' "
            "or tomlkit.__version__ != '0.13.3' or PIL.__version__ != '12.3.0')"], env=base_environment(), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def choose_python(state, ui):
    private = state / '.venv/bin/python'
    for python in (private, Path(sys.executable).absolute()):
        if prerequisites(python):
            return str(python)
    require(not (state / '.venv').exists(), 'incomplete_private_environment_preserved')
    if not ui.confirm(f'首次使用需要在 {state / ".venv"} 安装 mcp 1.29.0、tomlkit 0.13.3、Pillow 12.3.0 及依赖。\n'
                      '仅从 PyPI 下载到这个私有 Python 环境。是否安装？', 'INSTALL'):
        raise Cancelled()
    private_dir(state, create=True)
    env = base_environment()
    env['PIP_CONFIG_FILE'] = os.devnull
    try:
        subprocess.run([sys.executable, '-m', 'venv', '--copies', str(state / '.venv')], check=True,
                       env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([str(private), '-m', 'pip', '--isolated', 'install', '--disable-pip-version-check',
                        '--index-url', 'https://pypi.org/simple', '-r', str(ROOT / 'requirements-global.txt')],
                       check=True, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        raise ProtocolError('private_dependency_install_failed_preserved_for_review') from None
    require(prerequisites(private), 'private_dependency_check_failed')
    return str(private)


def ensure_private_dependencies(state, settings, ui):
    """An explicit upgrade may modify only our owned private environment.

    Never pip-install into a shared/system Python or update a running service.
    Existing settings, credentials, profile and global config stay untouched.
    """
    python = Path(settings['python']).absolute()
    if prerequisites(python):
        return
    private = Path(state) / '.venv/bin/python'
    require(python == private.absolute(), 'dependency_upgrade_requires_owned_private_environment')
    require(private.is_file() and not private.is_symlink(), 'private_dependency_environment_missing')
    no_symlinks(private)
    require(private.stat().st_uid == os.getuid(), 'private_dependency_environment_owner_mismatch')
    with private_lock(Path(state) / 'lifecycle.lock'):
        launcher = Launcher(state, ui=ui)
        require(not any(alive(record.get('pid')) for record in launcher.records(launcher.current()).values()),
                'dependency_upgrade_requires_explicit_stop')
        if prerequisites(python):
            return
        if not ui.confirm(f'此 Direct 私有环境需要升级依赖：mcp 1.29.0、tomlkit 0.13.3、Pillow 12.3.0。\n'
                          f'仅从 PyPI 安装到 {private.parent.parent}；保留原设置、凭据、profile 和队列。\n'
                          '不会修改系统 Python。是否升级？', 'UPGRADE'):
            raise Cancelled()
        env = base_environment(); env['PIP_CONFIG_FILE'] = os.devnull
        try:
            subprocess.run([str(python), '-m', 'pip', '--isolated', 'install', '--disable-pip-version-check',
                            '--index-url', 'https://pypi.org/simple', '-r', str(ROOT / 'requirements-global.txt')],
                           check=True, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            raise ProtocolError('private_dependency_upgrade_failed_preserved_for_review') from None
        require(prerequisites(python), 'private_dependency_check_failed')


def existing_tunnel_binary():
    candidates = [os.environ.get('TUNNEL_CLIENT_BIN'), shutil.which('tunnel-client'),
                  '/opt/homebrew/bin/tunnel-client', '/usr/local/bin/tunnel-client']
    for item in candidates:
        if item and Path(item).is_file() and os.access(item, os.X_OK):
            binary = str(Path(item).absolute())
            # Actual binary capabilities win over an assumed version number.
            for command, flags in (('init', ('--profile-dir', '--mcp-command', '--health-listen-addr')),
                                   ('run', ('--profile-dir', '--profile'))):
                result = subprocess.run([binary, command, '--help'], capture_output=True, text=True,
                                        env=base_environment(), stdin=subprocess.DEVNULL, timeout=10)
                require(result.returncode == 0 and all(flag in result.stdout + result.stderr for flag in flags),
                        'tunnel_client_required_flags_missing')
            return binary
    raise ProtocolError('official_tunnel_client_missing')


def existing_profile_hint(home=None):
    home = Path.home() if home is None else Path(home)
    directory = home / '.config/tunnel-client'
    if not directory.is_dir() or directory.is_symlink():
        return ''
    candidates = sorted(directory.glob('dots-direct-*.yaml'))
    return str(candidates[0]) if len(candidates) == 1 and not candidates[0].is_symlink() else ''


def reviewed_tunnel_id(value):
    """Extract only an ID; never adopt commands, headers, keys or Lean profiles."""
    if re.fullmatch(r'tunnel_[a-z0-9]{32}', value):
        return value
    path = no_symlinks(value)
    require(path.name.startswith('dots-direct-') and path.suffix == '.yaml', 'dedicated_direct_profile_required')
    require(path.is_file() and path.stat().st_uid == os.getuid() and path.stat().st_size <= 65536,
            'invalid_existing_profile')
    raw = path.read_text()
    found = re.findall(r'^\s+tunnel_id:\s*[\"\']?(tunnel_[a-z0-9]{32})[\"\']?\s*$', raw, re.M)
    require(len(found) == 1, 'existing_profile_tunnel_id_unreadable')
    return found[0]


def setup(state, ui=None):
    ui = ui or UI()
    require(sys.stdin.isatty(), 'interactive_first_use_required')
    state = no_symlinks(state)
    if settings_path(state).exists():
        settings = load_settings(state)
        ensure_private_dependencies(state, settings, ui)
        from direct_bridge.global_credentials import prepare_credentials
        return prepare_credentials(settings['credential_dir'], interactive=True)
    package_sha256 = verify_package()
    binary = existing_tunnel_binary()
    source = ui.text('现有 Direct 专用 tunnel ID，或它的 dots-direct-*.yaml 路径', existing_profile_hint())
    tunnel_id = reviewed_tunnel_id(source)
    home = Path(ui.text('CLI 与 Desktop 共用的 CODEX_HOME', os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))).expanduser()
    from dots_lite.config_transaction import known_home
    home = known_home(home)
    credential_dir = no_symlinks(ui.text('仅用于现有本机 env / http-bearer 的私有目录', str(DEFAULT_CREDENTIALS)))
    http_port, admin_port = 18765, 18766
    require(port_available(http_port) and port_available(admin_port), 'service_port_occupied_nothing_started')
    if not ui.confirm(f'使用现有专用隧道 {tunnel_id}，在\n{state}\n保存共享 Direct 服务设置和新私有 profile。\n'
                      f'仅读取和复用 {credential_dir} 中的现有凭据；保存缺失值另行确认。\n'
                      '旧 profile 和 Lean 不会修改；旧 Direct tunnel/bridge 须已正常停止。\n'
                      '启动只运行 MCP、官方隧道和本地 Responses 服务，不会启动模型或原生控制器。\n'
                      '本机服务设置长期保留，直到你停止；原生任务仍须逐项明确接入，最多 8 条路由、每条 128 次请求。\n'
                      '确认这个现有隧道仅供你自己的 Direct 工作区使用？', 'SETUP'):
        raise Cancelled()
    private_dir(state, create=True)
    python = choose_python(state, ui)
    from direct_bridge.global_credentials import prepare_credentials
    credential = prepare_credentials(credential_dir, interactive=True)
    with private_lock(state / 'lifecycle.lock'):
        require(not settings_path(state).exists(), 'setup_changed_retry_start')
        bridge = private_dir(state / 'bridge', create=True)
        config_path = bridge / 'config.json'
        # Incomplete setup can be resumed, but durable configuration is immutable.
        if config_path.exists():
            config = read(config_path)
        else:
            from direct_bridge.facade.global_runtime import default_config
            config = default_config(bridge)
            save(config_path, config)
        config_id = config['config_id']
        profile_dir = private_dir(state / 'tunnel-profile', create=True)
        profile_path = profile_dir / 'direct-global.yaml'
        command = mcp_command(state, python, http_port)
        if profile_path.exists():
            # A prior interrupted creation is never overwritten or silently adopted.
            raise ProtocolError('incomplete_profile_exists_review_before_retry')
        if not ui.confirm(f'创建私有本地 profile: {profile_path}\n复用 tunnel ID: {tunnel_id}\n'
                          f'MCP 命令（无密钥）: {command}\n'
                          f'管理端口仅监听 127.0.0.1:{admin_port}。API key 仅引用环境变量。\n'
                          '这不创建远端 tunnel、API key 或新权限。', 'PROFILE'):
            raise Cancelled()
        try:
            subprocess.run([binary, 'init', '--sample', 'sample_mcp_stdio_local', '--profile', 'direct-global',
                            '--profile-dir', str(profile_dir), '--tunnel-id', tunnel_id, '--mcp-command', command,
                            '--health-listen-addr', f'127.0.0.1:{admin_port}',
                            '--control-plane-api-key-ref', 'env:CONTROL_PLANE_API_KEY'],
                           env=base_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            raise ProtocolError('profile_creation_failed_existing_files_preserved') from None
        require(profile_path.is_file() and not profile_path.is_symlink(), 'profile_creation_not_verified')
        os.chmod(profile_path, 0o600)
        # The generated file is private and identity-pinned; no arbitrary profile
        # overrides or inherited tunnel environment will be supplied to run.
        settings = {'contract': CONTRACT, 'python': python, 'http_port': http_port, 'admin_port': admin_port,
                    'tunnel_binary': binary, 'profile_dir': str(profile_dir), 'profile': 'direct-global',
                    'profile_sha256': hashlib.sha256(read_private_file(profile_path, 65536)).hexdigest(),
                    'codex_home': str(home), 'config_id': config_id,
                    'bundle_root': str(BUNDLE_ROOT), 'package_sha256': package_sha256,
                    'credential_dir': str(credential_dir)}
        save(settings_path(state), settings)
    ui.say('首次设置已保存；尚未更改 Codex 全局配置。以后双击 DIRECT.command 选择 Start，再选单会话或全局；请求和动作记录会跨重启保留。')
    return credential


def print_status(value, ui):
    if not value:
        return
    ui.say('服务状态: ' + value.get('stage', value.get('phase', '完成')))
    if value.get('stage') == 'RUNNING' and value.get('ready') is False:
        ui.say('已有本工具的服务进程，但本次未验证完整就绪状态；未发出模型请求。')
    elif value.get('stage') == 'RUNNING':
        ui.say('MCP + 官方隧道 + 本地 Responses 已启动。')
        if value.get('configuration_scope') == 'session':
            ui.say('本次为单会话 CLI；全局配置保持原样。退出 CLI 不会停止共享服务。')
        elif value.get('configuration_scope') == 'global':
            ui.say('全局配置已启用；已有 Codex 窗口请正常退出后重开。')
        ui.say('这不证明实际原生控制器已接入；需要在 dot 中明确启动/继续相应任务。')
    if value.get('stage') == 'SESSION_EXITED':
        ui.say('本次 CLI 已退出（退出码 ' + str(value['exit_code']) + '）。共享服务和原有全局配置保持原样。')
    if value.get('stage') == 'STOPPED':
        ui.say('本机服务已停止；请求、未知动作和恢复记录均保留。原生任务须在 dot 中单独停止。')
    if value.get('stage') == 'FAILED':
        ui.say('启动失败: ' + value.get('error', 'unknown'))
    configuration = value.get('configuration', {})
    if configuration.get('active_transaction'):
        ui.say('存在本工具的全局配置/恢复记录；Stop 或 Restore 可恢复，冲突时不会强行覆盖。')
    if value.get('active_session_clients'):
        ui.say('已记录的活动单会话 CLI: ' + str(value['active_session_clients']) + '；Stop 会中断它们使用的共享服务。')


def print_diagnostics(value, ui):
    ui.say('Direct sanitized diagnostics v1')
    ui.say('Recorded service stage: ' + value['stage'] + ' (snapshot only; readiness not probed)')
    for role in ('owner', 'tunnel', 'bridge'):
        process = value['processes'].get(role)
        if process is not None:
            ui.say(role + ': running=' + str(process['running']).lower() + ' owned=' + str(process['owned']).lower())
    if value['local_status'] != 'available':
        ui.say('Local process status unavailable; no service action was taken.')
    ui.say('Retention: latest two 256 KiB event files (512 KiB total); oldest events rotate out, no age-based purge.')
    ui.say('No payloads, tool arguments/results, tokens, raw IDs, or tunnel stdout/stderr. ID references use a shared local salt across restarts.')
    if value['status'] == 'not_available':
        ui.say('No diagnostic events available. Older running versions produce none; reading does not start or restart a service.')
    elif value['status'] in {'unavailable', 'invalid_line_limit'}:
        ui.say('Diagnostic events unavailable or unsafe to read; no files were changed.')
    elif value['status'] == 'partial':
        ui.say('Partial snapshot: unsafe, oversized, or malformed records were omitted.')
    for event in value['events']:
        ui.say(json.dumps(event, sort_keys=True, separators=(',', ':')))
    ui.say('coroutine_cancelled does not prove a user clicked Cancel; socket_flushed does not prove client receipt or execution.')


def help_text(ui):
    ui.say('DIRECT：单会话 / 全局\n'
           '  session  启动新 CLI，仅本次 provider 覆盖；不改全局配置\n'
           '  global   启动共享服务并启用全局 Codex CLI/Desktop 配置\n'
           '  start    global 的兼容命令；明确选择全局模式\n'
           '  stop     只停止本工具拥有的本机进程，并恢复全局配置\n'
           '  restore  单独恢复配置，不需要 Google、隧道或密钥\n'
           '  status   查看本地状态；不启动模型，不触发请求\n'
           '  diagnostics  只读最近脱敏事件；--lines 1–200（默认 80），不重启服务\n'
           '  setup    首次引导：现有专用 tunnel、私有依赖和本机 env\n'
           '  desktop  打开你选择的已安装 Desktop app，不强制关闭现有窗口\n'
           '  codex    在选定项目启动现有正常 Codex CLI，不限单次会话\n'
           '  help     本说明\n\n'
           '双击 DIRECT.command，选择 Start 后再选单会话或全局。Python >=3.11。--state-dir 可指定私有状态目录。\n'
           '单会话需要 codex-cli 0.159.2，使用 --no-daemon；不改变现有安全设置。\n'
           '单会话仅限定本次 CLI 的配置覆盖；共享后端保持运行直到明确 Stop，不隔离其他已启用的全局客户端。\n'
           '首次关键变更逐项确认；之后同一设置一键启动。不会自动开机启动或重放未知动作。\n'
           'CLI/Desktop 共用选定 CODEX_HOME；现有窗口需正常退出并重开，新建会话。\n'
           '不要为重试删除数据库。不要绕过 macOS 安全提示。官方 tunnel 安装: ' + GUIDE)



def open_desktop(ui):
    require(sys.platform == 'darwin', 'desktop_launch_requires_macos')
    choices = [base / name for base in (Path('/Applications'), Path.home() / 'Applications')
               for name in ('ChatGPT.app', 'Codex.app') if (base / name).is_dir()]
    if len(choices) == 1:
        app = choices[0]
    else:
        if choices:
            ui.say('已找到: ' + ', '.join(str(path) for path in choices))
        app = Path(ui.text('要打开的已安装 .app 完整路径', str(choices[0]) if choices else '')).expanduser()
    require(app.is_absolute() and app.is_dir() and app.suffix == '.app', 'existing_desktop_app_required')
    # Open does not close, restart or signal an existing application.
    result = subprocess.run(['/usr/bin/open', '-a', str(app)], env=base_environment(),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    require(result.returncode == 0, 'desktop_open_failed')
    ui.say('已请求打开 app。请使用新会话；打开成功不代表该版本已读取全局 provider。')


def launch_codex(ui, settings=None):
    from direct_bridge.session_client import existing_codex
    binary = existing_codex()
    project = Path(ui.text('Codex 项目目录', str(Path.home()))).expanduser()
    require(project.is_absolute() and project.is_dir(), 'existing_project_directory_required')
    # Normal client behavior, global settings and existing user safety settings.
    # No session marker, forced trial flags, hidden wake or model API call.
    env = base_environment()
    if settings is not None:
        env['CODEX_HOME'] = settings['codex_home']
    return subprocess.call([binary, '-C', str(project)], env=env, cwd=str(project))


def choose_mode(ui):
    while True:
        ui.say('启动方式：1 单会话 CLI（不改全局配置）   2 全局 CLI/Desktop（需 APPLY 授权）   0 取消')
        choice = ui.text('选择模式', '1')
        if choice in ('0', 'cancel', 'exit'):
            raise Cancelled()
        if choice in ('1', 'session'):
            return 'session'
        if choice in ('2', 'global'):
            return 'global'
        ui.say('请选择 0、1 或 2')

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=('start', 'global', 'session', 'stop', 'restore', 'status', 'diagnostics', 'setup', 'desktop', 'codex', 'help', '_serve'))
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--runtime', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--lines', type=int, default=80, choices=range(1, 201), metavar='1-200', help='Maximum recent sanitized diagnostic events (default 80)')
    cli_args = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(cli_args)
    ui = UI()
    try:
        launcher = Launcher(args.state_dir, ui=ui)
        state = launcher.state
        if args.command == 'help':
            help_text(ui)
            return 0
        if args.command == 'diagnostics':
            print_diagnostics(launcher.diagnostics(args.lines), ui)
            return 0
        if args.command == '_serve':
            require(args.runtime is not None, 'runtime_required')
            return supervise(state, args.runtime)
        # Prefer the already prepared interpreter, including offline restore. No
        # install/credential check is imposed by status, stop or restore.
        if settings_path(state).exists():
            try:
                settings = load_settings(state)
            except Exception:
                if args.command not in ('stop', 'restore', 'status'):
                    raise
                settings = None
            if settings is not None:
                selected = Path(settings['python'])
                if selected.is_file() and Path(sys.executable).absolute() != selected.absolute():
                    os.execve(str(selected), [str(selected), '-B', str(ROOT / 'global_launcher.py'), *cli_args], base_environment() | {k: os.environ[k] for k in _SECRET_NAMES if k in os.environ})
        def action(command):
            if command == 'desktop':
                open_desktop(ui)
                return {}
            if command == 'codex':
                settings = load_settings(state) if settings_path(state).exists() else None
                return {'exit_code': launch_codex(ui, settings)}
            if command in ('start', 'global', 'session', 'setup'):
                require(sys.stdin.isatty(), 'interactive_start_required')
                client = None
                if command == 'session':
                    from direct_bridge.session_client import preflight
                    client = preflight(ui)
                package_sha256 = verify_package()
                credential = setup(state, ui)
                if command == 'setup':
                    return {'stage': 'CONFIGURED'}
                settings = load_settings(state)
                require(settings['bundle_root'] == str(BUNDLE_ROOT),
                        'bundle_changed_review_new_profile_before_start')
                require(prerequisites(settings['python']), 'private_dependencies_unavailable')
                if Path(sys.executable).absolute() != Path(settings['python']).absolute():
                    env = credential.tunnel_env(base_environment())
                    if 'CODEX_BIN' in os.environ:
                        env['CODEX_BIN'] = os.environ['CODEX_BIN']
                    os.execve(settings['python'], [settings['python'], '-B', str(ROOT / 'global_launcher.py'),
                                                  command, '--state-dir', str(state)], env)
                if command == 'session':
                    return launcher.session(credential, client, package_sha256=package_sha256)
                outcome = launcher.start(credential, package_sha256=package_sha256)
                print_status(outcome, ui)
                if sys.platform == 'darwin' and ui.confirm('现在打开已安装的 Desktop app？若它已运行，请先正常退出再打开，避免保留旧 provider。', 'OPEN'):
                    open_desktop(ui)
                return {}
            if command == 'stop':
                if launcher.active_session_clients(launcher.current()) and not ui.confirm(
                        'Stop 会中断所有共用本服务的单会话 CLI 和全局客户端的后续请求，并恢复全局配置。\n'
                        '不会关闭 CLI/Desktop 窗口或停止原生任务。确认停止共享服务？', 'STOP'):
                    raise Cancelled()
                return launcher.stop()
            if command == 'restore':
                require(sys.stdin.isatty(), 'interactive_restore_required')
                return launcher.restore()
            if command == 'status':
                return launcher.status()
            help_text(ui)
            return {}
        if args.command:
            result = action(args.command)
            print_status(result, ui)
            return result.get('exit_code', 0)
        require(sys.stdin.isatty(), 'interactive_menu_required')
        while True:
            ui.say('\nDIRECT   1 Start（选择模式）   2 Stop + Restore   3 Status   4 Restore only   5 Help   6 Open Desktop   7 普通 Codex CLI   0 Exit')
            choice = ui.text('选择', '1')
            if choice in ('0', 'exit', 'quit'):
                return 0
            command = {'1': 'start', '2': 'stop', '3': 'status', '4': 'restore', '5': 'help', '6': 'desktop', '7': 'codex'}.get(choice, choice)
            if command not in ('start', 'global', 'session', 'stop', 'status', 'restore', 'help', 'setup', 'desktop', 'codex'):
                ui.say('请选择 0–7')
                continue
            try:
                if choice == '1':
                    command = choose_mode(ui)
                print_status(action(command), ui)
            except Cancelled:
                ui.say('已取消；已有配置和请求记录保留。')
            except Exception as error:
                ui.say('未完成: ' + safe_error(error) + '。已有状态保留；可选择 Status 或 Restore。')
    except (Cancelled, KeyboardInterrupt, EOFError):
        ui.say('已取消；已有配置和请求记录保留。')
        return 130
    except Exception as error:
        ui.say('未完成: ' + safe_error(error) + '。已有状态保留；可运行 status 或 restore。')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
