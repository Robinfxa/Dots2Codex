"""Desktop Global lifecycle: real supervisor, native JOIN, preflight, reviewed config.

No native agent is created by Python. The user sends the GLOBAL controller JOIN
once; an active native controller owns per-thread child admission. All Google
effects and config writes occur only after their separate displayed consents.
"""
from __future__ import annotations
import argparse
import concurrent.futures
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time

from . import global_config as config_tx, router_mac as router
from . import codex_desktop as app_identity
from .backend import read_private_file
from .global_gateway import Store, Gateway, DEFAULT_PORT, private_dir, private_write, probe, strict_json
from .mac_environment import private_lock
from .mac_setup import no_symlinks
from .mac_ui import Cancelled
from .model import ProtocolError, canonical, hash_bytes, require

CONTRACT = 'dots-desktop-global/1'
MAX = 2 * 1024 * 1024
READ_RECOVERY_DELAYS = (1, 2, 4, 8, 15)


def read(path, maximum=MAX):
    return strict_json(read_private_file(path, maximum))


def save(path, value):
    private_write(path, canonical(value))


def safe_error(exc):
    import re
    value = str(exc)
    return value if isinstance(exc, (ProtocolError, ValueError, RuntimeError)) and re.fullmatch(r'[a-z][a-z0-9_]{1,160}', value) else type(exc).__name__


class _PreparationReads:
    """One Drive caller, with owner-only Docs recovery and a drained pause fence."""
    def __init__(self, docs, bridge):
        self.docs, self.bridge = docs, bridge
        self.cancelled = threading.Event()
        self.pause_requested = threading.Event()
        self.condition = threading.Condition()
        self.state = None; self.snapshot = None
        self.expires = None; self.deadline = None
        if docs.paused: self.pause_requested.set()

    def limit(self, expires):
        require(self.expires is None, 'global_resource_preparation_deadline_already_set')
        self.expires = expires
        self.deadline = self.docs.monotonic() + max(0, expires-self.docs.now())

    def check(self):
        require(not self.cancelled.is_set(), 'global_resource_preparation_cancelled')
        require(self.expires is None or (self.docs.now() < self.expires and self.docs.monotonic() < self.deadline),
                'global_resource_preparation_expired')

    def drive(self, check, function, *args):
        # Worker lifecycle checks never touch the Docs client or its credentials.
        # Holding the gate through each Drive RPC and its receipt makes pause
        # acknowledgment a drain barrier.
        with self.condition:
            while True:
                self.check(); self.docs._check()
                if not self.pause_requested.is_set():
                    check()
                    return function(*args)
                self.condition.wait(.1)

    def pause(self, function):
        # Publish intent before waiting for an in-flight effect. A queued task
        # cannot acquire the gate repeatedly and starve the owner's pause.
        self.pause_requested.set()
        with self.condition: function()

    def resume(self, snapshot):
        with self.condition:
            self.snapshot = snapshot; self.state = snapshot.state
            self.pause_requested.clear()
            self.condition.notify_all()

    def reconcile(self):
        require(threading.get_ident() == self.docs.owner, 'global_docs_recovery_owner_required')
        self.check()
        return self.docs.reconcile(self.bridge)

    def cancel(self):
        self.cancelled.set()
        with self.condition: self.condition.notify_all()


