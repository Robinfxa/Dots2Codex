"""Mac desktop Global gateway and explicit single-session Router controls.

Ports, Global backend, and UI are injectable for headless offline tests. Each
backend owns its mutations, admission/probe gates, lifecycle, and exact cleanup.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
from . import router_mac as router
from .backend import read_private_file
from .mac_environment import Environment, private_lock, verify_package
from .mac_setup import (absolute_path, no_symlinks, private_directory, credential_candidates,
                        folder_id, validate_local, validate_google, validate_credential)
from .mac_ui import UI, Cancelled
from .model import require, ProtocolError
from .selection import load_catalog, select
from .session import _save

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = Path.home() / '.config/dots2codex-launcher'
CODEX_INSTRUCTIONS = 'https://github.com/openai/codex'
REUSE_NOTICE = ('Reuse this Mac\'s existing authorized-user file for Google access?\n'
    'The recorded drive.readonly scope can read broadly across your Drive, beyond this folder. '
    'The launcher only checks the exact folder. The selected routing mode asks separately '
    'before creating its control Docs and probes there. '
    'No new OAuth, scope expansion, token copying, sharing changes, or Cloud project creation.\n')
MENU_ACTIONS = {
    'Start Global desktop routing': 'global-start',
    'Global status': 'global-status',
    'Stop Global routing': 'global-stop',
    'Restore Global config': 'global-restore',
    'Settings': 'settings',
    'Start single-session Router': 'single-start',
    'Single-session status': 'single-status',
    'Stop single-session Router': 'single-stop',
    'Quit': 'quit',
}
OPERATION_ALIASES = {'start':'single-start', 'status':'single-status',
                     'stop':'single-stop', 'restore-global':'global-restore'}


def choose_operation(ui):
    selected = ui.choose('Dots2Codex desktop routing', MENU_ACTIONS)
    operation = MENU_ACTIONS[selected]
    if operation == 'quit': raise Cancelled()
    return operation


def desktop_global(**kwargs):
    # Optional Global dependencies must not make single-session recovery or
    # opening/cancelling the launcher menu depend on a Global installation.
    from .global_desktop import DesktopGlobal
    return DesktopGlobal(**kwargs)


def safe_error(exc):
    value = str(exc)
    return value if isinstance(exc, (ProtocolError, RuntimeError, ValueError)) and re.fullmatch('[a-z][a-z0-9_]{1,120}', value) else type(exc).__name__


def default_config(folder, credential, workdir, codex):
    return {'folder_id':folder, 'authorized_user_file':str(credential), 'workdir':str(workdir),
        'mac_writer_identity':'mac-controller-router','worker_writer_identity':'remote-worker-router',
        'seconds':14400,'max_requests':128,'scope':'responses_tools','port':0,'deadline':1800,
        'poll_interval':5,'heartbeat_interval':15,'bootstrap_ttl':1800,'bootstrap_poll_interval':5,
        'codex':str(codex),'expected_codex_version':router.EXPECTED_CODEX}


class Ports:
    def __init__(self, *, run=subprocess.run): self.run = run

    def verify_codex(self, path):
        candidate = str(path)
        if not Path(candidate).is_absolute():
            candidate = shutil.which(candidate) or ''
        require(bool(candidate) and Path(candidate).is_file() and os.access(candidate, os.X_OK), 'launcher_codex_not_found')
        result = self.run([candidate, '--version'], capture_output=True, text=True, timeout=10, check=False)
        require(result.returncode == 0 and result.stdout.strip() == router.EXPECTED_CODEX,
                'codex_version_requires_live_acceptance')
        help_result = self.run([candidate, '--help'], capture_output=True, text=True, timeout=10, check=False)
        require(help_result.returncode == 0 and all(x in help_result.stdout for x in
            ['--config','--model','--cd','--sandbox','--ask-for-approval']), 'launcher_codex_flags_missing')
        return str(absolute_path(candidate))

    def google(self, config): return validate_google(config)
    def copy(self, text): return router._copy_clipboard(text)
    def start(self, args): return router.start(args)
    def stop(self, args): return router.stop(args)

    def active_status(self, active):
        owned = router._owned_process(active)
        out = {'stage':active.get('stage'), 'facade_alive':router._pid_alive(active.get('facade_pid')),
               'facade_owned':owned, 'closed':active.get('closed') is True,
               'process_stopped':active.get('process_stopped') is True,
               'router_ready':False, 'worker_stop_confirmed':False,
               'underlying_model_verified':False, 'mode':'single-session'}
        if active.get('stage') == 'READY' and owned and not router._cancelled(active):
            try:
                ready = router._load_private_json(active['ready_file'])
                require(ready.get('pid') == active['facade_pid'] and ready.get('deployment') == active['deployment'] and
                    ready.get('session_control') == 'docs_cas' and ready.get('expires') == active['pin_expires'] and
                    ready.get('selection') == active.get('model_selection'), 'launcher_ready_binding_mismatch')
                code, body = router._bridge_call(ready, active['controller_journal'], 'status')
                out['router_ready'] = code == 200 and body.get('closed') is False and body.get('deployment') == active['deployment'] and body.get('expires', 0) > time.time() and body.get('selection') == active.get('model_selection')
            except Exception: out['readiness_check'] = 'unavailable'
        return out


class Launcher:
    def __init__(self, *, root=ROOT, state=DEFAULT_STATE, config=router.DEFAULT_CONFIG,
                 active=router.DEFAULT_ACTIVE, ui=None, ports=None, global_factory=None,
                 global_ports=None):
        self.root, self.state = Path(root), no_symlinks(state)
        self.config, self.active = no_symlinks(config), no_symlinks(active)
        self.ui, self.ports = ui or UI(), ports or Ports()
        self.global_factory, self.global_ports = global_factory or desktop_global, global_ports
        self.consent = self.state / 'approved-config.json'

    def global_backend(self):
        kwargs = {'root':self.root, 'state':self.state / 'global',
                  'config':self.config, 'ui':self.ui}
        if self.global_ports is not None: kwargs['ports'] = self.global_ports
        return self.global_factory(**kwargs)

    def load(self):
        if not self.config.exists(): return None
        no_symlinks(self.config)
        return router._validate_config(json.loads(read_private_file(self.config, 131072)))

    def active_record(self):
        if not self.active.exists(): return None
        no_symlinks(self.active)
        return json.loads(read_private_file(self.active, 131072))

    def active_blocks(self):
        active = self.active_record()
        if active is None: return False
        # A forged or incomplete stage is never treated as a clean restart.
        return not ((active.get('closed') is True and active.get('process_stopped') is True) or
                    (active.get('stage') == 'ABORTED' and active.get('process_stopped') is True))

    def _approval(self, config):
        st = no_symlinks(config['authorized_user_file']).stat()
        return {'contract':1, 'config_sha256':hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
                'credential_identity':[st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns]}

    def approved(self, config):
        try: return json.loads(read_private_file(self.consent, 32768)) == self._approval(config)
        except Exception: return False

    def reuse(self, config):
        if not self.approved(config):
            if not self.ui.confirm(REUSE_NOTICE + config['authorized_user_file']): raise Cancelled()
        validate_credential(config['authorized_user_file'])

    def _pick_codex(self, candidate):
        candidates = [candidate, shutil.which('codex'), '/opt/homebrew/bin/codex', '/usr/local/bin/codex',
                      '/Applications/Codex.app/Contents/Resources/codex']
        seen = set()
        for path in candidates:
            if not path or str(path) in seen: continue
            seen.add(str(path))
            try: return self.ports.verify_codex(path)
            except Exception: continue
        self.ui.notify('Official codex-cli 0.159.2 with the required flags was not found.\n'
                       'Install it yourself using the official instructions, or select an already installed executable.\n' + CODEX_INSTRUCTIONS)
        return self.ports.verify_codex(self.ui.file('Select an existing official codex executable'))

    def configure(self, args):
        require(not self.active_blocks(), 'launcher_active_session_stop_before_settings')
        original = read_private_file(self.config, 131072) if self.config.exists() else None
        prior = self.load() or {}
        pair = router._resolve_selection(args, {})  # reject incomplete/unsupported flags before prompts/mutations
        candidates = credential_candidates(prior)
        supplied = getattr(args, 'credentials', None)
        if supplied: credential = absolute_path(supplied)
        elif candidates:
            options = [str(x) for x in candidates] + ['Select another file']
            choice = self.ui.choose('Choose this Mac\'s existing authorized-user file', options)
            credential = absolute_path(self.ui.file('Select this Mac\'s authorized-user file') if choice == options[-1] else choice)
        else: credential = absolute_path(self.ui.file('Select this Mac\'s existing authorized-user file'))
        if not self.ui.confirm(REUSE_NOTICE + str(credential)): raise Cancelled()
        validate_credential(credential)
        folder = folder_id(getattr(args, 'folder_id', None) or self.ui.text('Dedicated Google Drive folder ID (or exact folder URL)', prior.get('folder_id', '')))
        work = no_symlinks(getattr(args, 'workdir', None) or self.ui.folder('Choose an existing Codex workspace', prior.get('workdir', str(Path.home()))))
        codex = self._pick_codex(getattr(args, 'codex', None) or prior.get('codex'))
        if pair is None:
            catalog = load_catalog(getattr(args, 'catalog', None))
            model = self.ui.choose('Choose the native model for NEW sessions (availability requires actual admission)', catalog['models'])
            effort = self.ui.choose('Choose reasoning effort (fixed after pairing)', catalog['models'][model]['bridge_efforts'])
            pair = select(catalog, model, effort)
        config = {**(prior or default_config(folder, credential, work, codex)),
                  'folder_id':folder, 'authorized_user_file':str(credential), 'workdir':str(work),
                  'codex':codex, 'expected_codex_version':router.EXPECTED_CODEX, 'model_selection':pair}
        if getattr(args, 'catalog', None): config['catalog'] = str(no_symlinks(args.catalog))
        validate_local(config)
        print('[launcher] Checking exact Google folder metadata (read only)...', flush=True)
        self.ports.google(config)  # read-only; no workspace/config mkdir before this succeeds
        summary = ('Save these routing settings and defaults for new sessions?\n'
            f'Config: {self.config}\nCredential path: {credential}\nFolder ID: {folder}\nWorkspace: {work}\n'
            f'Codex: {codex}\nModel: {pair["model"]}\nEffort: {pair["reasoning_effort"]}\n'
            'Folder preflight passed; bidirectional pairing is still pending. '
            'These settings provide defaults for new single-session and Global desktop sessions. '
            'Global Codex config changes require a separate exact-diff confirmation.')
        if not self.ui.confirm(summary): raise Cancelled()
        private_directory(self.state, create=True)
        private_directory(self.config.parent, create=True)
        with private_lock(Path(str(self.config) + '.settings.lock')), router._lifecycle_lock(self.active):
            require(not self.active_blocks(), 'launcher_active_session_stop_before_settings')
            latest = read_private_file(self.config, 131072) if self.config.exists() else None
            require(latest == original, 'launcher_config_changed_retry_settings')
            _save(self.config, config)
            _save(self.consent, self._approval(config))
        return {'configured':True,'folder_verified':True,'router_ready':False,
                'bidirectional_access_verified':False,'model':pair['model'],'effort':pair['reasoning_effort'],
                'credentials_copied':False,'global_config_changed':False}

    def copy_join(self, active, message=None):
        if message is None:
            require(active.get('stage') == 'WAITING_FOR_WORKER', 'launcher_join_not_pending')
            path = no_symlinks(active['join_message_file'])
            require(path.parent == no_symlinks(active['runtime']) and path.name == 'join-message.txt', 'launcher_join_path_mismatch')
            message = read_private_file(path, 32768).decode('utf-8')
        if not self.ui.confirm('Copy this session\'s private Router join message to the clipboard?\nSend it only in your private Dots conversation, then clear your clipboard. No OAuth or token is included.'):
            raise Cancelled()
        copied = self.ports.copy(message)
        if copied: self.ui.notify('Private join message copied. Send it to Dots once. Waiting for actual native admission and bidirectional checks; the Router is not ready yet.')
        else: self.ui.notify('Clipboard copy failed. The private join message is preserved at:\n' + active['join_message_file'] + '\nOpen it locally and send only in your private Dots conversation. Pairing remains pending.')
        return {'join_copied':copied,'router_ready':False}

    def status(self):
        active = self.active_record()
        if active is None: return {'configured':self.config.exists(),'stage':'NOT_STARTED','router_ready':False,'mode':'single-session'}
        return self.ports.active_status(active)

    def _active_menu(self, args):
        active = self.active_record()
        choices = ['Status', 'Stop', 'Cancel']
        if active.get('stage') == 'WAITING_FOR_WORKER': choices.insert(0, 'Copy current join')
        choice = self.ui.choose('An active or incomplete Router session already exists. No new Docs will be created.', choices)
        if choice == 'Cancel': raise Cancelled()
        if choice == 'Copy current join': return self.copy_join(active)
        if choice == 'Stop': return self.stop(args)
        return self.status()

    def start(self, args):
        # Even an offline or stale record blocks replacement. Original router
        # rechecks under its lifecycle lock immediately before cloud mutations.
        if self.active_blocks(): return self._active_menu(args)
        if self.load() is None or self.load().get("model_selection") is None: self.configure(args)
        config_raw = read_private_file(self.config, 131072)
        config = router._validate_config(json.loads(config_raw))
        pair = router._resolve_selection(args, config)
        validate_local(config)
        self.ports.verify_codex(config['codex'])
        self.reuse(config)
        self.ports.google(config)
        require(pair is not None, 'launcher_choose_model_effort_in_settings')
        if not self.ui.confirm('Start a new bounded Router session?\n'
            f'Model: {pair["model"]}; effort: {pair["reasoning_effort"]}\n'
            'Creates one Control Doc, one pairing Doc and a probe in the saved folder. '
            'You will send one private join message to Dots. Actual native admission is required. '
            'The native worker is not automatically started. Global Codex config is not changed.'):
            raise Cancelled()
        # Mark approved existing configuration only after explicit reuse/start consent.
        private_directory(self.state, create=True)
        _save(self.consent, self._approval(config))
        call = SimpleNamespace(**vars(args))
        call.config, call.active = str(self.config), str(self.active)
        call.expected_config_sha256 = hashlib.sha256(config_raw).hexdigest()
        call.join_callback = self.copy_join
        call.launch_codex = not getattr(args, 'no_launch_codex', False)
        return self.ports.start(call)

    def stop(self, args):
        # Do not precheck active existence here: first start may own its lease
        # before publishing active state. router.stop must signal and wait.
        # Stop remains available even with broken config validation/network. The
        # router's authoritative close and exact PID cleanup decide the outcome.
        call = SimpleNamespace(**vars(args)); call.config, call.active = str(self.config), str(self.active)
        result = self.ports.stop(call)
        result['global_config_restore'] = 'not_applicable_single_session'
        return result

    def global_start(self, args):
        config = self.load()
        if config is None or config.get('model_selection') is None: self.configure(args)
        config_raw = read_private_file(self.config, 131072)
        config = router._validate_config(json.loads(config_raw))
        pair = router._resolve_selection(args, config)
        validate_local(config)
        self.reuse(config)
        self.ports.google(config)
        require(pair is not None, 'launcher_choose_model_effort_in_settings')
        call = SimpleNamespace(**vars(args))
        call.config = str(self.config)
        call.expected_config_sha256 = hashlib.sha256(config_raw).hexdigest()
        # The desktop backend obtains bounded cloud and exact config-diff
        # consent. It never calls router.start or opens a sandboxed CLI session.
        result = self.global_backend().start(call)
        private_directory(self.state, create=True)
        _save(self.consent, self._approval(config))
        return result

    def dispatch(self, args):
        operation = OPERATION_ALIASES.get(args.operation, args.operation)
        if operation == 'menu': operation = choose_operation(self.ui)
        if operation in {'quit','cancel'}: raise Cancelled()
        if operation == 'global-start': return self.global_start(args)
        if operation in {'global-status','global-stop','global-restore'}:
            return getattr(self.global_backend(), operation.removeprefix('global-'))(args)
        if operation == 'settings': return self.configure(args)
        if operation == 'single-status': return self.status()
        if operation == 'single-stop': return self.stop(args)
        require(operation == 'single-start', 'launcher_unknown_operation')
        return self.start(args)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation', choices=['menu','global-start','global-status','global-stop','global-restore',
        'settings','single-start','single-status','single-stop', *OPERATION_ALIASES], nargs='?', default='menu')
    p.add_argument('--config', default=str(router.DEFAULT_CONFIG)); p.add_argument('--active', default=str(router.DEFAULT_ACTIVE))
    p.add_argument('--state', default=str(DEFAULT_STATE))
    p.add_argument('--credentials'); p.add_argument('--folder-id'); p.add_argument('--workdir'); p.add_argument('--codex')
    p.add_argument('--codex-home', help='Explicit existing Codex desktop home for Global routing')
    p.add_argument('--desktop-app', help='Advanced: explicit installed .app folder; normally detected automatically')
    p.add_argument('--desktop-codex', help='Legacy strict two-binary verification; explicit engine path only')
    p.add_argument('--model','-m'); p.add_argument('--effort'); p.add_argument('--catalog')
    p.add_argument('--no-launch-codex', action='store_true')
    return p


def main(argv=None):
    args = parser().parse_args(argv); os.umask(0o077)
    require(sys.version_info >= (3,11), 'launcher_python_311_required')
    require((args.model is None) == (args.effort is None), 'model_and_effort_required_together')
    ui = UI()
    verify_package(ROOT)
    launcher = Launcher(state=args.state, config=args.config, active=args.active, ui=ui)
    if args.operation == 'menu':
        args.operation = choose_operation(ui)
    args.operation = OPERATION_ALIASES.get(args.operation, args.operation)
    if args.operation not in {'single-start','global-start','settings'}:
        require(args.model is None and args.effort is None and args.catalog is None,
                'selection_flags_require_new_session_start')
    # Stop never has an installation prerequisite. Prefer a healthy known
    # interpreter, otherwise router.stop still attempts exact local termination
    # and leaves authoritative close unverified if Google SDK/auth is unavailable.
    python = None
    is_global = args.operation.startswith('global-')
    needs_setup = args.operation == 'global-start' or (args.operation in {'single-start','settings'} and not launcher.active_blocks())
    if needs_setup:
        python = Environment(ROOT, args.state, include_global=is_global).ensure(ui)
    elif args.operation in {'single-start','settings','single-stop','global-status','global-stop','global-restore'}:
        try:
            current = Environment(ROOT, args.state, include_global=is_global).current()
            if current: python = current / 'bin/python3'
        except Exception: pass
    if python is not None and absolute_path(sys.prefix) != python.parent.parent:
        forwarded = [args.operation]
        for key in ('config','active','state','credentials','folder_id','workdir','codex','codex_home','desktop_codex','desktop_app','model','effort','catalog'):
            value = getattr(args, key)
            if value is not None: forwarded.extend(['--' + key.replace('_', '-'), value])
        if args.no_launch_codex: forwarded.append('--no-launch-codex')
        child_env = {k:v for k,v in os.environ.items() if k not in {'PYTHONPATH','PYTHONHOME'}}
        child_env['PYTHONNOUSERSITE'] = '1'
        os.execve(str(python), [str(python), '-B', '-m', 'remote_transport.mac_launcher', *forwarded], child_env)
    result = launcher.dispatch(args)
    ui.notify(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result.get('stage') == 'RECOVERY_REQUIRED' else 0


def cli():
    try: return main()
    except (Cancelled, KeyboardInterrupt):
        print('Cancelled. Existing configuration and session evidence are preserved.', flush=True); return 130
    except Exception as exc:
        print(json.dumps({'error':safe_error(exc),'router_ready':False,'retry_remote_mutations':False}), flush=True)
        return 1


if __name__ == '__main__': raise SystemExit(cli())
