"""Optional, payload-free local timings; never evidence of remote execution.

Set DOTS_CONNECTOR_TIMING=1 for a helper process to append to timings.jsonl in
its existing private worker runtime. Completed records are fsynced. This is a
best-effort diagnostic, not a journal: full, unsafe, busy or failed sinks drop
events without changing a protocol operation's result or exception. A crash can
leave no event or a final incomplete line. No recovery decision may use this file.

Only fixed stage/status labels and monotonic integers enter the event schema.
Arguments, return values, exception messages, identities and paths are never
inspected or serialized. No wall-clock timestamps or remote calls are recorded.
Durations are inclusive for nested stages and exclude their own append/fsync;
outer durations include inner instrumentation overhead. Monotonic values may be
compared only on the same host/boot, and cannot measure native/connector time.
"""
import fcntl
import functools
import json
import os
import stat
import time


TIMING_ENV = 'DOTS_CONNECTOR_TIMING'
TIMING_FILE = 'timings.jsonl'
MAX_TIMING_BYTES = 8 * 1024 * 1024
STAGES = frozenset({
    'connector.poll', 'connector.tick', 'connector.accept', 'connector.input',
    'connector.result', 'connector.status', 'control.observe', 'control.plan',
    'journal.save', 'object.read', 'object.save',
})


def _append(root, stage, started, duration, status):
    """Append one allowlisted record to a bounded, private, anchored file."""
    if (stage not in STAGES or status not in {'ok', 'error'} or
            type(started) is not int or type(duration) is not int or duration < 0):
        return
    raw = (json.dumps({'stage': stage, 'started_monotonic_ns': started,
                       'duration_ns': duration, 'status': status},
                      sort_keys=True, separators=(',', ':')) + '\n').encode('ascii')
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            return
        flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(TIMING_FILE, flags | os.O_CREAT | os.O_EXCL, 0o600,
                         dir_fd=directory)
        except FileExistsError:
            fd = os.open(TIMING_FILE, flags, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_mode & 0o077 or info.st_nlink != 1):
                return
            # Never wait for another diagnostic writer or take the protocol lock.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            size = os.fstat(fd).st_size
            if size + len(raw) > MAX_TIMING_BYTES:
                return
            # Preserve a crash-truncated tail rather than joining it to a new row.
            if size and os.pread(fd, 1, size - 1) != b'\n':
                return
            if os.write(fd, raw) != len(raw):
                return
            os.fsync(fd)
            # Also covers a file created by an earlier failed diagnostic write.
            os.fsync(directory)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def _record(worker, stage, started, status):
    try:
        finished = time.monotonic_ns()
        _append(worker.root, stage, started, finished - started, status)
    except BaseException:
        # Even an interrupted sink must not mask the original operation outcome,
        # especially after input exposure or result persistence became durable.
        pass


def timed_stage(stage):
    """Measure a worker method without reading its arguments or return value."""
    if stage not in STAGES:
        raise ValueError('unknown_timing_stage')

    def decorate(function):
        @functools.wraps(function)
        def measured(worker, *args, **kwargs):
            try:
                enabled = os.environ.get(TIMING_ENV) == '1'
                started = time.monotonic_ns() if enabled else None
            except BaseException:
                started = None
            if started is None:
                return function(worker, *args, **kwargs)
            try:
                result = function(worker, *args, **kwargs)
            except BaseException:
                _record(worker, stage, started, 'error')
                raise
            _record(worker, stage, started, 'ok')
            return result
        return measured
    return decorate