class RecoveringDocs:
    """Retry an exhausted transient GET in place, never its enclosing operation.

    The supervisor may already have issued a one-attempt write before this read.
    Holding that stack preserves exact reconciliation and all no-replay journals.
    Background facade calls retain their normal bounded transport behavior.
    """
    def __init__(self, docs, *, store, runtime, spec, on_pause, on_resume,
                 now=None, monotonic=None, wait=None):
        self.docs, self.store, self.runtime, self.spec = docs, store, Path(runtime), spec
        self.on_pause, self.on_resume = on_pause, on_resume
        self.now, self.monotonic, self.wait = now or time.time, monotonic or time.monotonic, wait or time.sleep
        self.owner = threading.get_ident(); self.token = secrets.token_hex(16)
        self.expires = min(int(spec['expires']), int(store.activation(spec['generation'])['expires']))
        self.last_now = self.now(); self.deadline = self.monotonic() + max(0, self.expires - self.last_now)
        self.paused = False; self.stopping = False
        self.preparation = None; self.check_lock = threading.RLock()
        self.setup_expires = None; self.setup_deadline = None

    @contextmanager
    def preparation_scope(self, bridge):
        require(threading.get_ident() == self.owner and self.preparation is None,
                'global_docs_preparation_owner_required')
        scope = _PreparationReads(self, bridge)
        self.preparation = scope
        try:
            scope.reconcile()
            yield scope
        finally:
            scope.cancel()
            self.preparation = None

    def _check(self):
        # Lifecycle checks also run at worker dispatch. They never touch the
        # Docs client; serialize the clock high-water mark across both callers.
        with self.check_lock: self._check_locked()

    def _check_locked(self):
        if self.preparation is not None: self.preparation.check()
        now = self.now()
        require(now >= self.last_now, 'global_docs_recovery_clock_rollback')
        self.last_now = now
        require(not self.stopping and not (self.runtime / 'stop.json').exists(), 'global_stop_requested')
        root_stop = self.runtime.parent.parent / 'stop-intent.json'
        current_stop = read(root_stop, 32768)['request_id'] if root_stop.exists() else None
        require(current_stop == self.spec.get('stop_intent_id'), 'global_start_cancelled_by_stop')
        require(now < self.expires and self.monotonic() < self.deadline, 'global_docs_recovery_expired')
        require(self.setup_expires is None or (now < self.setup_expires and self.monotonic() < self.setup_deadline),
                'pilot_prewarm_setup_expired')
        require(self.store.activation(self.spec['generation'])['enabled'], 'global_activation_disabled')
        self.store.require_read_recovery_current()

    def get_document(self, document_id):
        from examples.google_clients import DocsReadError
        if self.stopping or threading.get_ident() != self.owner:
            return self.docs.get_document(document_id)
        failures = 0
        while True:
            self._check()
            try:
                value = self.docs.get_document(document_id, deadline=min(self.deadline, self.setup_deadline or self.deadline), check=self._check)
            except DocsReadError as exc:
                if not exc.retryable: raise
                self._check()
                if not self.paused:
                    def pause():
                        self.store.pause_docs_reads(self.spec['generation'], self.token)
                        self.paused = True
                    if self.preparation is None: pause()
                    else: self.preparation.pause(pause)
                    self._check()
                delay = min(READ_RECOVERY_DELAYS[min(failures, len(READ_RECOVERY_DELAYS)-1)],
                            self.expires-self.now(), self.deadline-self.monotonic())
                failures += 1
                self.on_pause(error=safe_error(exc), read_error=exc.diagnostics,
                              retry_round=failures, retry_at=self.now()+delay,
                              activation_expires=self.expires)
                until = self.monotonic() + delay
                while self.monotonic() < until:
                    self._check()
                    self.wait(min(.25, until-self.monotonic()))
                continue
            self._check()  # A late in-flight success cannot authorize later effects.
            return value

    def reconcile(self, bridge):
        """Clear only after fresh authenticated queue and heartbeat observation."""
        if not self.paused: return
        require(threading.get_ident() == self.owner, 'global_docs_recovery_owner_required')
        from . import global_control as queue
        from .global_google import mirror_verified_queue
        self._check()
        snapshot = bridge.read(); state = snapshot.state
        queue.verify(state, bridge.code, expected_root=bridge.source_root)
        require(not state['logical']['closed'], 'global_queue_closed')
        if state['logical']['controller'] is not None:
            mirror_verified_queue(self.store, state, bridge.code)
        self._check()
        self.store.resume_docs_reads(self.spec['generation'], self.token)
        self.paused = False
        self.on_resume()
        if self.preparation is not None: self.preparation.resume(snapshot)
        return state

    def stop_recovery(self):
        self.stopping = True

    def batch_update_document(self, *args, **kwargs):
        if not self.stopping and threading.get_ident() == self.owner:
            if self.preparation is not None: self.preparation.reconcile()
            self._check()
        return self.docs.batch_update_document(*args, **kwargs)


def binary_evidence(path, *, run=subprocess.run):
    invocation = Path(path).expanduser().absolute()
    path = no_symlinks(invocation.resolve(strict=True))
    before = path.stat()
    require(path.is_file() and os.access(path, os.X_OK), 'global_codex_binary_required')
    value = run([str(path), '--version'], capture_output=True, text=True, timeout=10, check=False)
    require(value.returncode == 0 and value.stdout.strip() == config_tx.CODEX_VERSION,
            'global_codex_version_requires_acceptance')
    digest = hash_bytes(path.read_bytes()); after = path.stat()
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), 'global_codex_binary_changed')
    return {'path': str(path), 'invocation_path': str(invocation), 'version': value.stdout.strip(), 'sha256': digest,
            'identity': [after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns]}


def check_binary(evidence):
    path = no_symlinks(evidence['path']); st = path.stat()
    require(Path(evidence.get('invocation_path', evidence['path'])).resolve(strict=True) == path,
            'global_codex_binary_changed')
    require([st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns] == evidence['identity']
            and hash_bytes(path.read_bytes()) == evidence['sha256'], 'global_codex_binary_changed')


def resolve_codex_home(*, explicit=None, saved=None, environ=None, home=None, ui=None):
    """Resolve only known homes; normal startup never opens a folder chooser.

    A prior explicit selection is reused unless the current terminal environment
    contradicts it. A custom terminal home and an existing default desktop home
    need a path choice; an environment variable alone cannot prove the desktop
    uses that same directory. Missing saved paths are stale, unsafe paths are not.
    """
    env = os.environ if environ is None else environ
    default = (Path.home() if home is None else Path(home)) / '.codex'
    def checked(value):
        # Preserve the original traversal for safety validation first. Only
        # then collapse harmless lexical aliases, avoiding duplicate choices.
        return Path(os.path.normpath(str(config_tx.known_home(value))))
    if explicit is not None:
        return checked(explicit)
    saved_path = None
    if saved:
        try: saved_path = checked(saved)
        except FileNotFoundError: pass
    raw_env = env.get('CODEX_HOME')
    env_path = None
    if raw_env:
        require(Path(raw_env).expanduser().is_absolute(), 'global_codex_home_environment_must_be_absolute')
        try: env_path = checked(raw_env)
        except FileNotFoundError:
            raise ProtocolError('global_codex_home_environment_missing') from None
    if saved_path is not None and (env_path is None or env_path == saved_path):
        return saved_path
    default_path = None
    try: default_path = checked(default)
    except FileNotFoundError: pass
    if env_path is None:
        require(default_path is not None, 'global_codex_home_missing_start_codex_once')
        return default_path
    candidates = []
    for path, label in ((saved_path, 'saved setting'), (env_path, 'CODEX_HOME'),
                        (default_path, 'standard Codex location')):
        if path is not None and path not in [p for p, _ in candidates]: candidates.append((path, label))
    if len(candidates) == 1: return candidates[0][0]
    require(ui is not None, 'global_codex_home_conflict_requires_selection')
    choices = [str(path) + ' (' + label + ')' for path, label in candidates]
    chosen = ui.choose('Codex has different known configuration homes. Choose the one to configure for this trial. '
                       'Only local clients reading that file may be affected; the exact file will be shown before any change.', choices)
    require(chosen in choices, 'global_codex_home_selection_invalid')
    return checked(candidates[choices.index(chosen)][0])


