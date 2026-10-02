"""One Mac Inbox writer and one loopback Responses listener for bounded routes.

HTTP timeout ends waiting, never native execution. Unknown writes reconcile the
same intent. Cached tool output has a durable one-use emission barrier, not an
exactly-once client/tool execution claim. No background heartbeat or inference.
"""
from __future__ import annotations
import copy
import fcntl
import http.server
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import threading
import time
from . import docs as control
from .protocol import (PROTOCOL, INBOX_MAX_BYTES, OUTBOX_MAX_BYTES, ProtocolError, canonical, sha256, require,
                       validate_grant, make_inbox, parse_inbox, parse_outbox, validate_result_envelope)
from .storage import Journal, private_read, private_write, fsync_dir
from .wire import (strict_json, validate_request, validate_response, validate_history,
                   response_events, has_tools, event_frame, call_binding)


def route_identity(identity):
    require(isinstance(identity, dict) and set(identity) == {'session-id', 'thread-id'}, 'canonical_runtime_identity_required')
    require(all(isinstance(v, str) and re.fullmatch(r'[!-~]{1,256}', v) for v in identity.values()), 'invalid_runtime_identity')
    return copy.deepcopy(identity)


class MacGateway:
    def __init__(self, state_dir, grant, key, docs, drive, *, create=False, wait_seconds=900, poll_seconds=1):
        self.grant = validate_grant(grant)
        require(type(wait_seconds) in (int, float) and 0 < wait_seconds <= 900, 'invalid_wait_budget')
        require(type(poll_seconds) in (int, float) and .01 <= poll_seconds <= 30, 'invalid_poll_interval')
        self.key, self.docs, self.drive = key, docs, drive
        self.wait_seconds, self.poll_seconds = wait_seconds, poll_seconds
        self._wall_anchor, self._mono_anchor = time.time(), time.monotonic()
        self.root = Path(state_dir).absolute()
        require(not any(p.is_symlink() for p in [self.root, *self.root.parents]), 'symlink_path_rejected')
        if create: self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(self.root.is_dir(), 'gateway_state_missing')
        info = self.root.stat()
        require(info.st_uid == os.getuid() and not info.st_mode & 0o077, 'private_gateway_directory_required')
        self._owner_fd = os.open(self.root / 'supervisor.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try: fcntl.flock(self._owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._owner_fd); self._owner_fd = None
            raise ProtocolError('gateway_already_running') from None
        self._lock, self._route_locks = threading.RLock(), {}
        self._admission_stop = threading.Event()
        try:
            if create:
                self.journal = Journal.create(self.root / 'mac', {'grant': self.grant, 'key_sha256': sha256(str(key).encode()),
                    'stopped': False, 'last_admission_wall': self._wall_anchor, 'inbox': None, 'write_intent': None, 'routes': {}, 'requests': {}, 'first_round_trip_verified': False})
            else: self.journal = Journal(self.root / 'mac')
            state = self.journal.read()
            require(state['grant'] == self.grant and state['key_sha256'] == sha256(str(key).encode()), 'gateway_authority_mismatch')
            self._cleanup_acknowledged(state)
        except BaseException:
            self.close(); raise

    def close(self):
        # Do not release writer ownership while a serialized Inbox operation is
        # still in flight. Outbox-only readers recheck _read before persistence.
        lock = getattr(self, '_lock', None)
        if lock is not None: lock.acquire()
        try:
            if getattr(self, '_owner_fd', None) is not None:
                fcntl.flock(self._owner_fd, fcntl.LOCK_UN); os.close(self._owner_fd); self._owner_fd = None
        finally:
            if lock is not None: lock.release()

    def _read(self):
        require(self._owner_fd is not None, 'gateway_closed')
        return self.journal.read()

    def _save(self, state):
        saved = self.journal.write(state)
        state['generation'] = saved['generation']
        return state

    def _blob(self, request_id, kind):
        require(re.fullmatch('[0-9a-f]{32}', request_id) is not None, 'invalid_local_request_id')
        return self.journal.directory / (request_id + '.' + kind + '.json')

    def _check_deadline(self, deadline):
        require(deadline is None or time.monotonic() < deadline, 'response_wait_expired_same_request_recoverable')

    def _new_allowed(self, state):
        require(not state['stopped'] and not self._admission_stop.is_set() and
                not (self.root / 'stop-requested.json').exists() and not (self.root / 'stop.json').exists(),
                'gateway_stopped_new_requests_blocked')
        now, elapsed = time.time(), time.monotonic() - self._mono_anchor
        require(elapsed >= 0 and now >= state['last_admission_wall'] - 1 and
                self.grant['created_at'] <= now < self.grant['expires_at'] and
                elapsed < self.grant['expires_at'] - self._wall_anchor, 'grant_expired_or_clock_unproven')
        state['last_admission_wall'] = max(now, state['last_admission_wall'])

    def _finish_intent(self, state, outcome):
        if outcome['status'] in {'accepted', 'applied'}:
            state['inbox'] = outcome['snapshot']; state['write_intent'] = None
            return self._save(state)
        raise ProtocolError('inbox_write_unknown_reconcile_same_operation' if outcome['status'] == 'unknown' else 'inbox_write_conflicting')

    def _reconcile(self, state, deadline=None):
        plan = state['write_intent']
        if plan is None: return state
        actual = self.docs.get_document(self.grant['inbox_id'], deadline=deadline)
        return self._finish_intent(state, control.reconcile_write(plan, actual))

    def _publish(self, state, *, deadline=None):
        self._check_deadline(deadline)
        state = self._reconcile(state, deadline)
        slots = [copy.deepcopy(r['slot']) for r in state['routes'].values() if r['slot']['outbox_id']]
        record = make_inbox(self.grant, slots, self.key, secrets.token_hex(16), state['stopped'])
        plan = control.plan_write(state['inbox'], record, record['operation_id'])
        state['write_intent'] = plan; state = self._save(state)
        try:
            actual = self.docs.batch_update_document(plan['document_id'], plan['body']['requests'], plan['body']['writeControl'], deadline=deadline)
        except Exception:
            return self._reconcile(state, deadline)
        outcome = control.accept_write(plan, actual)
        if outcome['status'] == 'accepted': return self._finish_intent(state, outcome)
        return self._reconcile(state, deadline)

    def initialize(self, *, deadline=None):
        with self._lock:
            state = self._read()
            if state['write_intent'] is not None: return self._reconcile(state, deadline)
            if state['inbox'] is not None:
                actual = self.docs.get_document(self.grant['inbox_id'], deadline=deadline)
                snap = control.snapshot(actual, self.grant['inbox_id'], state['inbox']['tab_id'])
                require(snap['text'] == state['inbox']['text'], 'inbox_changed_outside_owner')
                if snap['text'] == '\n':
                    self._new_allowed(state)
                    state['inbox'] = snap
                    self._publish(state, deadline=deadline)
                    return self.status()
                parse_inbox(snap, self.grant, self.key)
                state['inbox'] = snap; self._save(state)
                return self.status()
            self._new_allowed(state)
            snap = control.snapshot(self.docs.get_document(self.grant['inbox_id'], deadline=deadline), self.grant['inbox_id'])
            require(snap['text'] == '\n', 'fresh_blank_inbox_required')
            state['inbox'] = snap; state = self._save(state)
            self._publish(state, deadline=deadline)
            return self.status()

    def _ticket(self, request):
        return {k: request[k] for k in ('request_id', 'route_id', 'seq', 'request_sha256', 'state')}

    def submit(self, identity, raw_request, request_key=None, *, deadline=None, begin_before=None):
        self._check_deadline(deadline)
        identity = route_identity(identity)
        require(type(raw_request) is bytes and 0 < len(raw_request) <= self.grant['limits']['max_request_bytes'], 'wire_request_too_large')
        request = validate_request(strict_json(raw_request), max_bytes=self.grant['limits']['max_request_bytes'])
        pair = {'model': request['model'], 'reasoning_effort': request['reasoning']['effort']}
        require(pair in self.grant['allowed_pairs'], 'pair_not_authorized')
        require(request_key is None or isinstance(request_key, str) and re.fullmatch(r'[!-~]{1,256}', request_key), 'invalid_idempotency_key')
        digest = sha256(raw_request)
        identity_hash = sha256(canonical(identity))
        route_id = sha256(canonical({'activation_id': self.grant['activation_id'], 'identity': identity}))[:32]
        key_hash = sha256(request_key.encode()) if request_key is not None else None
        with self._lock:
            state = self._read()
            for old in state['requests'].values():
                if key_hash is not None and old['route_id'] == route_id and old['key_hash'] == key_hash:
                    require(old['request_sha256'] == digest, 'idempotency_key_payload_conflict')
                    return self._ticket(old)
            self._new_allowed(state)
            state = self._reconcile(state, deadline)
            require(state['inbox'] is not None, 'gateway_not_initialized')
            route = state['routes'].get(route_id)
            if route is None:
                require(len(state['routes']) < self.grant['limits']['max_routes'], 'route_capacity_exhausted')
                slot = {'route_id': route_id, 'identity_sha256': identity_hash, **pair, 'outbox_id': None, 'request': None, 'stop': False}
                route = {'slot': slot, 'creation': 'reserved', 'current_request_id': None, 'request_ids': [],
                         'outbox_snapshot': None, 'observed': None, 'admission_binding': None, 'quarantine': None, 'issued_calls': {}}
                state['routes'][route_id] = route; state = self._save(state)
                try: outbox_id = self.drive.create_document_once(self.grant['folder_id'], 'Dots lite Outbox ' + route_id, deadline=deadline)
                except Exception: raise ProtocolError('outbox_creation_unknown_no_automatic_retry') from None
                require(isinstance(outbox_id, str) and outbox_id and outbox_id != self.grant['inbox_id'] and
                        all(r['slot']['outbox_id'] != outbox_id for r in state['routes'].values()), 'outbox_resource_conflict')
                route = state['routes'][route_id]; route['slot']['outbox_id'] = outbox_id; route['creation'] = 'created'
                state = self._save(state)
            route = state['routes'][route_id]
            require(route['creation'] == 'created', 'outbox_creation_unknown_no_automatic_retry')
            require(route['quarantine'] is None, 'route_quarantined')
            require(all(route['slot'][k] == v for k, v in pair.items()), 'route_model_pair_changed')
            previous_id = route['current_request_id']
            previous_request, prior_outputs = None, []
            if previous_id is not None:
                previous = state['requests'][previous_id]
                require(previous['state'] == 'verified' and previous['delivery_started'], 'route_request_inflight_or_delivery_unknown')
                previous_request = strict_json(private_read(self._blob(previous_id, 'request'), self.grant['limits']['max_request_bytes']))
                prior_outputs.append(strict_json(private_read(self._blob(previous_id, 'result'), self.grant['limits']['max_result_bytes']))['output'])
            validate_history(request, previous_request, prior_outputs, route['issued_calls'])
            seq = len(route['request_ids']) + 1
            require(seq <= self.grant['limits']['max_requests_per_route'], 'route_request_quota_exhausted')
            rid = secrets.token_hex(16)
            begin_before = self.grant['expires_at'] if begin_before is None else min(begin_before, self.grant['expires_at'])
            require(type(begin_before) is int and time.time() < begin_before, 'request_begin_deadline_expired')
            private_write(self._blob(rid, 'request'), raw_request, immutable=True)
            job = {'request_id': rid, 'route_id': route_id, 'seq': seq, 'key_hash': key_hash, 'request_sha256': digest,
                   'byte_length': len(raw_request), 'begin_before': begin_before, 'state': 'reserved', 'file_id': None,
                   'result_locator': None, 'delivery_started': False, 'tool_delivery': False, 'upload_attempts': 0}
            state['requests'][rid] = job; route['request_ids'].append(rid); route['current_request_id'] = rid
            state = self._save(state)
            try:
                return self._upload_publish(state, rid, deadline)
            except ProtocolError as exc:
                exc.request_id = rid
                raise

    def _upload_publish(self, state, rid, deadline=None):
        self._check_deadline(deadline)
        job = state['requests'][rid]; route = state['routes'][job['route_id']]
        if job['file_id'] is None:
            require(job['upload_attempts'] < 3, 'request_upload_attempts_exhausted')
            raw = private_read(self._blob(rid, 'request'), self.grant['limits']['max_request_bytes'])
            require(sha256(raw) == job['request_sha256'] and len(raw) == job['byte_length'], 'saved_request_changed')
            job['upload_attempts'] += 1; job['state'] = 'upload_unknown'; state = self._save(state)
            try: file_id = self.drive.create_bytes(self.grant['folder_id'], 'request-' + rid + '.json', raw, None, deadline=deadline)
            except Exception: raise ProtocolError('request_upload_unknown_same_bytes_recoverable') from None
            require(isinstance(file_id, str) and file_id, 'request_upload_outcome_unknown')
            job = state['requests'][rid]; job['file_id'] = file_id; job['state'] = 'uploaded'; state = self._save(state)
        ack = None
        if job['seq'] > 1:
            prior = state['requests'][route['request_ids'][-2]]['result_locator']
            ack = {k: prior[k] for k in ('result_id', 'result_sha256')}
        descriptor = {k: job[k] for k in ('seq','request_id','request_sha256','byte_length','file_id','begin_before')}
        descriptor.update(folder_id=self.grant['folder_id'], previous_result_ack=ack)
        route['slot']['request'] = descriptor
        job['state'] = 'publish_unknown'; state = self._save(state)
        self._new_allowed(state)
        state = self._publish(state, deadline=deadline)
        state['requests'][rid]['state'] = 'published'; self._save(state)
        self._prune_acknowledged(state, rid)
        return self._ticket(state['requests'][rid])

    def _prune_acknowledged(self, state, rid):
        route = state['routes'][state['requests'][rid]['route_id']]
        if len(route['request_ids']) < 2: return
        # The successor's fully validated history and authenticated REQUEST ACK
        # supersede old raw caches. Keep only fixed compact descriptors/fences.
        for old_id in route['request_ids'][:-1]:
            old = state['requests'][old_id]
            require(old['state'] in {'verified', 'acknowledged'}, 'cannot_prune_unresolved_request')
            old['state'] = 'acknowledged'
        self._save(state)
        self._cleanup_acknowledged(state)

    def _cleanup_acknowledged(self, state):
        removed = False
        for old_id, old in state['requests'].items():
            if old['state'] != 'acknowledged': continue
            for kind in ('request', 'result'):
                path = self._blob(old_id, kind)
                if path.exists(): path.unlink(); removed = True
        if removed: fsync_dir(self.journal.directory)

    def recover_request(self, ticket, *, deadline=None, retry_upload=False):
        """Same identity only. An upload retry is explicit, bounded, identical bytes."""
        with self._lock:
            state = self._read(); job = self._get_job(state, ticket)
            if state['write_intent'] is not None:
                state = self._reconcile(state, deadline); job = state['requests'][job['request_id']]
                if job['state'] == 'publish_unknown':
                    job['state'] = 'published'; self._save(state)
                    self._prune_acknowledged(state, job['request_id'])
            if job['state'] in {'reserved', 'uploaded'} or retry_upload and job['state'] == 'upload_unknown':
                self._new_allowed(state)
                return self._upload_publish(state, job['request_id'], deadline)
            return self._ticket(job)

    def _get_job(self, state, ticket):
        require(isinstance(ticket, dict), 'invalid_request_ticket')
        job = state['requests'].get(ticket.get('request_id'))
        require(job is not None and all(ticket.get(k) == job[k] for k in ('request_id','route_id','seq','request_sha256')), 'request_ticket_mismatch')
        return job

    def poll(self, ticket, *, deadline=None):
        with self._lock:
            rid = self._get_job(self._read(), ticket)['route_id']
            lock = self._route_locks.setdefault(rid, threading.RLock())
        with lock:
            return self._poll_once(ticket, deadline=deadline)

    def _poll_once(self, ticket, *, deadline=None):
        with self._lock:
            state = self._read(); job = self._get_job(state, ticket)
            require(job['state'] != 'acknowledged', 'result_acknowledged_cache_pruned')
            if job['state'] == 'verified':
                raw = private_read(self._blob(job['request_id'], 'result'), self.grant['limits']['max_result_bytes'])
                require(sha256(raw) == job['result_locator']['result_sha256'], 'cached_result_changed')
                return strict_json(raw)['output']
            route = copy.deepcopy(state['routes'][job['route_id']]); job = copy.deepcopy(job)
        if job['state'] == 'publish_unknown': self.recover_request(ticket, deadline=deadline)
        if job['state'] not in {'published', 'publish_unknown'}: return None
        actual = self.docs.get_document(route['slot']['outbox_id'], deadline=deadline)
        snap = control.snapshot(actual, route['slot']['outbox_id'],
            route['outbox_snapshot']['tab_id'] if route['outbox_snapshot'] else None, OUTBOX_MAX_BYTES)
        if snap['text'] == '\n':
            require(route['observed'] is None, 'outbox_rollback_to_blank')
            return None
        record = parse_outbox(snap, route['slot'], self.key, self.grant)
        old = route['observed']
        binding = {k: record[k] for k in ('child_task_id','parent_task_id','spawn_operation_id','admission')}
        if route['admission_binding'] is not None:
            require(binding == route['admission_binding'], 'outbox_admission_changed')
        if old is not None:
            require(all(record[k] == old[k] for k in ('parent_task_id', 'spawn_operation_id')), 'outbox_spawn_binding_changed')
            require(record['consumed_seq'] >= old['consumed_seq'], 'outbox_sequence_rollback')
            if record['consumed_seq'] == old['consumed_seq'] and old['phase'] == 'BEGIN':
                require(record['begin_operation_id'] == old['begin_operation_id'] and record['request'] == old['request'],
                        'outbox_begin_binding_changed')
            if record['consumed_seq'] == old['consumed_seq'] and old['phase'] == 'RESULT':
                require(record == old, 'outbox_result_conflict')
        require(record['consumed_seq'] <= job['seq'], 'outbox_future_sequence')
        if record['phase'] in {'BEGIN', 'RESULT'} and record['consumed_seq'] == job['seq']:
            require(record['request'] == route['slot']['request'], 'outbox_current_request_mismatch')
        with self._lock:
            state = self._read(); current = state['routes'][job['route_id']]
            changed = current['outbox_snapshot'] != snap or current['observed'] != record
            current['outbox_snapshot'] = snap; current['observed'] = record
            if record['phase'] != 'SPAWN_RESERVED': current['admission_binding'] = binding
            if changed: self._save(state)
        if record['phase'] != 'RESULT' or record['consumed_seq'] != job['seq']: return None
        require(record['request'] == route['slot']['request'], 'result_request_descriptor_mismatch')
        locator = record['result']
        metadata = self.drive.get_metadata(locator['file_id'], deadline=deadline)
        require(isinstance(metadata, dict) and metadata.get('id') == locator['file_id'] and
                metadata.get('parents') == [self.grant['folder_id']] and metadata.get('trashed') is False and
                metadata.get('mimeType') == 'application/json', 'result_metadata_mismatch')
        if metadata.get('size') is not None:
            require(str(locator['byte_length']) == str(metadata['size']), 'result_metadata_size_mismatch')
        raw = self.drive.get_bytes(locator['file_id'], self.grant['limits']['max_result_bytes'], deadline=deadline)
        require(type(raw) is bytes and len(raw) == locator['byte_length'] and sha256(raw) == locator['result_sha256'], 'result_bytes_hash_mismatch')
        result = validate_result_envelope(strict_json(raw), self.grant, route['slot'], record)
        request = strict_json(private_read(self._blob(job['request_id'], 'request'), self.grant['limits']['max_request_bytes']))
        response = validate_response(result['output'], request, max_bytes=self.grant['limits']['max_result_bytes'])
        with self._lock:
            state = self._read(); current = self._get_job(state, ticket)
            private_write(self._blob(job['request_id'], 'result'), raw, immutable=True)
            current['state'] = 'verified'; current['result_locator'] = locator
            current['tool_delivery'] = has_tools(response); state['first_round_trip_verified'] = True
            issued = state['routes'][job['route_id']]['issued_calls']
            for item in response['output']:
                if item['type'] in {'function_call','custom_tool_call'}:
                    binding_hash = sha256(canonical(call_binding(item)))
                    require(item['call_id'] not in issued or issued[item['call_id']] == binding_hash, 'issued_call_id_conflict')
                    issued[item['call_id']] = binding_hash
            self._save(state)
        return response

    def delivery(self, ticket):
        with self._lock:
            state = self._read(); job = self._get_job(state, ticket)
            require(job['state'] == 'verified', 'result_not_verified')
            require(not job['tool_delivery'] or not job['delivery_started'], 'tool_delivery_unknown_do_not_replay')
            raw = private_read(self._blob(job['request_id'], 'result'), self.grant['limits']['max_result_bytes'])
            require(sha256(raw) == job['result_locator']['result_sha256'], 'cached_result_changed')
            response = strict_json(raw)['output']
            frames = response_events(response)
            # Persist BEFORE headers or any response tool frame reaches client.
            job['delivery_started'] = True; self._save(state)
            return frames

    def request_stop(self):
        self._admission_stop.set()
        private_write(self.root / 'stop-requested.json', canonical({'activation_id': self.grant['activation_id'], 'stop': True}), immutable=True)

    def stop(self, *, deadline=None):
        self.request_stop()
        with self._lock:
            state = self._read(); state['stopped'] = True; state = self._save(state)
            if state['inbox'] is not None: self._publish(state, deadline=deadline)
            return self.status()

    def status(self):
        with self._lock:
            state = self._read()
            return {'protocol': PROTOCOL, 'activation_id': self.grant['activation_id'], 'listener_ready': False,
                    'join_configured': state['inbox'] is not None and state['write_intent'] is None,
                    'first_round_trip_verified': state['first_round_trip_verified'], 'production_ready': False,
                    'stopped': state['stopped'], 'native_interruption_confirmed': False,
                    'routes': [{'route_id': rid, 'request_id': route['current_request_id'],
                                'state': state['requests'][route['current_request_id']]['state'] if route['current_request_id'] else route['creation']}
                               for rid, route in state['routes'].items()]}


class ResponsesServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    def __init__(self, gateway, port=0):
        self.gateway = gateway
        self.waiters = threading.BoundedSemaphore(gateway.grant['limits']['max_routes'] * 2)
        super().__init__(('127.0.0.1', port), ResponsesHandler)
    @property
    def base_url(self):
        return f'http://127.0.0.1:{self.server_port}/activations/{self.gateway.grant["activation_id"]}/v1'


class ResponsesHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'DotsLite/3'
    sys_version = ''
    def log_message(self, *_): pass
    def setup(self):
        super().setup(); self.connection.settimeout(5)
    def _headers(self):
        require(self.client_address[0] == '127.0.0.1' and self.headers.get_all('Host') == [f'127.0.0.1:{self.server.server_port}'], 'invalid_loopback_host')
        require(not any(self.headers.get_all(k) for k in ('Origin','Authorization','Cookie','Proxy-Authorization',
            'Sec-Fetch-Site','Transfer-Encoding','Upgrade')), 'forbidden_local_header')
        require(not self.headers.get_all('Content-Encoding') or self.headers.get_all('Content-Encoding') == ['identity'], 'unsupported_encoding')
    def _json(self, value, status=200):
        raw = canonical(value); self.send_response(status); self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw))); self.send_header('Connection','close'); self.end_headers()
        self.wfile.write(raw); self.close_connection = True
    def do_GET(self):
        try:
            self._headers()
            owner = self.server.gateway
            require(self.path in {'/health', '/activations/' + owner.grant['activation_id'] + '/health'}, 'unsupported_endpoint')
            self._json({**owner.status(), 'listener_ready': True})
        except ProtocolError as exc: self._json({'error': {'code': exc.code}}, 400)
    def do_POST(self):
        ticket, timer, acquired = None, None, False
        try:
            self._headers(); owner = self.server.gateway
            require(self.path == '/activations/' + owner.grant['activation_id'] + '/v1/responses', 'unsupported_endpoint')
            deadline = time.monotonic() + owner.wait_seconds
            acquired = self.server.waiters.acquire(blocking=False)
            require(acquired, 'http_waiter_capacity_exhausted')
            def end_wait():
                try: self.connection.shutdown(socket.SHUT_RDWR)
                except OSError: pass
            timer = threading.Timer(owner.wait_seconds + .05, end_wait)
            timer.daemon = True; timer.start()
            lengths = self.headers.get_all('Content-Length') or []
            require(len(lengths) == 1 and re.fullmatch(r'[0-9]{1,10}', lengths[0]), 'invalid_content_length')
            length = int(lengths[0]); require(0 < length <= owner.grant['limits']['max_request_bytes'], 'request_too_large')
            require(self.headers.get_content_type() == 'application/json', 'json_content_type_required')
            identity = {}
            require(not self.headers.get_all('session_id') and not self.headers.get_all('thread_id'), 'ambiguous_legacy_session_header')
            for name in ('session-id', 'thread-id'):
                values = self.headers.get_all(name) or []
                require(len(values) == 1, 'canonical_runtime_identity_required'); identity[name] = values[0]
            keys = self.headers.get_all('Idempotency-Key') or []
            require(len(keys) <= 1, 'duplicate_idempotency_key')
            self.connection.settimeout(min(5, max(.01, deadline-time.monotonic())))
            raw = self.rfile.read(length); require(len(raw) == length, 'truncated_request')
            ticket = owner.submit(identity, raw, keys[0] if keys else None, deadline=deadline)
            while time.monotonic() < deadline:
                if owner.poll(ticket, deadline=deadline) is not None: break
                time.sleep(min(owner.poll_seconds, max(0, deadline-time.monotonic())))
            else: raise ProtocolError('response_wait_expired_same_request_recoverable')
            frames = owner.delivery(ticket)
            self.connection.settimeout(max(.01, deadline-time.monotonic()))
            self.send_response(200); self.send_header('Content-Type','text/event-stream')
            self.send_header('Cache-Control','no-cache'); self.send_header('X-Request-ID',ticket['request_id'])
            self.send_header('Content-Length',str(len(frames))); self.send_header('Connection','close'); self.end_headers()
            self.wfile.write(frames); self.wfile.flush()
        except ProtocolError as exc:
            try: self._json({'error': {'code': exc.code}, 'request_id': ticket['request_id'] if ticket else getattr(exc, 'request_id', None),
                            'automatic_retry': False, 'native_fallback': False}, 409)
            except OSError: pass
        except (OSError, ValueError):
            try: self._json({'error': {'code': 'gateway_io_outcome_unknown'}, 'request_id': ticket['request_id'] if ticket else None,
                            'automatic_retry': False}, 503)
            except OSError: pass
        finally:
            if timer: timer.cancel()
            if acquired: self.server.waiters.release()
            self.close_connection = True
    def do_OPTIONS(self): self._json({'error': {'code': 'browser_access_not_supported'}}, 405)
