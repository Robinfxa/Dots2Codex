"""Single-owner durable protocol. Native execution lives outside this module."""
import contextlib
import fcntl
import json
import os
import stat
import time
import uuid
from pathlib import Path
from .backend import fsync_dir, read_private_file
from .model import Object, ProtocolError, canonical, hash_bytes, require
from .selection import pin_selection, validate_request_selection

MAX_STATE = 96 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
TURN_RESERVE_BYTES = 3 * 1024 * 1024


def _save(path, state):
    raw = json.dumps(state, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
    require(len(raw) <= MAX_STATE, 'journal_size_exceeded')
    tmp = path.parent / ('.tmp-' + uuid.uuid4().hex)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as out:
            out.write(raw); out.flush(); os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    fsync_dir(path.parent)


class Journal:
    """Resume requires an existing intact state and the externally trusted pin."""
    @classmethod
    def provision(cls, root, pin, role):
        require(role in {'controller', 'worker'}, 'invalid_role')
        pin.validate()
        require(pin._body['kind'] == 'deployment', 'deployment_pin_required')
        root = Path(root)
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
        fd = os.open(root / 'lock', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        state = dict(version=1, pin=pin.oid, role=role,
                     incarnation=pin._body['identity'][role + '_journal_id'],
                     objects={pin.oid: pin.value}, published={}, uploads={},
                     requests={}, executions={}, deliveries={}, blocked=None, control_mode=None)
        _save(root / 'state.json', state)
        fsync_dir(root)
        return cls(root, pin, role)

    def __init__(self, root, pin, role):
        pin.validate()
        require(pin._body['kind'] == 'deployment' and role in {'controller', 'worker'}, 'invalid_journal_pin')
        self.root, self.pin, self.role = Path(root), pin, role
        require(not self.root.is_symlink(), 'unsafe_journal_directory')
        st = self.root.stat()  # Missing state is fatal; never mkdir on resume.
        require(stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077,
                'unsafe_journal_directory')
        with self.locked():
            pass

    @contextlib.contextmanager
    def locked(self):
        fd = os.open(self.root / 'lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            st = os.fstat(fd)
            require(stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077,
                    'unsafe_journal_lock')
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    require(time.monotonic() < deadline, 'journal_lock_timeout')
                    time.sleep(.01)
            try:
                state = json.loads(read_private_file(self.root / 'state.json', MAX_STATE))
            except (ValueError, UnicodeError):
                raise ProtocolError('corrupt_journal') from None
            require(isinstance(state, dict) and state.get('version') == 1 and
                    state.get('pin') == self.pin.oid and state.get('role') == self.role and
                    state.get('incarnation') == self.pin._body['identity'][self.role + '_journal_id'],
                    'journal_pin_mismatch')
            require(set(state) == {'version','pin','role','incarnation','objects','published',
                    'uploads','requests','executions','deliveries','blocked','control_mode'}, 'corrupt_journal')
            for key in ('objects', 'published', 'uploads', 'requests', 'executions', 'deliveries'):
                require(isinstance(state[key], dict), 'corrupt_journal')
            require(self.pin.oid in state['objects'], 'missing_deployment_pin')
            _validate_state(state, self.pin, self.role)
            yield state
        finally:
            os.close(fd)

    def save(self, state):
        _validate_state(state, self.pin, self.role)
        _save(self.root / 'state.json', state)


def _check_payload(obj):
    b=obj._body
    p,links=b['payload'],b['links']
    kind = b['kind']
    if kind == 'deployment':
        return
    if kind == 'request' and set(p) == {'responses_request', 'scope'}:
        require(p['scope'] in {'text_only','responses_tools'} and isinstance(p['responses_request'], dict), 'invalid_responses_payload')
        from .wire import validate_remote_request
        validate_remote_request(p['responses_request'],p['scope'])
    elif kind == 'result' and set(p)=={'response_result'}:
        require(isinstance(p['response_result'],dict),'invalid_response_result')
    elif kind in {'request', 'result'}:
        require(set(p) == {'text'} and isinstance(p['text'], str) and 1 <= len(p['text']) <= 16384,
                'invalid_text_payload')
    elif kind == 'claim':
        require(p == {'status': 'claimed'}, 'invalid_claim')
    elif kind == 'started':
        expected = hash_bytes(canonical(dict(request=links.get('request'),
                              incarnation=b['identity']['worker_journal_id'])))
        require(p == {'dispatch_id': expected, 'status': 'dispatch_intent'}, 'invalid_started')
    elif kind == 'receipt':
        require(set(p) == {'status', 'evidence'} and p['status'] == 'delivered' and
                isinstance(p['evidence'], str) and 1 <= len(p['evidence']) <= 1024,
                'invalid_receipt')
    elif kind == 'ambiguity':
        require(p == {'status': 'execution_unknown'}, 'invalid_ambiguity')
    wanted = {'request': set() if b['seq'] == 1 else {'previous_receipt'},
              'claim': {'request'}, 'started': {'request', 'claim'},
              'result': {'request', 'started'}, 'receipt': {'request', 'result'},
              'ambiguity': {'request', 'started'}}[kind]
    require(set(links) == wanted, 'invalid_link_shape')


def _check_request_binding(payload, pin):
    selection = pin_selection(pin)
    if 'responses_request' in payload:
        validate_request_selection(payload['responses_request'], selection)
    else:
        require(selection is None, 'selected_session_requires_explicit_responses_request')


def _index(values, pin):
    slots, objects = {}, {}
    for oid, val in values.items():
        obj = Object.parse(canonical(val))
        require(oid == obj.oid and obj._body['identity'] == pin._body['identity'], 'object_pin_mismatch')
        require((obj._body['kind'] == 'deployment' and oid == pin.oid) or
                (obj._body['kind'] != 'deployment' and obj._body['deployment'] == pin.oid), 'deployment_hash_mismatch')
        require(obj._body['seq'] <= pin._body['payload']['max_requests'], 'request_budget_exceeded')
        _check_payload(obj)
        if obj._body['kind'] == 'request':
            _check_request_binding(obj._body['payload'], pin)
        if 'scope' in obj._body['payload']:
            require(obj._body['payload']['scope']==pin._body['payload']['scope'],'request_scope_mismatch')
        if 'response_result' in obj._body['payload']:
            require(pin._body['payload']['scope']=='responses_tools','text_only_result_required')
        require(obj.slot not in slots or slots[obj.slot] == oid, 'semantic_slot_conflict')
        slots[obj.slot] = oid
        objects[oid] = obj
    # Validate available references even when unrelated references are delayed.
    memo={};bodies={oid:obj._body for oid,obj in objects.items()}
    for obj in objects.values():
        _chain(obj, objects, set(), memo, bodies)
    return objects


def _validate_state(state, pin, role):
    mode=state['control_mode']
    require(mode is None or (isinstance(mode,dict) and set(mode)=={'document_id','tab_id','control_id','deployment_hash'} and
            mode['deployment_hash']==pin.oid and all(isinstance(v,str) and 1<=len(v)<=256 for v in mode.values())),
            'invalid_control_mode_marker')
    objects = _index(state['objects'], pin)
    own = [o for o in objects.values() if o._body['actor'] == role]
    require(state['blocked'] is None or isinstance(state['blocked'], str), 'corrupt_journal')
    require(set(state['published']) <= set(state['uploads']), 'publication_index_corrupt')
    for oid, upload in state['uploads'].items():
        require(oid in objects and objects[oid]._body['actor'] == role and
                isinstance(upload, dict) and set(upload) == {'reservation', 'status'} and
                upload['status'] in {'intent', 'verified'} and
                (upload['reservation'] is None or isinstance(upload['reservation'], str)),
                'upload_index_corrupt')
        require((oid in state['published']) == (upload['status'] == 'verified'), 'publication_index_corrupt')
    if role == 'controller':
        require(not state['executions'], 'wrong_role_index')
        requests = state['requests']
        require(len(requests) == len(set(requests.values())) and
                set(requests.values()) == {o.oid for o in own if o._body['kind'] == 'request'},
                'request_index_corrupt')
        require({objects[oid]._body['seq'] for oid in requests.values()} == set(range(1,len(requests)+1)),
                'request_sequence_corrupt')
        require(set(state['deliveries'].values()) == {o.oid for o in own if o._body['kind'] == 'receipt'},
                'delivery_index_corrupt')
        for seq, oid in state['deliveries'].items():
            require(oid in objects and str(objects[oid]._body['seq']) == seq and
                    objects[oid]._body['kind'] == 'receipt', 'delivery_index_corrupt')
    else:
        require(not state['requests'] and not state['deliveries'], 'wrong_role_index')
        executions = state['executions']
        require(set(executions) == {str(i) for i in range(1,len(executions)+1)}, 'execution_index_corrupt')
        require({e.get('started') for e in executions.values() if isinstance(e,dict)} ==
                {o.oid for o in own if o._body['kind'] == 'started'}, 'execution_history_missing')
        result_ids = set()
        for seq, e in executions.items():
            require(isinstance(e, dict) and e.get('phase') in {'dispatch_intent', 'result_saved'}, 'execution_index_corrupt')
            keys = {'phase', 'request', 'started', 'dispatch_id'}
            if e['phase'] == 'result_saved': keys.add('result')
            require(set(e) == keys and e['started'] in objects and e['request'] in objects,
                    'execution_index_corrupt')
            started = objects[e['started']]
            require(str(started._body['seq']) == seq and started._body['kind'] == 'started' and
                    started._body['links']['request'] == e['request'] and
                    started._body['payload']['dispatch_id'] == e['dispatch_id'], 'execution_index_corrupt')
            if 'result' in e:
                require(e['result'] in objects and objects[e['result']]._body['kind'] == 'result' and
                        objects[e['result']]._body['links'] == {'request':e['request'], 'started':e['started']},
                        'execution_result_index_corrupt')
                result_ids.add(e['result'])
        require(result_ids == {o.oid for o in own if o._body['kind'] == 'result'}, 'execution_result_index_corrupt')
    return objects


def _chain(obj, objects, visiting, memo=None, bodies=None):
    memo={} if memo is None else memo
    bodies={oid:o._body for oid,o in objects.items()} if bodies is None else bodies
    require(obj.oid not in visiting, 'cyclic_object_graph')
    if obj.oid in memo:return memo[obj.oid]
    visiting = visiting | {obj.oid}
    b = bodies[obj.oid]
    complete = True
    for name, oid in b['links'].items():
        if oid not in objects:
            complete = False
            continue
        ref = objects[oid]
        expected_kind = 'receipt' if name == 'previous_receipt' else name
        expected_seq = b['seq'] - 1 if name == 'previous_receipt' else b['seq']
        require(bodies[oid]['kind'] == expected_kind and bodies[oid]['seq'] == expected_seq,
                'reference_kind_or_sequence_mismatch')
        if name not in {'request', 'previous_receipt'} and 'request' in bodies[oid]['links']:
            require(bodies[oid]['links']['request'] == b['links']['request'], 'reference_request_mismatch')
        complete = _chain(ref, objects, visiting, memo, bodies) and complete
    if b['kind']=='result' and b['links'].get('request') in objects:
        req=bodies[b['links']['request']]['payload']
        if 'responses_request' in req:
            from .wire import result_item
            result_item(b['payload'],req['responses_request'],b['links']['request'],req['scope'])
    memo[obj.oid]=complete
    return complete


class _Participant:
    def __init__(self, journal, backend, role):
        require(journal.role == role, 'wrong_journal_role')
        self.journal, self.backend, self.pin = journal, backend, journal.pin
        with journal.locked() as state:self._control_guard(state)

    def _control_guard(self,state):
        require(state['control_mode'] is None or state['control_mode']==getattr(self,'_control_mode',None),
                'cas_enrolled_journal_requires_controlled_adapter')

    def _live(self):
        require(time.time() < self.pin._body['payload']['expires'], 'deployment_expired')

    def _add(self, state, kind, seq, payload, links=None):
        obj = Object.make(self.pin._body['identity'], kind, seq, self.pin.oid, payload, links)
        proposed = dict(state['objects'], **{obj.oid: obj.value})
        _index(proposed, self.pin)
        state['objects'] = proposed
        return obj

    def _publish(self, state, obj, *, recovery=False):
        self._control_guard(state)
        if obj.oid in state['published']:
            return state['published'][obj.oid]
        if obj.oid in state['uploads']:
            require(recovery, 'publication_outcome_unknown')
            reservation = state['uploads'][obj.oid]['reservation']
        else:
            # Bytes are already durable before reservation or any mutating upload.
            self.journal.save(state)
            reservation = self.backend.reserve()
            state['uploads'][obj.oid] = dict(reservation=reservation, status='intent')
            self.journal.save(state)
        result = self.backend.publish(obj, reservation)
        state['published'][obj.oid] = result
        state['uploads'][obj.oid]['status'] = 'verified'
        self.journal.save(state)
        return result

    def publish_deployment(self):
        require(self.journal.role == 'controller', 'wrong_journal_role')
        with self.journal.locked() as s:
            return self._publish(s, self.pin)

    def reconcile(self):
        # Keep local lock during bounded I/O: same host cannot race its own state.
        with self.journal.locked() as s:
            self._control_guard(s)
            require(s['blocked'] is None, 'session_blocked:' + str(s['blocked']))
            try:
                observed = self.backend.scan(self.pin._body['identity']['deployment_id'])
            except ProtocolError as exc:
                if str(exc) in {'hash_mismatch', 'object_name_mismatch', 'noncanonical_object',
                                'duplicate_json_key', 'invalid_envelope', 'invalid_body',
                                'invalid_json', 'invalid_identity', 'wrong_actor'}:
                    s['blocked'] = str(exc); self.journal.save(s)
                raise
            proposed = dict(s['objects'])
            try:
                for obj in observed:
                    require(obj._body['actor'] != self.journal.role or obj.oid in s['objects'],
                            'own_role_unissued_object')
                    proposed[obj.oid] = obj.value
                objects = _index(proposed, self.pin)
            except ProtocolError as exc:
                s['blocked'] = str(exc); self.journal.save(s)
                raise
            # Commit only after the complete scan + graph validation succeeds.
            s['objects'] = proposed
            for obj in observed:
                if obj.oid in s['uploads']:
                    s['published'].setdefault(obj.oid, 'observed_exact_bytes')
                    s['uploads'][obj.oid]['status'] = 'verified'
            self.journal.save(s)
            return objects

    def recover_publication(self, object_id):
        """Explicit retry of exact saved bytes. Never returns an invocation permit."""
        with self.journal.locked() as s:
            self._control_guard(s)
            require(s['blocked'] is None, 'session_blocked')
            require(object_id in s['objects'] and object_id in s['uploads'], 'unknown_upload_intent')
            obj = Object.parse(canonical(s['objects'][object_id]))
            require(obj._body['actor'] == self.journal.role, 'wrong_object_author')
            return self._publish(s, obj, recovery=True)

    def poll(self, check, *, attempts=4, initial_delay=.25, max_delay=2, sleep=time.sleep):
        require(type(attempts) is int and 1 <= attempts <= 8, 'invalid_poll_budget')
        require(type(initial_delay) in (int,float) and type(max_delay) in (int,float) and
                0 < initial_delay <= max_delay <= 5, 'invalid_poll_delay')
        for index in range(attempts):
            value = check()
            if value is not None:
                return value
            if index + 1 < attempts:
                sleep(min(max_delay, initial_delay * (2 ** index)))
        return None  # pending, not failed, not unstarted


class Controller(_Participant):
    def __init__(self, journal, backend):
        super().__init__(journal, backend, 'controller')

    def submit(self, text, idempotency_key):
        return self._submit_payload({'text':text}, idempotency_key)

    def submit_request(self, request, idempotency_key):
        return self._submit_payload({'responses_request':request, 'scope':self.pin._body['payload']['scope']}, idempotency_key)

    def _submit_payload(self, payload, idempotency_key):
        _check_request_binding(payload, self.pin)
        require(isinstance(idempotency_key, str) and 1 <= len(idempotency_key) <= 128,
                'invalid_idempotency_key')
        with self.journal.locked() as s:
            self._control_guard(s)
            self._live(); require(s['blocked'] is None, 'session_blocked')
            if idempotency_key in s['requests']:
                oid = s['requests'][idempotency_key]
                require(s['objects'][oid]['body']['payload'] == payload, 'idempotency_payload_conflict')
                return oid  # Caller uses reconcile/explicit publication recovery.
            require(sum(len(canonical(v)) for v in s['objects'].values()) + TURN_RESERVE_BYTES <= MAX_TRANSCRIPT_BYTES,
                    'session_transcript_budget_exceeded')
            seq = len(s['requests']) + 1
            require(seq <= self.pin._body['payload']['max_requests'], 'request_budget_exceeded')
            links = {}
            if seq > 1:
                require(str(seq-1) in s['deliveries'], 'previous_delivery_unconfirmed')
                links['previous_receipt'] = s['deliveries'][str(seq-1)]
                require(links['previous_receipt'] in s['published'], 'previous_receipt_unpublished')
            obj = self._add(s, 'request', seq, payload, links)
            s['requests'][idempotency_key] = obj.oid
            self.journal.save(s)
            self._publish(s, obj)
            return obj.oid

    def result(self, request_id):
        objects = self.reconcile()
        require(request_id in objects and objects[request_id]._body['kind'] == 'request', 'unknown_request')
        for obj in objects.values():
            if obj._body['kind'] == 'result' and obj._body['links']['request'] == request_id:
                if _chain(obj, objects, set()):
                    return obj
        return None

    def record_delivery(self, request_id, result_id, evidence):
        objects = self.reconcile()
        require(result_id in objects and objects[result_id]._body['kind'] == 'result' and
                objects[result_id]._body['links']['request'] == request_id and
                _chain(objects[result_id], objects, set()), 'unverified_result')
        with self.journal.locked() as s:
            self._control_guard(s)
            require(s['blocked'] is None, 'session_blocked')
            seq = objects[result_id]._body['seq']
            obj = self._add(s, 'receipt', seq, {'status':'delivered', 'evidence':evidence},
                            {'request':request_id, 'result':result_id})
            s['deliveries'][str(seq)] = obj.oid
            self.journal.save(s)
            self._publish(s, obj)
            return obj.oid


class Worker(_Participant):
    def __init__(self, journal, backend):
        super().__init__(journal, backend, 'worker')

    def start_next(self):
        self.reconcile()
        with self.journal.locked() as s:
            self._control_guard(s)
            self._live(); require(s['blocked'] is None, 'session_blocked')
            objects = _index(s['objects'], self.pin)
            executions = s['executions']
            recorded = {e['started'] for e in executions.values()}
            require(all(o.oid in recorded for o in objects.values() if o._body['kind'] == 'started'),
                    'execution_history_missing')
            for execution in executions.values():
                require(execution['phase'] == 'result_saved', 'execution_outcome_unknown')
            seq = len(executions) + 1
            requests = [o for o in objects.values() if o._body['kind'] == 'request' and o._body['seq'] == seq]
            if not requests:
                return None
            req = requests[0]
            if not _chain(req, objects, set()):
                return None
            if seq > 1:
                previous = executions[str(seq-1)]
                require(previous['result'] in s['published'], 'previous_result_unpublished')
            claim = self._add(s, 'claim', seq, {'status':'claimed'}, {'request':req.oid})
            self.journal.save(s)
            self._publish(s, claim)
            self._live()
            dispatch_id = hash_bytes(canonical(dict(request=req.oid, incarnation=s['incarnation'])))
            started = self._add(s, 'started', seq, {'status':'dispatch_intent', 'dispatch_id':dispatch_id},
                                {'request':req.oid, 'claim':claim.oid})
            executions[str(seq)] = dict(phase='dispatch_intent', request=req.oid,
                                       started=started.oid, dispatch_id=dispatch_id)
            # This commit is the irreversible boundary. Every later exception is ambiguous.
            self.journal.save(s)
            self._publish(s, started)
            self._live()
            permit = dict(request_id=req.oid, dispatch_id=dispatch_id, request=req._body['payload'],
                          native_task_id=self.pin._body['identity']['native_task_id'])
            if 'inference' in self.pin._body['payload']:
                permit['inference_binding'] = self.pin.body['payload']['inference']
            return permit

    def complete(self, permit, text):
        with self.journal.locked() as s:
            self._control_guard(s)
            require(s['blocked'] is None, 'session_blocked')
            require(isinstance(permit, dict), 'invalid_permit')
            require(permit.get('native_task_id') == self.pin._body['identity']['native_task_id'], 'native_identity_mismatch')
            if 'inference' in self.pin._body['payload']:
                require(permit.get('inference_binding') == self.pin._body['payload']['inference'],
                        'permit_inference_binding_mismatch')
            matches = [(seq,e) for seq,e in s['executions'].items() if e['request'] == permit.get('request_id')]
            require(len(matches) == 1, 'unknown_execution')
            seq, execution = matches[0]
            require(execution['dispatch_id'] == permit.get('dispatch_id'), 'dispatch_id_mismatch')
            payload={'text':text} if isinstance(text,str) else {'response_result':text}
            require(isinstance(text,str) or self.pin._body['payload']['scope']=='responses_tools','text_only_result_required')
            obj = self._add(s, 'result', int(seq), payload,
                            {'request':execution['request'], 'started':execution['started']})
            if execution['phase'] == 'result_saved':
                require(execution['result'] == obj.oid, 'result_conflict')
            execution.update(phase='result_saved', result=obj.oid)
            self.journal.save(s)
            self._publish(s, obj)
            return obj.oid

    def mark_ambiguous(self, request_id):
        with self.journal.locked() as s:
            self._control_guard(s)
            matches = [(seq,e) for seq,e in s['executions'].items() if e['request'] == request_id]
            require(len(matches) == 1 and matches[0][1]['phase'] == 'dispatch_intent', 'unknown_execution')
            seq,e = matches[0]
            obj = self._add(s, 'ambiguity', int(seq), {'status':'execution_unknown'},
                            {'request':request_id, 'started':e['started']})
            self.journal.save(s)
            self._publish(s, obj)
            return obj.oid