def client_arguments(spec):
    from .global_pilot import CONFIG_TRIAL
    profile=spec.get('client_evidence_profile')
    require(profile is None or (isinstance(profile,str) and profile==CONFIG_TRIAL),
            'global_unknown_client_evidence_profile')
    if profile==CONFIG_TRIAL:
        require('desktop_app' not in spec and 'desktop' not in spec, 'global_client_evidence_profile_conflict')
        return {'cli_version':spec['cli']['version'],'desktop_version':None,'client_profile':CONFIG_TRIAL}
    if 'desktop_app' in spec:
        require('desktop' not in spec, 'global_client_evidence_profile_conflict')
        return {'cli_version':spec['cli']['version'],'desktop_version':None,'desktop_app':spec['desktop_app']}
    require('desktop' in spec,'global_explicit_client_evidence_profile_required')
    return {'cli_version':spec['cli']['version'],'desktop_version':spec['desktop']['version']}


def check_clients(spec):
    client_arguments(spec)  # Reject mixed/unknown/missing profiles before any fallback.
    check_binary(spec['cli'])
    if 'desktop_app' in spec: app_identity.check_application(spec['desktop_app'])
    elif 'desktop' in spec: check_binary(spec['desktop'])


def client_route_observation(runtime, transaction):
    """Post-commit client traffic, never desktop identity or config attestation.

    No request bodies or prompts leave the local store. A changed/restored config,
    stale activation, or preflight route cannot count as this trial's result.
    """
    result={'completed_client_routes':0,'client_route_observed':False,
            'desktop_new_thread_verified':False,'desktop_compatibility_verified':False,
            'client_compatibility_verified':False}
    if transaction.get('phase')!='committed' or not transaction.get('committed_at'): return result
    from .global_pilot import CONFIG_TRIAL, config_target
    if transaction.get('client_evidence_profile')==CONFIG_TRIAL:
        try:target_matches=config_target(transaction['codex_home'])==transaction.get('config_target_evidence')
        except (OSError,ProtocolError):target_matches=False
        if not target_matches:
            return {**result,'client_observation_state':'config_target_changed_revalidate'}
    try:current=config_tx.snapshot(transaction['config_path'])
    except (OSError,ProtocolError):
        return {**result,'client_observation_state':'config_unavailable_revalidate'}
    if not current['exists'] or current['hash']!=transaction['after_hash']:
        return {**result,'client_observation_state':'config_changed_revalidate'}
    store=Store(Path(runtime)/'gateway');activation=store.activation()
    if activation['id']!=transaction['generation'] or not activation['enabled'] or activation['expires']<=time.time():
        return {**result,'client_observation_state':'activation_inactive'}
    with store.transaction() as db:
        rows=db.execute("SELECT DISTINCT r.id,r.identity FROM routes r JOIN requests q ON q.route=r.id "
                        "WHERE r.generation=? AND r.created>=? AND q.created>=? "
                        "AND q.state IN ('text_complete','tool_complete') AND q.backend_response_id IS NOT NULL",
                        (transaction['generation'],transaction['committed_at'],transaction['committed_at'])).fetchall()
    count=sum(1 for row in rows if row['id']!=transaction.get('preflight_route_id')
              and not strict_json(row['identity']).get('session-id','').startswith('dots-pilot-'))
    return {**result,'completed_client_routes':count,'client_route_observed':bool(count),
            'client_observation_state':'client_route_seen_app_identity_unverified' if count else 'awaiting_new_client_thread'}


