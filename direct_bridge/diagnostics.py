"""Bounded, best-effort local event diagnostics. Never a payload/raw-log sink.

Only compile-time enums, bounded numbers, and salted logical-ID references are
stored. The shared random salt is correlation data, not an access credential.
Diagnostic failures never change protocol execution or authorize a retry.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import itertools
import queue
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

VERSION = 1
MAX_FILE_BYTES = 256 * 1024
MAX_LINE_BYTES = 2048
MAX_LINES = 200
MAX_QUEUED_EVENTS = 256
EVENT_FILES = ('events.jsonl.1', 'events.jsonl')
METHODS = frozenset({'bridge_status', 'get_request', 'discover_tools', 'lookup_schema',
    'submit_action_and_wait_result', 'await_result', 'finish_request', 'cancel_request',
    'prepare_hosted_call', 'record_hosted_result', 'responses', 'unknown'})
OPERATIONS = frozenset({'mcp', 'runtime_wait', 'response_commit', 'http', 'service'})
STAGES = frozenset({'begin', 'queued', 'invoke_begin', 'invoke_end', 'end', 'ingested',
    'delivery_fenced', 'socket_written', 'socket_flushed', 'timeout', 'disconnect', 'error', 'started', 'stopping', 'stopped'})
OUTCOMES = frozenset({'ok', 'error', 'timeout', 'cancelled', 'pending', 'ready', 'closed',
    'already_completed', 'completed', 'too_late_result_committed', 'unknown', 'tools', 'message'})
REASONS = frozenset({'deadline', 'cooperative_cancel', 'service_closed', 'coroutine_cancelled',
    'socket_error', 'validation', 'runtime', 'shutdown'})
ERRORS = frozenset({'runtime_error', 'request_timeout', 'unknown_tool', 'arguments_too_large',
    'invalid_arguments', 'too_many_inflight_calls', 'invalid_runtime_result', 'result_too_large',
    'outcome_pending', 'bridge_closed', 'request_cancelled', 'authorization_required',
    'delivery_outcome_unknown_no_reemission', 'http_waiter_capacity_exhausted',
    'action_binding_mismatch', 'action_id_conflict', 'action_id_required', 'unknown_route',
    'unknown_request', 'route_claim_required', 'native_route_not_claimed', 'stale_request',
    'context_receipt_required', 'request_already_answered_or_stale',
    'request_cancelled_or_route_closed', 'previous_request_unsettled',
    'previous_response_not_emitted', 'incomplete_body', 'invalid_wait_ms',
    'invalid_after_seq', 'cursor_ahead_of_route', 'authorization_expired',
    'clock_rollback_new_work_paused'})
ENUMS = {'operation': OPERATIONS, 'method': METHODS, 'stage': STAGES,
         'outcome': OUTCOMES, 'reason': REASONS, 'error_code': ERRORS}
NUMBERS = {'monotonic_ms': 10**15, 'event_seq': 10**15, 'duration_ms': 86400000,
           'wait_ms': 300000, 'after_seq': 1024, 'http_status': 599}
REFS = frozenset({'process_ref', 'call_ref', 'route_ref', 'request_ref', 'action_ref'})
UTC_PATTERN = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z')
REF_PATTERN = re.compile(r'[0-9a-f]{24}')


def sanitize(value):
    """Defense in depth for both writer and reader. Never stringify unknown data."""
    if type(value) is not dict or value.get('version') != VERSION:
        return None
    result = {'version': VERSION}
    if type(value.get('utc')) is not str or not UTC_PATTERN.fullmatch(value['utc']):
        return None
    result['utc'] = value['utc']
    for key, choices in ENUMS.items():
        item = value.get(key)
        if type(item) is str and item in choices:
            result[key] = item
    if not {'operation', 'stage'} <= result.keys():
        return None
    for key, maximum in NUMBERS.items():
        item = value.get(key)
        if type(item) is int and 0 <= item <= maximum:
            result[key] = item
    for key in REFS:
        item = value.get(key)
        if type(item) is str and REF_PATTERN.fullmatch(item):
            result[key] = item
    return result


def _directory(path, create=False):
    path = Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise OSError('unsafe_diagnostic_directory')
    parent = path.parent.stat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise OSError('unsafe_diagnostic_parent')
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise OSError('unsafe_diagnostic_directory')
    return fd


def _file(directory, name, flags):
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        os.close(fd)
        raise OSError('unsafe_diagnostic_file')
    return fd


def _read(directory, name, maximum):
    fd = _file(directory, name, os.O_RDONLY)
    try:
        if os.fstat(fd).st_size > maximum:
            raise OSError('diagnostic_file_too_large')
        return os.read(fd, maximum + 1)
    finally:
        os.close(fd)


class Diagnostics:
    def __init__(self, state_directory):
        self.path = Path(state_directory) / 'diagnostics'
        self.process_ref = secrets.token_hex(12)
        self._salt = None
        self._seq = itertools.count(1)
        self._queue = queue.Queue(maxsize=MAX_QUEUED_EVENTS)
        self._closed = False
        try:
            self._writer = threading.Thread(target=self._run, name='direct-safe-diagnostics', daemon=True)
            self._writer.start()
        except Exception:
            self._closed = True  # A missing diagnostic worker must not stop the service.

    def _prepare(self, directory):
        if self._salt is None:
            try:
                raw = _read(directory, 'correlation-salt.bin', 32)
            except FileNotFoundError:
                fd = _file(directory, 'correlation-salt.bin', os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                try:
                    raw = secrets.token_bytes(32)
                    if os.write(fd, raw) != len(raw):
                        raise OSError('diagnostic_salt_write_failed')
                finally:
                    os.close(fd)
            if len(raw) != 32:
                raise OSError('invalid_diagnostic_salt')
            self._salt = raw

    def event(self, operation, stage, *, ids=None, **fields):
        """Enqueue fixed metadata only. Never wait for filesystem I/O or queue space.

        ids accepts only logical IDs, never authentication or context tokens.
        The bounded in-memory queue may drop records. Unknown fields and enum
        values are discarded before enqueueing; the writer revalidates them.
        """
        try:
            if self._closed:
                return
            value = {**fields, 'version': VERSION, 'operation': operation, 'stage': stage,
                'utc': datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
                'monotonic_ms': int(time.monotonic() * 1000),
                'event_seq': next(self._seq), 'process_ref': self.process_ref}
            event = sanitize(value)
            if event is None:
                return
            logical_ids = {}
            if type(ids) is dict:
                for key in ('call', 'route', 'request', 'action'):
                    raw = ids.get(key)
                    if type(raw) is str and 0 < len(raw) <= 256:
                        logical_ids[key] = raw
            # Queue.put_nowait waits for its mutex. Use nonblocking admission so
            # even a busy producer/consumer cannot hold up the protocol path.
            if not self._queue.mutex.acquire(blocking=False):
                return
            try:
                if self._queue._qsize() >= MAX_QUEUED_EVENTS:
                    return
                self._queue._put((event, logical_ids))
                self._queue.unfinished_tasks += 1
                self._queue.not_empty.notify()
            finally:
                self._queue.mutex.release()
        except Exception:
            pass

    def _run(self):
        while not self._closed or not self._queue.empty():
            try:
                value, ids = self._queue.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                self._write(value, ids)
            except Exception:
                pass  # Neither failures nor raw exceptions leave the writer.
            finally:
                self._queue.task_done()

    def close(self):
        # Daemon writer may drain already queued events; protocol shutdown never waits.
        self._closed = True

    def wait_idle(self, timeout=2):
        """Bounded test synchronization only; protocol/diagnostic CLI never call it."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._queue.mutex:
                if self._queue.unfinished_tasks == 0:
                    return True
            time.sleep(.001)
        return False

    def _write(self, value, ids):
        directory = _directory(self.path, create=True)
        try:
            lock = _file(directory, 'writer.lock', os.O_RDWR | os.O_CREAT)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._prepare(directory)
                for key, raw in ids.items():
                    value[key + '_ref'] = hmac.new(self._salt,
                        (key + ':' + raw).encode('utf-8'), hashlib.sha256).hexdigest()[:24]
                event = sanitize(value)
                if event is None:
                    return
                raw = (json.dumps(event, sort_keys=True, separators=(',', ':')) + '\n').encode('ascii')
                if len(raw) > MAX_LINE_BYTES:
                    return
                fd = _file(directory, 'events.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT)
                try:
                    if os.fstat(fd).st_size > MAX_FILE_BYTES:
                        raise OSError('diagnostic_file_too_large')
                    if os.fstat(fd).st_size + len(raw) > MAX_FILE_BYTES:
                        os.close(fd)
                        fd = None
                        try:
                            check = _file(directory, 'events.jsonl.1', os.O_RDONLY)
                            os.close(check)
                        except FileNotFoundError:
                            pass
                        os.replace('events.jsonl', 'events.jsonl.1', src_dir_fd=directory, dst_dir_fd=directory)
                        fd = _file(directory, 'events.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL)
                    os.write(fd, raw)
                finally:
                    if fd is not None:
                        os.close(fd)
            finally:
                os.close(lock)
        finally:
            os.close(directory)


def recent(state_directory, lines=80):
    """Read without creating directories/files, acquiring a writer lock or a service call."""
    if type(lines) is not int or not 1 <= lines <= MAX_LINES:
        return {'status': 'invalid_line_limit', 'events': []}
    result = deque(maxlen=lines)
    try:
        directory = _directory(Path(state_directory) / 'diagnostics')
    except FileNotFoundError:
        return {'status': 'not_available', 'events': []}
    except Exception:
        return {'status': 'unavailable', 'events': []}
    incomplete = False
    try:
        for name in EVENT_FILES:
            try:
                raw = _read(directory, name, MAX_FILE_BYTES)
            except FileNotFoundError:
                continue
            except Exception:
                incomplete = True
                continue
            for line in raw.splitlines():
                if len(line) > MAX_LINE_BYTES:
                    incomplete = True
                    continue
                try:
                    event = sanitize(json.loads(line))
                except (ValueError, UnicodeError, RecursionError):
                    event = None
                if event is not None:
                    result.append(event)
                else:
                    incomplete = True
    finally:
        os.close(directory)
    return {'status': 'partial' if incomplete else 'available' if result else 'not_available',
            'events': list(result)}


def duration_ms(started):
    return min(86400000, max(0, int((time.monotonic() - started) * 1000)))
