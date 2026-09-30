"""Canonical, content-addressed envelopes. Hashes are integrity, not authentication."""
import copy
from functools import cached_property
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass

MAX_BYTES = 2 * 1024 * 1024
MAX_WIRE_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 128 * 1024
MAX_SESSION_SECONDS = 8 * 60 * 60
MAX_REQUESTS = 128
CONTRACT = 'dots-drive-objects/0'
KINDS = {'deployment', 'request', 'claim', 'started', 'result', 'receipt', 'ambiguity'}
ACTORS = {'deployment': 'controller', 'request': 'controller', 'receipt': 'controller',
          'claim': 'worker', 'started': 'worker', 'result': 'worker', 'ambiguity': 'worker'}
IDENTITY_KEYS = {'deployment_id', 'session_id', 'controller_id', 'worker_id',
                 'generation', 'assignment_epoch', 'native_task_id',
                 'controller_journal_id', 'worker_journal_id'}

class ProtocolError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise ProtocolError(code)


def canonical(value, *, max_bytes=MAX_BYTES):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                         allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ProtocolError('invalid_json') from None
    require(len(raw) <= max_bytes, 'object_too_large')
    return raw


def hash_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def valid_hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _pairs(pairs):
    value = {}
    for k, v in pairs:
        require(k not in value, 'duplicate_json_key')
        value[k] = v
    return value


@dataclass(frozen=True)
class Object:
    # Keep private canonical bytes rather than mutable nested dictionaries.
    raw: bytes

    @classmethod
    def make(cls, identity, kind, seq, deployment_id, payload, links=None):
        body = dict(contract=CONTRACT, identity=identity, kind=kind, seq=seq,
                    attempt=0 if kind == 'deployment' else 1, actor=ACTORS[kind],
                    deployment=deployment_id, links=links or {}, payload=payload)
        obj = cls(canonical(dict(object_id=hash_bytes(canonical(body)), body=body)))
        obj.validate()
        return obj

    @classmethod
    def parse(cls, raw):
        require(isinstance(raw, bytes) and len(raw) <= MAX_BYTES, 'object_too_large')
        obj = cls(raw)
        obj.validate()
        return obj

    @cached_property
    def _decoded(self):
        try:
            return json.loads(self.raw, object_pairs_hook=_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError('invalid_json')))
        except (ValueError, UnicodeError, RecursionError):
            raise ProtocolError('invalid_json') from None

    @property
    def value(self):
        return copy.deepcopy(self._decoded)

    @property
    def body(self):
        return copy.deepcopy(self._decoded['body'])

    @property
    def _body(self):
        # Internal read-only-by-convention view. Public body/value stay defensive.
        return self._decoded['body']

    @property
    def oid(self):
        return self._decoded['object_id']

    @property
    def slot(self):
        b = self._body
        # Content hash deliberately excluded: competing values conflict.
        return (b['identity']['deployment_id'], b['identity']['session_id'],
                b['identity']['generation'], b['identity']['assignment_epoch'],
                b['kind'], b['seq'], b['attempt'], b['actor'])

    def validate(self):
        v = self._decoded
        require(isinstance(v, dict) and set(v) == {'object_id', 'body'}, 'invalid_envelope')
        b = v['body']
        require(isinstance(b, dict) and set(b) == {'contract', 'identity', 'kind', 'seq',
                'attempt', 'actor', 'deployment', 'links', 'payload'}, 'invalid_body')
        require(b['contract'] == CONTRACT and isinstance(b['kind'], str) and b['kind'] in KINDS, 'unsupported_contract')
        ident = b['identity']
        require(isinstance(ident, dict) and set(ident) == IDENTITY_KEYS, 'invalid_identity')
        for key, value in ident.items():
            if key in {'generation', 'assignment_epoch'}:
                require(type(value) is int and 1 <= value <= 1000000, 'invalid_identity')
            else:
                require(isinstance(value, str) and re.fullmatch('[A-Za-z0-9_:/.-]{1,256}', value), 'invalid_identity')
        require(type(b['seq']) is int and 0 <= b['seq'] <= MAX_REQUESTS, 'invalid_sequence')
        require(b['actor'] == ACTORS[b['kind']], 'wrong_actor')
        require(type(b['attempt']) is int and b['attempt'] == (0 if b['kind'] == 'deployment' else 1), 'invalid_attempt')
        require(isinstance(b['payload'], dict) and isinstance(b['links'], dict), 'invalid_payload')
        require(all(isinstance(k, str) and valid_hash(v) for k, v in b['links'].items()), 'invalid_link')
        require(valid_hash(v['object_id']) and hash_bytes(canonical(b)) == v['object_id'], 'hash_mismatch')
        require(self.raw == canonical(v), 'noncanonical_object')
        if b['kind'] == 'deployment':
            p = b['payload']
            require(b['seq'] == 0 and b['deployment'] is None and not b['links'], 'invalid_deployment')
            require(set(p) == {'created', 'expires', 'max_requests', 'scope', 'ownership'}, 'invalid_deployment')
            require(type(p['created']) is int and type(p['expires']) is int and
                    1 <= p['expires'] - p['created'] <= MAX_SESSION_SECONDS, 'invalid_lifetime')
            require(type(p['max_requests']) is int and 1 <= p['max_requests'] <= MAX_REQUESTS and
                    p['scope'] in {'text_only','responses_tools'} and p['ownership'] == 'externally_pinned_single_writer',
                    'unsupported_deployment')
        else:
            require(b['seq'] >= 1 and valid_hash(b['deployment']), 'invalid_scope')
        return self


def deployment(session_id, native_task_id, *, controller_id='controller', worker_id='worker',
               generation=1, assignment_epoch=1, seconds=600, max_requests=3, scope='text_only', now=None):
    """Create a fresh-session definition; never a recovery/failover operation."""
    now = int(time.time()) if now is None else now
    identity = dict(deployment_id=uuid.uuid4().hex, session_id=session_id,
                    controller_id=controller_id, worker_id=worker_id,
                    generation=generation, assignment_epoch=assignment_epoch,
                    native_task_id=native_task_id, controller_journal_id=uuid.uuid4().hex,
                    worker_journal_id=uuid.uuid4().hex)
    return Object.make(identity, 'deployment', 0, None, dict(created=now, expires=now + seconds,
                       max_requests=max_requests, scope=scope, ownership='externally_pinned_single_writer'))