class DesktopPorts:
    def desktop_app(self, explicit, ui): return app_identity.discover_application(explicit=explicit, ui=ui)
    def binary(self, path): return binary_evidence(path)
    def copy(self, text): return router._copy_clipboard(text)
    def owned(self, active): return router._owned_process(active)
    def alive(self, active): return router._pid_alive(active.get('facade_pid'))
    def now(self): return time.time()
    def wait(self, seconds): time.sleep(seconds)
    def spawn(self, root, runtime):
        path = runtime / 'supervisor.log'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        env = {k: v for k, v in os.environ.items() if k not in {'PYTHONPATH', 'PYTHONHOME'}}
        env['PYTHONNOUSERSITE'] = '1'
        try:
            with os.fdopen(fd, 'wb') as out:
                child = subprocess.Popen([sys.executable, '-B', '-m', 'remote_transport.global_desktop',
                    'supervise', '--runtime', str(runtime)], cwd=str(root), env=env,
                    stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
        except Exception: raise ProtocolError('global_supervisor_launch_unknown_reconcile') from None
        return {'facade_pid': child.pid, 'facade_identity': router._process_identity(child.pid)}


class DesktopGlobal:
    def __init__(self, *, root, state, config, ui, ports=None):
        self.root, self.state, self.config = Path(root), no_symlinks(state), no_symlinks(config)
        self.ui, self.ports = ui, ports or DesktopPorts()
        self.current = self.state / 'current.json'

    def active(self):
        if not self.current.exists(): return None
        value = read(self.current, 32768)
        require(value.get('contract') == CONTRACT, 'invalid_global_desktop_state')
        runtime = no_symlinks(value['runtime'])
        require(runtime.parent == self.state / 'runs', 'global_runtime_path_mismatch')
        spec = read(runtime / 'spec.json')
        require(spec['runtime'] == str(runtime) and spec['run_id'] == value['run_id'], 'global_runtime_binding_mismatch')
        # Reconcile the Popen-return/current-pointer crash window using the
        # supervisor's own private identity, never by searching process names.
        owned_path = runtime / 'supervisor.json'
        if owned_path.exists():
            owned = read(owned_path, 32768)
            require(owned['run_id'] == value['run_id'], 'global_supervisor_identity_mismatch')
            # Popen can observe a launcher's pre-exec command fingerprint. Adopt
            # the child's later identity only for that same PID and current OS
            # evidence; a different PID or stale self-record is never ownership.
            if (value.get('facade_pid') in (None, owned['facade_pid'])
                    and type(owned.get('facade_pid')) is int and owned['facade_pid'] > 0
                    and owned.get('facade_identity') and self.ports.owned(owned)):
                value.update(facade_pid=owned['facade_pid'], facade_identity=owned['facade_identity'])
        return {**value, 'spec': spec}

    def _stop_intent(self):
        path = self.state / 'stop-intent.json'
        return read(path, 32768)['request_id'] if path.exists() else None

    def status(self, args=None):
        active = self.active()
        if active is None: return {'mode': 'desktop_global', 'stage': 'NOT_STARTED', 'config_changed': False}
        runtime = Path(active['runtime']); status_path = runtime / 'status.json'
        status = read(status_path) if status_path.exists() else {'stage': 'STARTING'}
        transaction = self._transaction(active)
        result = {'mode': 'desktop_global', 'run_id': active['run_id'],
                  'stage': status['stage'], 'supervisor_owned': self.ports.owned(active),
                  'supervisor_alive': self.ports.alive(active), 'production_ready': False,
                  'native_children_stopped': status.get('native_children_stopped', False),
                  'config_changed': bool(transaction and transaction['phase'] in {'prepared', 'committed', 'restore_prepared'})}
        if transaction and transaction['phase'] in {'prepared', 'restore_prepared'}:
            result['config_recovery_required'] = True
        for name in ('error', 'read_error', 'retry_round', 'retry_at', 'activation_expires',
                     'resume_stage', 'preflight_state', 'setup_expires', 'queue_closed'):
            if name in status: result[name] = status[name]
        try:
            bound = probe(runtime / 'gateway')
            result.update(controller_active=bound['controller_active'],
                          route_counts=bound['route_counts'], activation_enabled=bound['activation_enabled'],
                          docs_read_paused=bound.get('docs_read_paused', False))
        except Exception: result['controller_active'] = False
        if result['config_changed']:
            result['restart_required'] = True
            result['desktop_new_thread_verified'] = False
            result.update(client_route_observation(runtime, transaction))
        return result

    def _transaction(self, active):
        runtime = Path(active['runtime']); directory = runtime / 'gateway' / 'config-transactions'
        found = []
        if directory.exists():
            for path in directory.glob('*.json'):
                value = read(path, config_tx.MAX_CONFIG_BYTES * 3)
                require(value.get('generation') == active['spec']['generation']
                        and value.get('codex_home') == active['spec']['codex_home'], 'global_transaction_binding_mismatch')
                if value['phase'] not in {'restored', 'aborted'}: found.append(value)
        require(len(found) <= 1, 'global_multiple_transactions_require_reconciliation')
        return found[0] if found else None

    def _copy_join(self, active):
        status=self.status()
        require(status.get('queue_closed') is not True and status.get('stage') not in {'FAILED','STOPPED','STOPPING'}
                and self.ports.now()<active['spec']['expires'],'global_new_activation_and_join_required')
        path = Path(active['runtime']) / 'bridge' / 'global-join.txt'
        require(path.exists(), 'global_join_not_yet_prepared')
        if not self.ui.confirm('Copy the private GLOBAL controller JOIN? Send it once in your private Dots conversation. '
            'It authorizes one bounded active controller to create a separate selected native child for each new desktop thread. '
            'This is different from a single-worker Router JOIN. Clear the clipboard afterward.'):
            raise Cancelled()
        copied = self.ports.copy(read_private_file(path, 32768).decode())
        require(copied, 'global_join_clipboard_failed_open_private_file')
        self.ui.notify('Global controller JOIN copied. Send it to Dots once, then continue here. '
                       'A native preflight will finish before any global settings are offered.')

    def _wait_stage(self, active, seconds, desired, *, prior_proof=None):
        until = self.ports.now() + seconds; runtime = Path(active['runtime'])
        while self.ports.now() < until:
            if (runtime / 'status.json').exists():
                status = read(runtime / 'status.json')
                if ((status['stage'] in desired and (prior_proof is None or status.get('pilot_proof_id') != prior_proof))
                        or status['stage'] in {'FAILED', 'STOPPED', 'PAUSED_DOCS_READ', 'PREFLIGHT_UNRESOLVED'}): return status
            require(self.ports.alive(active), 'global_supervisor_stopped_review_status')
            self.ports.wait(.5)
        return read(runtime / 'status.json') if (runtime / 'status.json').exists() else {'stage': 'STARTING'}

    def _recovery(self, active, status):
        current=self.status();runtime=Path(active['runtime'])
        ended=(status.get('queue_closed') is True or current.get('queue_closed') is True
               or self.ports.now()>=active['spec']['expires'] or (runtime/'stop.json').exists()
               or status.get('stage') in {'FAILED','STOPPED','STOPPING'}
               or current.get('stage') in {'FAILED','STOPPED','STOPPING'}
               or current.get('activation_enabled') is False
               or current.get('supervisor_owned') is False
               or not current.get('supervisor_alive',False))
        if ended:
            return {**current,'resume_possible':False,'new_activation_required':True,
                    'next':'This activation cannot resume. Preserve its evidence, use Stop if needed, then explicitly start a new activation and send its new JOIN. No Google resources are created by this status check.'}
        if current.get('stage') == 'PAUSED_DOCS_READ':
            return {**current,'resume_possible':True,'new_activation_required':False,
                    'next':'Google Docs reads are temporarily unavailable. New admissions, dispatches and config approval are paused while this supervisor retries the exact read before the signed deadline. Use Continue after recovery; no new activation or JOIN is created by this check.'}
        if current.get('stage') == 'PREFLIGHT_UNRESOLVED':
            return {**current,'resume_possible':False,'new_activation_required':True,
                    'next':'The one-attempt preflight did not return a verified result. Its evidence and open queue are preserved, but the request will not be replayed and config approval is unavailable. Stop this activation before explicitly starting a new one with a new JOIN.'}
        return {**current,'resume_possible':True,'new_activation_required':False,
                'next':'This activation is still open and supervised. Continue after its controller and preflight finish, before the signed expiry. A closed, expired, or failed activation requires a new JOIN.'}

    def _continue(self, active):
        runtime = Path(active['runtime']); spec = active['spec']
        if self._transaction(active) is not None or (runtime / 'config-applied.json').exists(): return self.status()
        status = self._wait_stage(active, 180, {'PREFLIGHT_VERIFIED'})
        if status['stage'] != 'PREFLIGHT_VERIFIED':
            return self._recovery(active,status)
        from . import global_pilot as pilot
        try:
            pilot.require_pilot(runtime / 'gateway', status['pilot_proof_id'],
                                codex_home=spec['codex_home'], **client_arguments(spec))
        except ProtocolError as exc:
            if str(exc) not in {'pilot_proof_expired', 'pilot_preflight_expired'}: raise
            if not self.ui.confirm('The native preflight is too old to authorize a config write. Run a fresh small preflight on the same pinned child? '
                'No replacement child is spawned. Settings remain unchanged until a new exact-diff confirmation.'):
                raise Cancelled()
            request_path = runtime / 'refresh-request.json'
            with private_lock(self.state / 'lifecycle.lock'):
                if request_path.exists():
                    pending = read(request_path)
                    handled = read(runtime / 'refresh-handled.json') if (runtime / 'refresh-handled.json').exists() else {}
                    require(pending['request_id'] == handled.get('request_id'), 'global_preflight_refresh_already_pending')
                save(request_path, {'request_id': secrets.token_hex(16), 'run_id': active['run_id'], 'requested': self.ports.now()})
            previous_proof = status['pilot_proof_id']
            status = self._wait_stage(active, 180, {'PREFLIGHT_VERIFIED'}, prior_proof=previous_proof)
            if status['stage'] != 'PREFLIGHT_VERIFIED' or status.get('pilot_proof_id') == previous_proof: return self.status()
        require(not (runtime / 'stop.json').exists(), 'global_stop_requested')
        check_clients(spec)
        plan = config_tx.preview(runtime / 'gateway', spec['codex_home'],
            **client_arguments(spec))
        trial = ('desktop_app' in spec)
        target = ('Detected app: ' + spec['desktop_app']['path'] + ' (app ' + spec['desktop_app']['app_version'] + ').\n'
            'Desktop engine/catalog compatibility has not been verified. This is a reversible configuration trial.\n'
            if trial else '')
        message = ('Apply this PILOT local-client routing configuration for new threads?\n' + target +
            'Target: ' + plan['config_path'] + '\n' + plan['diff'] + '\n'
            'The native BACKEND preflight passed; this does not test your client. The catalog adapter is pinned to codex-cli 0.159.2. '
            'Only local clients reading this exact file may be affected; ChatGPT Work/Cloud compatibility is not established. '
            'Fully quit and reopen the client (including a retained app-server or managed daemon when applicable), '
            'then create a new thread and check Global status for a completed client route. Traffic alone does not identify or certify an app. '
            'If the client rejects the catalog or no request reaches this gateway, use Restore Global config. Existing or resumed threads may retain their previous provider. '
            'Profiles, CLI overrides, or managed settings may override this file. '
            'The bounded native controller must stay active. A private exact backup enables restore.\n'
            'Only the displayed owned settings are changed. Proceed?')
        if not self.ui.confirm(message): raise Cancelled()
        # Recheck immediately after the potentially slow consent interaction.
        active = self.active()
        require(active is not None and active['run_id'] == spec['run_id']
                and self.ports.owned(active) and not (runtime / 'stop.json').exists(), 'global_supervisor_not_owned_or_stopping')
        check_clients(spec)
        applied = config_tx.apply(runtime / 'gateway', spec['codex_home'],
            **client_arguments(spec),
            expected_before_hash=plan['before_hash'], expected_after_hash=plan['after_hash'],
            confirm=True, pilot_proof_id=status['pilot_proof_id'])
        save(runtime / 'config-applied.json', applied)
        return {**applied, 'mode': 'desktop_global', 'pilot': True, 'production_ready': False,
                'next': 'Fully quit/reopen the local client and any retained app-server or managed daemon; create a new thread. Verify it reads the selected config path.'}

    def start(self, args):
        stop_intent = self._stop_intent()
        active = self.active()
        if active is not None:
            status = self.status()
            if status['stage'] != 'STOPPED' or status['config_changed']:
                choice = self.ui.choose('Existing Global activation', ['Continue activation', 'Copy global controller JOIN', 'Status', 'Stop', 'Restore settings', 'Cancel'])
                if choice == 'Cancel': raise Cancelled()
                if choice == 'Copy global controller JOIN': self._copy_join(active); return self.status()
                if choice == 'Status': return status
                if choice == 'Stop': return self.stop(args)
                if choice == 'Restore settings': return self.restore(args)
                return self._continue(active)
        # Real parser dependency must be installed before any Google mutation.
        config_tx.parser()
        raw = read_private_file(self.config, 131072)
        expected = getattr(args, 'expected_config_sha256', None)
        require(expected is not None and hash_bytes(raw) == expected, 'global_router_config_changed')
        cfg = router._validate_config(strict_json(raw))
        selected = router._resolve_selection(args, cfg)
        require(selected is not None, 'global_default_selection_required')
        home = resolve_codex_home(explicit=getattr(args, 'codex_home', None),
            saved=active['spec'].get('codex_home') if active is not None else None, ui=self.ui)
        cli = self.ports.binary(cfg['codex'])
        explicit_binary = getattr(args, 'desktop_codex', None)
        explicit_app = getattr(args, 'desktop_app', None)
        require(not (explicit_binary is not None and explicit_app is not None), 'global_choose_one_desktop_evidence_mode')
        if explicit_binary is not None:
            # Legacy opt-in CLI flag only; no file chooser or guessed binary path.
            desktop_evidence = {'desktop': self.ports.binary(explicit_binary)}
        elif explicit_app is not None:
            desktop_evidence = {'desktop_app': self.ports.desktop_app(explicit_app, self.ui)}
        else:
            from .global_pilot import CONFIG_TRIAL
            desktop_evidence = {'client_evidence_profile': CONFIG_TRIAL}
        if not self.ui.confirm('Start GLOBAL local-client routing for up to four hours?\n'
            'Dedicated Google folder: ' + cfg['folder_id'] + '\nCODEX_HOME: ' + str(home) + '\n'
            'No app installation or identity is required in the normal config-first trial. Only local consumers of the selected config can be affected. '
            'Creates one signed control queue, then separate pairing/control Docs and probes for each admitted thread. '
            'A tiny native preflight runs first. Capacity is bounded to three children including preflight. '
            'Send one GLOBAL controller JOIN to Dots. Global config changes require a separate exact-diff confirmation.'):
            raise Cancelled()
        private_dir(self.state, create=True)
        with private_lock(self.state / 'lifecycle.lock'):
            require(self._stop_intent() == stop_intent, 'global_start_cancelled_by_stop')
            latest = self.active()
            require(latest is None or latest['run_id'] == (active or {}).get('run_id'), 'global_activation_changed')
            if latest is not None:
                require(self.status()['stage'] == 'STOPPED' and not self.status()['config_changed'], 'global_previous_activation_needs_restore')
            run_id = secrets.token_hex(16); runtime = private_dir(self.state / 'runs' / run_id, create=True)
            seconds = min(cfg['seconds'], 14400)
            store, generation = Store.initialize(runtime / 'gateway', selected, port=DEFAULT_PORT,
                seconds=seconds, max_children=3, max_pending=3, idle_seconds=min(1800, seconds))
            private_write(runtime / 'router-config.json', raw)
            spec = {'contract': CONTRACT, 'run_id': run_id, 'runtime': str(runtime), 'generation': generation,
                    'codex_home': str(home), 'cli': cli, **desktop_evidence, 'config_hash': hash_bytes(raw),
                    'stop_intent_id': stop_intent,
                    'expires': store.activation()['expires'], 'created': self.ports.now()}
            save(runtime / 'spec.json', spec)
            record = {'contract': CONTRACT, 'run_id': run_id, 'runtime': str(runtime), 'facade_pid': None, 'facade_identity': None}
            save(self.current, record)  # Durable launch intent before the process effect.
            require(self._stop_intent() == stop_intent, 'global_start_cancelled_by_stop')
            record.update(self.ports.spawn(self.root, runtime)); save(self.current, record)
            active = {**record, 'spec': spec}
        status = self._wait_stage(active, 90, {'WAITING_CONTROLLER', 'NATIVE_PREFLIGHT', 'PREFLIGHT_VERIFIED'})
        if status['stage'] in {'FAILED', 'STOPPED'}: return self.status()
        if status['stage'] in {'PAUSED_DOCS_READ', 'PREFLIGHT_UNRESOLVED'}: return self._recovery(active,status)
        self._copy_join(active)
        return self._continue(active)

    def stop(self, args=None):
        # A root intent cancels a concurrent first start even before it publishes
        # current.json. It is not tied to a not-yet-created runtime or bare PID.
        private_dir(self.state, create=True)
        save(self.state / 'stop-intent.json', {'request_id': secrets.token_hex(16), 'requested': self.ports.now()})
        active = self.active()
        if active is None: return {'stage': 'NOT_STARTED', 'native_children_stopped': False}
        runtime = Path(active['runtime'])
        save(runtime / 'stop.json', {'run_id': active['run_id'], 'requested': self.ports.now()})
        # Fence admissions immediately even while Google is slow or unavailable.
        store = Store(runtime / 'gateway'); store.disable(active['spec']['generation'])
        with store.transaction() as db: db.execute('UPDATE controller SET heartbeat=0,expires=0')
        if self.ports.alive(active):
            try: self._wait_stage(active, 15, {'STOPPED'})
            except ProtocolError:
                if self.ports.alive(active): raise
        if not self.ports.alive(active):
            prior = read(runtime / 'status.json') if (runtime / 'status.json').exists() else {}
            save(runtime / 'status.json', {'stage': 'STOPPED', 'updated': self.ports.now(),
                'process_stopped': True, 'queue_closed': prior.get('queue_closed', False),
                'native_children_stopped': False, 'remote_close_requires_reconciliation': not prior.get('queue_closed', False)})
        return {**self.status(), 'settings_restored': (runtime / 'config-restored.json').exists(),
                'next': 'Restore settings before restarting desktop. Stop the bounded native controller in Dots as well.'}

    def restore(self, args=None):
        active = self.active()
        if active is None: return {'config_changed': False, 'write_performed': False}
        runtime = Path(active['runtime']); applied_path = runtime / 'config-applied.json'
        transaction = self._transaction(active)
        if transaction is None: return {'config_changed': False, 'write_performed': False}
        if not self.ui.confirm('Restore only the owned global settings from this activation’s private backup?\n'
            + active['spec']['codex_home'] + '/config.toml\nUnrelated edits are preserved; conflicts stop the restore. '
            'Restart desktop and terminal afterward. This does not stop a running native controller.'):
            raise Cancelled()
        if transaction['phase'] in {'prepared', 'restore_prepared'}:
            reconciled = config_tx.reconcile(runtime / 'gateway', transaction['id'])
            if reconciled['phase'] in {'restored', 'aborted'}: return reconciled
        result = config_tx.restore(runtime / 'gateway', transaction['id'], confirm=True)
        save(runtime / 'config-restored.json', result)
        return result


def post_preflight(store, plan, control=None):
    """One bounded HTTP attempt. Never retry a timeout or an unknown dispatch."""
    conn = http.client.HTTPConnection('127.0.0.1', store.config()['port'], timeout=600)
    deadline = time.monotonic() + 600
    try:
        conn.connect(); transport = conn.sock
        if control is not None:
            with control['lock']:
                require(not control['cancelled'], 'global_preflight_cancelled_before_dispatch')
                control['socket'] = transport
        conn.request('POST', f'/activations/{store.activation()["id"]}/v1/responses',
            body=canonical(plan['body']), headers={**plan['identity'], 'Content-Type': 'application/json'})
        response = conn.getresponse(); total = 0; blocks = []
        require(response.status == 200, 'global_preflight_request_failed')
        while True:
            if response.isclosed(): break
            remaining = deadline - time.monotonic()
            require(remaining > 0, 'global_preflight_deadline')
            transport.settimeout(remaining)
            block = response.read1(65536)
            if not block: break
            total += len(block); require(total <= MAX, 'global_preflight_response_too_large')
            blocks.append(block)
        # The gateway can already have sent HTTP 200 before its bounded
        # admission wait fails. That is not a completed native preflight.
        raw = b''.join(blocks)
        try:
            events = [strict_json(line[6:]) for line in raw.splitlines() if line.startswith(b'data: ')]
            require(all(isinstance(event, dict) for event in events), 'global_preflight_invalid_response_stream')
        except (ProtocolError, ValueError, UnicodeError):
            raise ProtocolError('global_preflight_invalid_response_stream') from None
        require(not any(event.get('type') == 'response.failed' for event in events), 'global_preflight_request_failed')
        require(events and events[-1].get('type') == 'response.completed'
                and isinstance(events[-1].get('response'), dict)
                and events[-1]['response'].get('status') == 'completed', 'global_preflight_request_incomplete')
        return {'http_status': response.status}
    finally:
        if control is not None:
            with control['lock']: control['socket'] = None
        conn.close()


def supervise(runtime):
    from .global_google import prepare_session
    from . import global_pilot as pilot
    runtime = private_dir(runtime); spec = read(runtime / 'spec.json'); store = Store(runtime / 'gateway')
    require(spec['contract'] == CONTRACT and spec['runtime'] == str(runtime)
            and spec['generation'] == store.activation()['id'], 'global_supervisor_spec_mismatch')
    save(runtime / 'supervisor.json', {'run_id': spec['run_id'], 'facade_pid': os.getpid(),
                                      'facade_identity': router._process_identity(os.getpid())})
    cfg_path = runtime / 'router-config.json'; require(hash_bytes(read_private_file(cfg_path, 131072)) == spec['config_hash'], 'global_supervisor_config_changed')
    cfg = router._load_config(cfg_path); bridge = None; future = None; read_port = None
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    request_control = {'lock': threading.Lock(), 'socket': None, 'cancelled': False}
    current_status = {'stage': 'STARTING'}
    def status(stage, **values):
        current_status.clear(); current_status.update(stage=stage, **values)
        save(runtime / 'status.json', {**current_status, 'updated': time.time()})
    def paused(**values):
        save(runtime / 'status.json', {'stage': 'PAUSED_DOCS_READ', 'updated': time.time(),
             'resume_stage': current_status['stage'], **values})
    def resumed():
        save(runtime / 'status.json', {**current_status, 'updated': time.time()})
    try:
        # Fixed-port bind precedes all credential refresh and cloud resource creation.
        with Gateway(store, admission_wait=pilot.SETUP_SECONDS) as gateway:
            require(not (runtime / 'stop.json').exists(), 'global_stop_requested')
            root_stop = runtime.parent.parent / 'stop-intent.json'
            current_stop = read(root_stop, 32768)['request_id'] if root_stop.exists() else None
            require(current_stop == spec.get('stop_intent_id'), 'global_start_cancelled_by_stop')
            router._set_google_env(cfg)
            from examples.google_clients import create_docs_client, create_drive_client
            read_port = RecoveringDocs(create_docs_client(), store=store, runtime=runtime, spec=spec,
                                       on_pause=paused, on_resume=resumed)
            bridge = prepare_session(store, runtime / 'bridge', read_port, create_drive_client(), cfg['folder_id'],
                mac_writer=cfg['mac_writer_identity'], worker_writer=cfg['worker_writer_identity'],
                bootstrap_seconds=pilot.SETUP_SECONDS, parallel_preparation=True)
            read_port.reconcile(bridge)
            status('WAITING_CONTROLLER')
            plan = None; proof = None; preflight_failure = None; reservation = None
            def observe_clients():
                check_clients(spec)
                cli_path = spec['cli'].get('invocation_path', spec['cli']['path'])
                if spec.get('client_evidence_profile')==pilot.CONFIG_TRIAL:
                    return pilot.observe_config_client(store, cli_path, spec['codex_home'])
                if 'desktop_app' in spec:
                    return pilot.observe_desktop_app(store, cli_path, spec['desktop_app'])
                return pilot.observe_versions(store, cli_path,
                    spec['desktop'].get('invocation_path', spec['desktop']['path']))
            while time.time() < spec['expires'] and not (runtime / 'stop.json').exists():
                current = bridge.step()
                if current['state'] == 'closed': break
                require(current.get('restart_required') is not True,'global_controller_restart_requires_new_activation')
                read_port.reconcile(bridge)
                refresh = None
                refresh_path = runtime / 'refresh-request.json'
                if proof is not None and refresh_path.exists():
                    candidate = read(refresh_path)
                    require(candidate['run_id'] == spec['run_id'], 'global_refresh_run_mismatch')
                    handled = read(runtime / 'refresh-handled.json') if (runtime / 'refresh-handled.json').exists() else {}
                    if candidate['request_id'] != handled.get('request_id'): refresh = candidate
                if preflight_failure is None and future is None:
                    if reservation is None and current['state'] == 'controller_active':
                        # A private identity creates demand; no HTTP/challenge budget
                        # starts while the controller spawns and pairs this child.
                        reservation = pilot.reserve_preflight_route(store, version_evidence=observe_clients())
                        read_port.setup_expires = reservation['expires']
                        read_port.setup_deadline = time.monotonic() + max(0, reservation['expires']-time.time())
                        status('PREPARING_PREFLIGHT_ROUTE', preflight_state='waiting_ready_no_post',
                               setup_expires=reservation['expires'])
                    if reservation is not None:
                        queue_state = bridge.read().state
                        readiness = pilot.preflight_route_status(store, reservation['reservation_id'],
                            queue_state=queue_state, join_code=bridge.code)
                        if readiness['ready']:
                            plan = pilot.finalize_preflight_route(store, reservation['reservation_id'],
                                version_evidence=observe_clients(), queue_state=queue_state, join_code=bridge.code)
                            read_port.setup_expires = None; read_port.setup_deadline = None
                elif preflight_failure is None and refresh is not None and current['state'] == 'controller_active':
                    save(runtime / 'refresh-handled.json', {**refresh, 'state': 'issued_outcome_unknown'})
                    plan = pilot.prepare_preflight(store, version_evidence=observe_clients(), previous_plan_id=plan['plan_id'])
                    proof = None
                    future = None
                if preflight_failure is None and plan is not None and future is None:
                    read_port._check()
                    require(not (runtime / 'stop.json').exists(), 'global_stop_requested')
                    attempt = runtime / ('preflight-post-' + plan['plan_id'] + '.json')
                    require(not attempt.exists(), 'global_preflight_attempt_already_issued_no_replay')
                    # Persist before submitting. A failed/unknown POST or supervisor
                    # restart never obtains a second attempt or another warm route.
                    save(attempt, {'plan_id': plan['plan_id'], 'state': 'issued_outcome_unknown'})
                    status('NATIVE_PREFLIGHT', preflight_state='one_attempt_running')
                    future = pool.submit(post_preflight, store, plan, request_control)
                if future is not None and future.done() and proof is None and preflight_failure is None:
                    try: future.result()
                    except Exception as exc:
                        if isinstance(exc, ProtocolError) and str(exc) in {
                                'global_preflight_invalid_response_stream', 'global_preflight_response_too_large'}:
                            raise
                        # A concurrent read pause can reject/delay the only POST.
                        # Keep the activation supervised without replaying that
                        # possibly dispatched request or minting another plan.
                        preflight_failure = safe_error(exc)
                        status('PREFLIGHT_UNRESOLVED', error=preflight_failure,
                               preflight_state='one_attempt_failed_no_replay')
                    else:
                        gateway.completion_for(plan['route_id']).wait(2)
                        queue_state = bridge.read().state
                        queue_state = read_port.reconcile(bridge) or queue_state
                        proof = pilot.verify_preflight(store, plan['plan_id'], queue_state=queue_state, join_code=bridge.code)
                        status('PREFLIGHT_VERIFIED', pilot_proof_id=proof['proof_id'])
                time.sleep(1)
            status('STOPPING')
            read_port.stop_recovery()
            outcome = bridge.stop(); status('STOPPED', **outcome)
    except Exception as exc:
        store.disable(spec['generation'])
        if read_port is not None: read_port.stop_recovery()
        outcome = {}
        if bridge is not None:
            try: outcome = bridge.stop()
            except Exception: outcome = {'queue_closed': False, 'native_children_stopped': False}
        from examples.google_clients import DocsReadError
        details = {'read_error': exc.diagnostics} if isinstance(exc, DocsReadError) else {}
        status('FAILED', error=safe_error(exc), **details, **outcome)
    finally:
        with request_control['lock']:
            request_control['cancelled'] = True
            transport = request_control['socket']
            if transport is not None:
                try: transport.shutdown(socket.SHUT_RDWR)
                except OSError: pass
        if bridge is not None: bridge.close()
        pool.shutdown(wait=True, cancel_futures=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation', choices=['supervise']); p.add_argument('--runtime', required=True)
    args = p.parse_args(argv); os.umask(0o077); supervise(args.runtime)


if __name__ == '__main__': main()
