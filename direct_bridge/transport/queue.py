"""A durable, bounded, one-use native execution handoff, using only SQLite.

The caller supplies an already-authenticated Principal OUTSIDE wire arguments.
This module never authenticates credentials, starts a child, or executes a tool.
Claims never expire or move to another worker: ambiguity stops new execution.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time

MAX_BYTES = 1024 * 1024
MAX_TURNS = 128


def schema_key(value):
    require(isinstance(value, str) and 0 < len(value.encode('utf-8')) <= 512
            and all(ord(c) >= 32 for c in value), 'invalid_schema_key')
    return value


class QueueError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise QueueError(code)


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                          separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, UnicodeError):
        raise QueueError('invalid_json') from None


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_./:-]{1,256}', value), 'invalid_id')
    return value


@dataclass(frozen=True)
class Principal:
    """Trusted runtime identity, never accepted from a JSON body/header as-is."""
    actor_id: str


@dataclass(frozen=True)
class Binding:
    grant_id: str
    route_id: str
    session_id: str
    thread_id: str
    model: str
    reasoning_effort: str

    def validate(self):
        for value in asdict(self).values():
            identifier(value)
        require(self.reasoning_effort in ('low', 'medium', 'high', 'xhigh'), 'unsupported_effort')
        return self


@dataclass(frozen=True)
class RouteAuthorization:
    """Pinned trusted installation input; not a remotely callable grant action.

    approval_ref refers to actual user authorization verified by the host. A
    string in this dataclass is evidence metadata, NOT authorization by itself.
    worker_actor must be the actual native child task identity from admission.
    """
    binding: Binding
    client_actor: str
    worker_actor: str
    not_before: float
    expires_at: float
    approval_ref: str


class Queue:
    def __init__(self, path, *, clock=time.time):
        self.path = str(Path(path).absolute())
        self.clock = clock
        with self._tx() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS routes(
                    route_id TEXT PRIMARY KEY, authorization BLOB NOT NULL,
                    binding BLOB NOT NULL, client_actor TEXT NOT NULL,
                    worker_actor TEXT NOT NULL, not_before REAL NOT NULL,
                    expires_at REAL NOT NULL, last_clock REAL NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS schemas(
                    route_id TEXT NOT NULL REFERENCES routes(route_id),
                    name TEXT NOT NULL, digest TEXT NOT NULL, body BLOB NOT NULL,
                    PRIMARY KEY(route_id,name,digest));
                CREATE TABLE IF NOT EXISTS requests(
                    request_id TEXT PRIMARY KEY, route_id TEXT NOT NULL REFERENCES routes(route_id),
                    seq INTEGER NOT NULL, fingerprint TEXT NOT NULL,
                    payload BLOB NOT NULL, schema_refs BLOB NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('queued','claimed','completed','cancelled')),
                    cancelled INTEGER NOT NULL DEFAULT 0,
                    execution_reserved INTEGER NOT NULL DEFAULT 0,
                    result_id TEXT, result_hash TEXT, result BLOB,
                    UNIQUE(route_id,seq));
            ''')

    @contextmanager
    def _tx(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def install_route(self, authorization):
        """Local trusted setup only; not exposed by the tool surface."""
        a = authorization
        require(type(a) is RouteAuthorization, 'authorization_required')
        a.binding.validate()
        for value in (a.client_actor, a.worker_actor, a.approval_ref):
            identifier(value)
        require(a.client_actor != a.worker_actor, 'separate_actors_required')
        require(all(type(t) in (int, float) and math.isfinite(t) for t in (a.not_before, a.expires_at))
                and a.not_before < a.expires_at, 'invalid_authorization_window')
        raw = canonical(asdict(a))
        with self._tx() as db:
            old = db.execute('SELECT authorization FROM routes WHERE route_id=?', (a.binding.route_id,)).fetchone()
            if old:
                require(old['authorization'] == raw, 'route_authorization_immutable')
                return
            db.execute('INSERT INTO routes(route_id,authorization,binding,client_actor,worker_actor,not_before,expires_at,last_clock) '
                       'VALUES(?,?,?,?,?,?,?,?)', (a.binding.route_id, raw, canonical(asdict(a.binding)),
                       a.client_actor, a.worker_actor, a.not_before, a.expires_at, a.not_before))

    def _authorize(self, db, principal, binding, role, *, active=False):
        require(type(principal) is Principal and type(binding) is Binding, 'trusted_principal_and_binding_required')
        binding.validate()
        row = db.execute('SELECT * FROM routes WHERE route_id=?', (binding.route_id,)).fetchone()
        require(row is not None and row['binding'] == canonical(asdict(binding)), 'binding_not_authorized')
        require(principal.actor_id == row[role + '_actor'], 'actor_not_authorized')
        if active:
            now = self.clock()
            require(type(now) in (int, float) and math.isfinite(now), 'untrusted_clock')
            require(not row['revoked'], 'route_revoked')
            require(now >= row['last_clock'] - 1, 'clock_rollback_new_work_paused')
            require(row['not_before'] <= now < row['expires_at'], 'authorization_expired')
            db.execute('UPDATE routes SET last_clock=MAX(last_clock,?) WHERE route_id=?', (now, binding.route_id))
        return row

    @staticmethod
    def _request(db, binding, request_id):
        identifier(request_id)
        row = db.execute('SELECT * FROM requests WHERE request_id=? AND route_id=?',
                         (request_id, binding.route_id)).fetchone()
        require(row is not None, 'request_not_found')
        return row

    @staticmethod
    def _receipt(row):
        return {'request_id': row['request_id'], 'seq': row['seq'], 'state': row['state'],
                'cancel_requested': bool(row['cancelled']),
                'execution_reserved': bool(row['execution_reserved']), 'result_id': row['result_id'],
                'result_sha256': row['result_hash']}

    def put_schema(self, principal, binding, name, schema):
        schema_key(name)
        raw = canonical(schema)
        require(type(schema) is dict and len(raw) <= MAX_BYTES, 'invalid_or_oversize_schema')
        sha = hashlib.sha256(raw).hexdigest()
        with self._tx() as db:
            self._authorize(db, principal, binding, 'client', active=True)
            db.execute('INSERT OR IGNORE INTO schemas VALUES(?,?,?,?)', (binding.route_id, name, sha, raw))
        return sha

    def enqueue_request(self, principal, binding, request_id, seq, payload, *, previous_result_id=None, schema_refs=None):
        identifier(request_id)
        require(type(seq) is int and 1 <= seq <= MAX_TURNS, 'invalid_or_exhausted_sequence')
        raw = canonical(payload)
        require(type(payload) is dict and len(raw) <= MAX_BYTES, 'invalid_or_oversize_payload')
        refs = {} if schema_refs is None else schema_refs
        require(type(refs) is dict and len(refs) <= 1024, 'invalid_schema_refs')
        for name, sha in refs.items():
            schema_key(name)
            require(type(sha) is str and re.fullmatch('[0-9a-f]{64}', sha), 'invalid_schema_digest')
        fingerprint = digest({'binding': asdict(binding), 'seq': seq, 'payload': payload,
                              'schema_refs': refs, 'previous_result_id': previous_result_id})
        with self._tx() as db:
            self._authorize(db, principal, binding, 'client')
            old = db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
            if old:
                require(old['fingerprint'] == fingerprint and old['route_id'] == binding.route_id, 'request_id_conflict')
                return self._receipt(old)
            self._authorize(db, principal, binding, 'client', active=True)
            previous = db.execute('SELECT * FROM requests WHERE route_id=? ORDER BY seq DESC LIMIT 1', (binding.route_id,)).fetchone()
            require(seq == (previous['seq'] + 1 if previous else 1), 'sequence_conflict')
            require(not previous or previous['state'] in ('completed', 'cancelled'), 'previous_request_unsettled')
            require(previous_result_id == (previous['result_id'] if previous else None), 'previous_result_ack_required')
            for name, sha in refs.items():
                require(db.execute('SELECT 1 FROM schemas WHERE route_id=? AND name=? AND digest=?',
                                   (binding.route_id, name, sha)).fetchone(), 'schema_not_registered')
            db.execute('INSERT INTO requests(request_id,route_id,seq,fingerprint,payload,schema_refs,state) VALUES(?,?,?,?,?,?,?)',
                       (request_id, binding.route_id, seq, fingerprint, raw, canonical(refs), 'queued'))
            return self._receipt(self._request(db, binding, request_id))

    def claim_request(self, principal, binding, request_id):
        """Replayable payload acquisition; not an instruction to execute again."""
        with self._tx() as db:
            self._authorize(db, principal, binding, 'worker')
            row = self._request(db, binding, request_id)
            if row['state'] in ('completed', 'cancelled') or row['cancelled']:
                return self._receipt(row)
            self._authorize(db, principal, binding, 'worker', active=True)
            first = row['state'] == 'queued'
            if first:
                db.execute("UPDATE requests SET state='claimed' WHERE request_id=?", (request_id,))
                row = self._request(db, binding, request_id)
            return {**self._receipt(row), 'delivery': 'first' if first else 'replay',
                    'binding': asdict(binding), 'payload': json.loads(row['payload']),
                    'schema_refs': json.loads(row['schema_refs'])}

    def reserve_execution(self, principal, binding, request_id):
        """Once-only runtime gate, separate from retryable acquisition.

        Persist immediately BEFORE native execution admission. A lost admission
        result must reconcile the SAME actual child. Never reset this gate.
        This is a host adapter hook, not one more model-facing control tool.
        """
        with self._tx() as db:
            self._authorize(db, principal, binding, 'worker')
            row = self._request(db, binding, request_id)
            if row['execution_reserved']:
                return {**self._receipt(row), 'execute': False}
            self._authorize(db, principal, binding, 'worker', active=True)
            require(row['state'] == 'claimed' and not row['cancelled'], 'request_not_executable')
            db.execute('UPDATE requests SET execution_reserved=1 WHERE request_id=?', (request_id,))
            return {**self._receipt(self._request(db, binding, request_id)), 'execute': True}

    def read_schema(self, principal, binding, request_id, name, sha256):
        with self._tx() as db:
            self._authorize(db, principal, binding, 'worker', active=True)
            row = self._request(db, binding, request_id)
            require(row['state'] == 'claimed' and not row['cancelled'], 'request_not_executable')
            require(json.loads(row['schema_refs']).get(name) == sha256, 'schema_not_authorized_for_request')
            schema = db.execute('SELECT body FROM schemas WHERE route_id=? AND name=? AND digest=?',
                                (binding.route_id, name, sha256)).fetchone()
            require(schema is not None, 'schema_missing')
            return {'name': name, 'sha256': sha256, 'schema': json.loads(schema['body'])}

    def submit_result(self, principal, binding, request_id, result):
        raw = canonical(result)
        require(type(result) is dict and len(raw) <= MAX_BYTES, 'invalid_or_oversize_result')
        sha = hashlib.sha256(raw).hexdigest()
        result_id = 'result-' + digest([request_id, sha])
        with self._tx() as db:
            self._authorize(db, principal, binding, 'worker')
            row = self._request(db, binding, request_id)
            if row['result'] is not None:
                require(row['result_hash'] == sha, 'immutable_result_conflict')
                return self._receipt(row)
            require(row['state'] == 'claimed' and row['execution_reserved'], 'execution_not_reserved')
            state = 'cancelled' if row['cancelled'] else 'completed'
            db.execute('UPDATE requests SET state=?,result_id=?,result_hash=?,result=? WHERE request_id=?',
                       (state, result_id, sha, raw, request_id))
            return self._receipt(self._request(db, binding, request_id))

    def get_result(self, principal, binding, request_id):
        with self._tx() as db:
            self._authorize(db, principal, binding, 'client')
            row = self._request(db, binding, request_id)
            receipt = self._receipt(row)
            if row['state'] == 'completed':
                receipt['result'] = json.loads(row['result'])
            return receipt

    def cancel_request(self, principal, binding, request_id):
        with self._tx() as db:
            self._authorize(db, principal, binding, 'client')
            row = self._request(db, binding, request_id)
            if row['state'] == 'queued' or (row['state'] == 'claimed' and not row['execution_reserved']):
                db.execute("UPDATE requests SET cancelled=1,state='cancelled',result_id=? WHERE request_id=?",
                           ('cancelled-' + digest([request_id]), request_id))
            elif row['state'] == 'claimed':
                db.execute('UPDATE requests SET cancelled=1 WHERE request_id=?', (request_id,))
            return self._receipt(self._request(db, binding, request_id))

    def revoke_route(self, principal, binding):
        """Client-side stop: terminal queued work, cooperative running cancel."""
        with self._tx() as db:
            self._authorize(db, principal, binding, 'client')
            db.execute('UPDATE routes SET revoked=1 WHERE route_id=?', (binding.route_id,))
            db.execute("UPDATE requests SET cancelled=1 WHERE route_id=? AND state='claimed' AND execution_reserved=1", (binding.route_id,))
            rows = db.execute("SELECT request_id FROM requests WHERE route_id=? AND (state='queued' OR (state='claimed' AND execution_reserved=0))", (binding.route_id,)).fetchall()
            for row in rows:
                db.execute("UPDATE requests SET cancelled=1,state='cancelled',result_id=? WHERE request_id=?",
                           ('cancelled-' + digest([row['request_id']]), row['request_id']))
            return {'route_id': binding.route_id, 'revoked': True}
