"""One Global response wait budget; never controller or native authority.

An earlier local header can shorten this budget, but cannot renew a signed
lease, alter a request identity, or authorize another dispatch. Every HTTP stage
uses the same wall/monotonic deadline. CAS/setup/heartbeat policies are separate.
"""
import contextlib
from contextvars import ContextVar
from functools import lru_cache
import subprocess
import sys
import uuid
import math
import re
import socket
import threading
import time

from .model import ProtocolError, require

GLOBAL_RESPONSE_WAIT_SECONDS = 900
RESPONSE_DEADLINE_HEADER = 'X-Dots-Response-Deadline'


_RESPONSE_BUDGET = ContextVar('global_response_budget', default=None)
_FALLBACK_CLOCK_DOMAIN = 'process:' + uuid.uuid4().hex


@lru_cache(maxsize=1)
def clock_domain():
    """Stable across local processes in one boot; unknown platforms fail closed."""
    try:
        if sys.platform.startswith('linux'):
            with open('/proc/sys/kernel/random/boot_id', encoding='ascii') as source:
                value = source.read(128).strip()
        elif sys.platform == 'darwin':
            value = subprocess.check_output(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'],
                timeout=1, stderr=subprocess.DEVNULL).decode('ascii').strip()
        else:
            return _FALLBACK_CLOCK_DOMAIN
        if re.fullmatch(r'[A-Fa-f0-9-]{32,36}', value):
            return 'boot:' + value.lower()
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return _FALLBACK_CLOCK_DOMAIN


def current_response_deadline():
    return _RESPONSE_BUDGET.get()


@contextlib.contextmanager
def response_operation(budget):
    token = _RESPONSE_BUDGET.set(budget)
    try:
        budget.remaining('remote_wait_budget_expired')
        yield
    finally:
        _RESPONSE_BUDGET.reset(token)


def response_call(function, *args, **kwargs):
    """No follow-on provider call after an expired composed facade operation."""
    budget = current_response_deadline()
    if budget is not None: budget.remaining('remote_wait_budget_expired')
    value = function(*args, **kwargs)
    if budget is not None: budget.remaining('remote_wait_budget_expired')
    return value


def _timestamp(value):
    require(type(value) in (int, float) and math.isfinite(value) and 0 < value < 1e12,
            'invalid_global_response_deadline')
    return value


def parse_response_deadline(headers):
    values = headers.get_all(RESPONSE_DEADLINE_HEADER) or []
    if not values:
        return None
    require(len(values) == 1 and isinstance(values[0], str) and
            re.fullmatch(r'[0-9]{1,12}(?:\.[0-9]{1,9})?', values[0]) is not None,
            'invalid_global_response_deadline')
    return _timestamp(float(values[0]))


class ResponseDeadline:
    def __init__(self, *caps, seconds=GLOBAL_RESPONSE_WAIT_SECONDS, issued_at=None,
                 now=None, monotonic=None):
        self.now = now or time.time
        self.monotonic = monotonic or time.monotonic
        self.lock = threading.RLock()
        wall = self.now(); mono = self.monotonic()
        require(type(seconds) in (int, float) and math.isfinite(seconds) and
                0 < seconds <= 28800, 'invalid_global_response_budget')
        self.issued_at = wall if issued_at is None else _timestamp(issued_at)
        require(wall >= self.issued_at, 'global_response_clock_rollback')
        self.expires = min([self.issued_at + seconds, *(_timestamp(c) for c in caps if c is not None)])
        self.until = mono + max(0, self.expires - wall)
        self.observed_at = wall
        self.started_monotonic = mono
        self.failure = None

    def remaining(self, error='global_response_wait_budget_expired'):
        with self.lock:
            wall = self.now(); mono = self.monotonic()
            if wall < self.observed_at or mono < self.started_monotonic:
                self.failure = 'global_response_clock_rollback'
            self.observed_at = max(wall, self.observed_at)
            require(self.failure is None, self.failure or error)
            remaining = min(self.expires - wall, self.until - mono)
            if remaining <= 0:
                self.failure = error
            require(remaining > 0, error)
            return remaining

    def tighten(self, *caps):
        with self.lock:
            self.expires = min([self.expires, *(_timestamp(c) for c in caps if c is not None)])
            self.until = min(self.until, self.monotonic() + max(0, self.expires - self.now()))
            self.remaining()
            return self

    def checkpoint(self):
        """Same-host recovery retains the original monotonic cap, never now+900."""
        with self.lock:
            return {'issued_at': self.issued_at, 'expires': self.expires,
                    'until': self.until, 'observed_at': self.observed_at,
                    'started_monotonic': self.started_monotonic, 'clock_domain': clock_domain()}

    @classmethod
    def restore(cls, value, *, now=None, monotonic=None):
        require(isinstance(value, dict) and set(value) ==
                {'issued_at','expires','until','observed_at','started_monotonic','clock_domain'},
                'invalid_global_response_checkpoint')
        require(all(type(value[k]) in (int,float) and math.isfinite(value[k]) and value[k] >= 0
                    for k in value if k!='clock_domain') and
                0 < value['issued_at'] <= value['observed_at'] and
                value['issued_at'] < value['expires'] < 1e12 and
                value['started_monotonic'] <= value['until'] and isinstance(value['clock_domain'],str),
                'invalid_global_response_checkpoint')
        require(value['clock_domain']==clock_domain(),'global_response_clock_domain_changed')
        result = cls(value['expires'], issued_at=value['issued_at'], seconds=28800, now=now, monotonic=monotonic)
        result.until = min(result.until, value['until'])
        result.observed_at = max(result.observed_at, value['observed_at'])
        result.started_monotonic = value['started_monotonic']
        return result

    @contextlib.contextmanager
    def watch_socket(self, transport):
        """Bound an http.client operation even if its internal reads trickle.

        Socket timeouts alone restart for each internal recv. This local guard
        shuts down only this attempt's captured socket and never retries it.
        """
        self.remaining(); stop = threading.Event()
        def watch():
            while not stop.is_set():
                try: remaining = self.remaining()
                except ProtocolError:
                    try: transport.shutdown(socket.SHUT_RDWR)
                    except OSError: pass
                    return
                stop.wait(min(.1, remaining))
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            yield
        finally:
            stop.set(); watcher.join()
