"""Sticky-session control plane. Data only: this module cannot spawn native agents."""
import argparse
import contextlib
import fcntl
import json
import os
import re
import stat
import threading
import time
import uuid
from pathlib import Path

from portable import (Deployment, QueueError, checked, digest, encode, init as init_deployment,
                      label, number, private_dir, protocol, read)

VERSION = 'dots-sticky-routing/1'
CORRELATION = ('registry_id', 'session_key', 'deployment_id', 'run_id', 'generation',
               'admission_id', 'worker_id', 'worker_instance', 'assignment_id', 'broker_epoch')


def evidence(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_:/@.\-]{1,256}', value):
        raise QueueError('invalid_evidence_reference')
    return value


def initialize(root, owner, seconds=900, max_sessions=8):
    label(owner)
    number(seconds, 5, 3600)
    if type(max_sessions) is not int or not 1 <= max_sessions <= 16:
        raise QueueError('invalid_session_limit')
    root = Path(root).absolute()
    if root.exists() or root.is_symlink():
        raise QueueError('registry_already_exists')
    root.mkdir(mode=0o700, parents=True)
    (root / 'sessions').mkdir(mode=0o700)
    fd = os.open(root / 'registry.lock', os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(fd)
    state = dict(contract=VERSION, registry_id=uuid.uuid4().hex, owner=owner,
                 created=time.time(), expires=time.time() + seconds,
                 max_sessions=max_sessions, sessions={}, admissions={})
    protocol.atomic_json(root / 'registry.json', state, exclusive=True)
    return state


class Registry:
    def __init__(self, root):
        self.root = private_dir(root)
        private_dir(self.root / 'sessions')
        self._lock_scope = threading.local()

    @contextlib.contextmanager
    def locked(self):
        fd = os.open(self.root / 'registry.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        token = None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise QueueError('invalid_registry_lock')
            end = time.monotonic() + 3
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= end:
                        raise QueueError('registry_lock_timeout')
                    time.sleep(.005)
            state = read(self.root / 'registry.json', 262144)
            if not isinstance(state, dict) or state.get('contract') != VERSION:
                raise QueueError('invalid_registry')
            checked(state['registry_id'])
            label(state['owner'])
            number(state['expires'] - state['created'], 5, 3601)
            if type(state['max_sessions']) is not int or not 1 <= state['max_sessions'] <= 16:
                raise QueueError('invalid_registry')
            if not isinstance(state['sessions'], dict) or not isinstance(state['admissions'], dict):
                raise QueueError('invalid_registry')
            if len(state['sessions']) > state['max_sessions'] or len(state['admissions']) > 3 * state['max_sessions']:
                raise QueueError('invalid_registry')
            token = dict(active=True, thread_id=threading.get_ident(), state=state)
            self._lock_scope.token = token
            yield state
        finally:
            if token is not None:
                token['active'] = False
                self._lock_scope.token = None
            os.close(fd)

    def save(self, state):
        if len(encode(state)) > 262144:
            raise QueueError('registry_too_large')
        protocol.atomic_json(self.root / 'registry.json', state)

    def live(self, state, owner=None):
        if owner is not None and owner != state['owner']:
            raise QueueError('owner_mismatch')
        if time.time() >= state['expires']:
            raise QueueError('registry_expired')

    def session(self, state, key):
        label(key)
        route = state['sessions'].get(key)
        if not route or route['phase'] == 'initializing':
            raise QueueError('session_missing_or_initialization_incomplete')
        if route['session_key'] != key or route['registry_id'] != state['registry_id']:
            raise QueueError('route_scope_mismatch')
        return route

    def deployment(self, state, route):
        token = getattr(self._lock_scope, 'token', None)
        if not token or not token['active'] or token['state'] is not state:
            raise QueueError('registry_lock_scope_required')
        # Never use paths supplied by a worker or absolute producer-side paths.
        label(route['session_key'])
        d = Deployment(self.root / 'sessions' / route['session_key'])
        if (d.m['deployment_id'], d.m['run_id'], d.m['owner_id']) != (
                route['deployment_id'], route['run_id'], state['owner']):
            raise QueueError('deployment_route_mismatch')
        marker = read(d.root / 'control/routing.json', 8192)
        expected = dict(contract=VERSION, registry_id=state['registry_id'],
                        session_key=route['session_key'], deployment_id=d.m['deployment_id'], run_id=d.m['run_id'])
        if marker != expected:
            raise QueueError('routing_marker_mismatch')
        # Scoped to this fresh Deployment instance AND calling thread. Never returned.
        d._routing_guard = threading.local()
        d._routing_guard.marker = marker
        d._routing_guard.token = token
        return d

    def _reserve(self, state, route):
        generation = route.get('generation', 0) + 1
        if generation > 3:
            raise QueueError('admission_budget_exhausted')
        aid = uuid.uuid4().hex
        worker_instance = uuid.uuid4().hex
        route.update(generation=generation, admission_id=aid,
                     worker_id='worker_' + worker_instance, worker_instance=worker_instance,
                     assignment_id=uuid.uuid4().hex, broker_epoch=route.get('broker_epoch', 0) + 1,
                     phase='awaiting_admission', lease_until=None, assignment=None)
        admission = {key: route[key] for key in CORRELATION}
        admission.update(state='pending', created=time.time(), native_task_id=None,
                         meaning='Parent must use an available authorized native task tool; this file spawns nothing')
        state['admissions'][aid] = admission
        return dict(admission)

    def create_session(self, owner, key, scope='text_only', seconds=600, max_requests=3):
        label(key)
        number(seconds, 5, 900)
        if scope not in ('text_only', 'tool_probe', 'repo_review') or type(max_requests) is not int or not 1 <= max_requests <= (16 if scope == 'repo_review' else 3):
            raise QueueError('invalid_session_options')
        with self.locked() as state:
            self.live(state, owner)
            if key in state['sessions']:
                route = self.session(state, key)
                if route['scope'] != scope or route['max_requests'] != max_requests:
                    raise QueueError('session_options_conflict')
                return dict(route)
            if len(state['sessions']) >= state['max_sessions']:
                raise QueueError('session_capacity_exhausted')
            remaining = min(seconds, state['expires'] - time.time())
            if remaining < 5:
                raise QueueError('registry_expiring')
            route = dict(registry_id=state['registry_id'], session_key=key, phase='initializing',
                         scope=scope, max_requests=max_requests)
            state['sessions'][key] = route
            self.save(state)  # Crash/partial initialization never silently becomes a live session.
            droot = self.root / 'sessions' / key
            manifest = init_deployment(droot, owner, remaining, max_requests, scope=scope)
            route.update(deployment_id=manifest['deployment_id'], run_id=manifest['run_id'])
            marker = dict(contract=VERSION, registry_id=state['registry_id'], session_key=key,
                          deployment_id=manifest['deployment_id'], run_id=manifest['run_id'])
            protocol.atomic_json(droot / 'control/routing.json', marker, exclusive=True)
            self._reserve(state, route)
            self.save(state)
            return dict(route)

    def _admission(self, state, admission_id):
        checked(admission_id)
        item = state['admissions'].get(admission_id)
        if item is None:
            raise QueueError('admission_missing')
        route = self.session(state, item['session_key'])
        if any(item[k] != route[k] for k in CORRELATION):
            raise QueueError('stale_admission')
        return item, route

    def dispatch_started(self, owner, admission_id, adapter):
        evidence(adapter)
        with self.locked() as state:
            self.live(state, owner)
            item, route = self._admission(state, admission_id)
            if item['state'] != 'pending':
                raise QueueError('admission_already_dispatched')
            item.update(state='dispatch_started', adapter=adapter, dispatch_started_at=time.time())
            self.save(state)
            return dict(item)

    def confirm(self, owner, admission_id, native_task_id, lease=180):
        evidence(native_task_id)
        number(lease, .05, 240)
        with self.locked() as state:
            self.live(state, owner)
            item, route = self._admission(state, admission_id)
            if item['state'] not in ('dispatch_started', 'uncertain', 'binding', 'confirmed'):
                raise QueueError('admission_not_dispatched')
            if item.get('native_task_id') not in (None, native_task_id):
                raise QueueError('native_task_identity_conflict')
            if any(a['admission_id'] != admission_id and a.get('native_task_id') == native_task_id
                   for a in state['admissions'].values()):
                raise QueueError('native_context_reuse_rejected')
            d = self.deployment(state, route)
            with d.locked() as control:
                d.live(control)
                old = control['roles'].get('broker')
            if item['state'] == 'confirmed':
                self._validate_binding(state, self.credential(route), allow_expired=True)
                return self.credential(route)
            item.update(state='binding', native_task_id=native_task_id)
            route['phase'] = 'binding'
            self.save(state)  # Durable full tuple before an assignment can exist.
            if old and old['assignment_id'] == route['assignment_id']:
                if (old['epoch'], old['worker_id'], old['deployment_id'], old['run_id'], old['owner_id']) != (
                        route['broker_epoch'], route['worker_id'], route['deployment_id'], route['run_id'], owner):
                    raise QueueError('binding_assignment_conflict')
                if old['status'] != 'active' or old['expires'] <= time.time():
                    raise QueueError('binding_assignment_expired')
                assignment = old
            else:
                if old and old['epoch'] >= route['broker_epoch']:
                    raise QueueError('binding_assignment_conflict')
                assignment = d.assign(owner, 'broker', route['worker_id'], lease=lease,
                                      native_attested=True, assignment_id=route['assignment_id'],
                                      expected_epoch=route['broker_epoch'])
            route.update(phase='active', lease_until=assignment['expires'], assignment=assignment,
                         native_task_id=native_task_id)
            item.update(state='confirmed', confirmed_at=time.time())
            self.save(state)
            return self.credential(route)

    @staticmethod
    def credential(route):
        return dict(contract=VERSION, **{key: route[key] for key in CORRELATION})

    def _validate_binding(self, state, credential, allow_expired=False, allow_closed=False):
        if not isinstance(credential, dict) or credential.get('contract') != VERSION:
            raise QueueError('invalid_worker_credential')
        route = self.session(state, credential.get('session_key'))
        if any(credential.get(key) != route[key] for key in CORRELATION):
            raise QueueError('stale_worker_generation')
        if route['phase'] != 'active' and not (allow_closed and route['phase'] == 'closed'):
            raise QueueError('worker_not_active')
        item = state['admissions'][route['admission_id']]
        if item['state'] != 'confirmed' or item['native_task_id'] != route['native_task_id']:
            raise QueueError('unconfirmed_worker')
        if not allow_expired and (time.time() >= route['lease_until'] or time.time() >= state['expires']):
            raise QueueError('worker_lease_expired')
        d = self.deployment(state, route)
        with d.locked() as control:
            if not allow_expired:
                d.live(control)
            current = control['roles'].get('broker')
            if allow_closed and current and current['status'] == 'closed':
                fields = ('contract', 'deployment_id', 'run_id', 'owner_id', 'role', 'worker_id', 'assignment_id', 'epoch')
                if any(current.get(k) != route['assignment'].get(k) for k in fields):
                    raise QueueError('binding_assignment_conflict')
            else:
                current = d.current(control, route['assignment'], allow_expired=allow_expired)
            if current['assignment_id'] != route['assignment_id'] or current['epoch'] != route['broker_epoch']:
                raise QueueError('binding_assignment_conflict')
        return route, d

    def failed_admission(self, owner, admission_id, outcome, proof):
        """Explicit operator evidence only. No time-based inference that a spawn failed."""
        evidence(proof)
        if outcome not in ('not_started', 'confirmed_stopped', 'unknown'):
            raise QueueError('invalid_admission_outcome')
        with self.locked() as state:
            self.live(state, owner)
            item, route = self._admission(state, admission_id)
            if item['state'] not in ('pending', 'dispatch_started', 'uncertain'):
                raise QueueError('admission_outcome_requires_review')
            item.update(state='uncertain' if outcome == 'unknown' else 'failed', outcome=outcome,
                        evidence=proof, resolved_at=time.time())
            route['phase'] = 'blocked_admission'
            self.save(state)
            return dict(item)

    def retry_admission(self, owner, key):
        with self.locked() as state:
            self.live(state, owner)
            route = self.session(state, key)
            item = state['admissions'][route['admission_id']]
            if item['state'] != 'failed':
                raise QueueError('admission_not_safely_failed')
            # Failed admission never created a broker assignment. Its reserved epoch is reusable.
            route['broker_epoch'] -= 1
            self._reserve(state, route)
            self.save(state)
            return dict(route)

    def _safe_failover(self, d, route):
        with d.locked() as control:
            d.live(control)
            jobs = d.jobs()
            for job in jobs:
                dispatch = control['dispatch'].get(job['id'], {})
                if dispatch.get('status') == 'started' and not (
                        job['state'] == 'completed' and job.get('completion_sha256')):
                    raise QueueError('ambiguous_inference_requires_review')
                if job['state'] == 'running':
                    raise QueueError('prior_job_lease_active')
                if job['state'] == 'completed' and job['delivery'] != 'delivered':
                    raise QueueError('ambiguous_delivery_requires_review')
            tool = control.get('tool_probe')
            if tool is not None:
                from tool_probe import expected_final
                source = next((job for job in jobs if job['id'] == tool['job_id']), None)
                if not source or source['state'] != 'completed' or source['delivery'] != 'delivered' or digest(source.get('result')) != tool['intent_sha256']:
                    raise QueueError('unresolved_tool_execution')
                proved = False
                for job in jobs:
                    if job['id'] == tool['job_id']:
                        continue
                    try:
                        expected_final(job['request'], tool)
                        proved = True
                    except QueueError:
                        pass
                if not proved:
                    raise QueueError('unresolved_tool_execution')
            current = control['roles'].get('broker')
            if current:
                if (current['assignment_id'], current['epoch']) != (route['assignment_id'], route['broker_epoch']):
                    raise QueueError('binding_assignment_conflict')
                current['status'] = 'closed'
                d.save(control)

    def failover(self, owner, key):
        with self.locked() as state:
            self.live(state, owner)
            route = self.session(state, key)
            if route['scope'] == 'repo_review':
                raise QueueError('repo_review_replacement_not_supported')
            if route['phase'] not in ('active', 'closed'):
                raise QueueError('route_not_replaceable')
            if route['phase'] == 'active' and route['lease_until'] > time.time():
                raise QueueError('worker_still_live')
            if route['generation'] >= 3:
                raise QueueError('admission_budget_exhausted')
            d = self.deployment(state, route)
            self._safe_failover(d, route)
            self._reserve(state, route)
            self.save(state)
            return dict(route)

    def resolve_inference(self, owner, key, job_id, outcome, proof):
        """Operator-proved termination/not-started; retire original job, never replay it."""
        checked(job_id); evidence(proof)
        if outcome not in ('confirmed_not_started', 'confirmed_stopped'):
            raise QueueError('invalid_resolution')
        with self.locked() as state:
            self.live(state, owner)
            route = self.session(state, key)
            if route['phase'] not in ('active', 'closed'):
                raise QueueError('route_not_replaceable')
            if route['phase'] == 'active' and route['lease_until'] > time.time():
                raise QueueError('worker_still_live')
            d = self.deployment(state, route)
            with d.locked() as control:
                d.live(control)
                assignment = control['roles'].get('broker')
                if not assignment or (assignment['assignment_id'], assignment['epoch']) != (route['assignment_id'], route['broker_epoch']):
                    raise QueueError('binding_assignment_conflict')
                # A binding-expired worker is fenced even if its conservative role lease remains.
                dispatch = control['dispatch'].get(job_id)
                if not dispatch or dispatch['assignment_id'] != route['assignment_id']:
                    raise QueueError('dispatch_scope_mismatch')
                if dispatch.get('status') == outcome and dispatch.get('evidence') == proof:
                    route.update(phase='closed', closed_at=time.time())
                    self.save(state)
                    return dict(dispatch, original_job_replayed=False)
                if dispatch.get('status') != 'started':
                    raise QueueError('no_ambiguous_dispatch')
                q = d.queue()
                try:
                    job = q.get(job_id, owner=owner, session=q.meta['session'])
                    if job['state'] == 'completed':
                        raise QueueError('completed_job_requires_no_resolution')
                    # Tool reservation is preserved, and still blocks failover without a receipt.
                    q.cancel(job_id, owner=owner, session=q.meta['session'], reason='cancelled')
                finally:
                    q.close()
                dispatch.update(status=outcome, evidence=proof, resolved_at=time.time())
                assignment['status'] = 'closed'
                d.save(control)
                route.update(phase='closed', closed_at=time.time())
                self.save(state)
                return dict(dispatch, original_job_replayed=False)

    def snapshot(self):
        with self.locked() as state:
            return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', required=True)
    subs = parser.add_subparsers(dest='command', required=True)
    p = subs.add_parser('init'); p.add_argument('--owner', required=True); p.add_argument('--seconds', type=float, default=900); p.add_argument('--max-sessions', type=int, default=8)
    p = subs.add_parser('create-session'); p.add_argument('--owner', required=True); p.add_argument('--session', required=True); p.add_argument('--scope', choices=['text_only', 'tool_probe', 'repo_review'], default='text_only'); p.add_argument('--seconds', type=float, default=600); p.add_argument('--max-requests', type=int, default=3)
    p = subs.add_parser('status')
    p = subs.add_parser('dispatch-started'); p.add_argument('--owner', required=True); p.add_argument('--admission', required=True); p.add_argument('--adapter', required=True)
    p = subs.add_parser('confirm'); p.add_argument('--owner', required=True); p.add_argument('--admission', required=True); p.add_argument('--native-task-id', required=True); p.add_argument('--lease', type=float, default=180); p.add_argument('--save', required=True)
    p = subs.add_parser('admission-outcome'); p.add_argument('--owner', required=True); p.add_argument('--admission', required=True); p.add_argument('--outcome', choices=['not_started', 'confirmed_stopped', 'unknown'], required=True); p.add_argument('--evidence', required=True)
    p = subs.add_parser('resolve-inference'); p.add_argument('--owner', required=True); p.add_argument('--session', required=True); p.add_argument('--job', required=True); p.add_argument('--outcome', choices=['confirmed_not_started', 'confirmed_stopped'], required=True); p.add_argument('--evidence', required=True)
    for command in ('retry-admission', 'failover'):
        p = subs.add_parser(command); p.add_argument('--owner', required=True); p.add_argument('--session', required=True)
    args = parser.parse_args(); os.umask(0o077)
    if args.command == 'init':
        result = initialize(args.registry, args.owner, args.seconds, args.max_sessions)
    else:
        reg = Registry(args.registry)
        if args.command == 'create-session': result = reg.create_session(args.owner, args.session, args.scope, args.seconds, args.max_requests)
        elif args.command == 'status': result = reg.snapshot()
        elif args.command == 'dispatch-started': result = reg.dispatch_started(args.owner, args.admission, args.adapter)
        elif args.command == 'confirm':
            result = reg.confirm(args.owner, args.admission, args.native_task_id, args.lease)
            path = Path(args.save)
            if path.exists():
                if read(path, 8192) != result: raise QueueError('credential_file_conflict')
            else: protocol.atomic_json(path, result, exclusive=True)
        elif args.command == 'admission-outcome': result = reg.failed_admission(args.owner, args.admission, args.outcome, args.evidence)
        elif args.command == 'resolve-inference': result = reg.resolve_inference(args.owner, args.session, args.job, args.outcome, args.evidence)
        elif args.command == 'retry-admission': result = reg.retry_admission(args.owner, args.session)
        else: result = reg.failover(args.owner, args.session)
    print(encode(result).decode())


if __name__ == '__main__':
    try: main()
    except (QueueError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'error': getattr(exc, 'code', type(exc).__name__)}))
        raise SystemExit(1)
