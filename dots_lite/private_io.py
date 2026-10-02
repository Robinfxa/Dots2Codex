"""Private filesystem primitives extracted from the MIT-licensed Dots2Codex
remote_transport/backend.py and global_gateway.py. No legacy runtime imports.
"""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
from .protocol import ProtocolError, require, canonical, hash_bytes

def private_dir(path, create=False):
    path = Path(path).expanduser().absolute()
    # Reject symlink traversal, including parents. Never silently resolve a link.
    require(not any(p.is_symlink() for p in [path, *path.parents]), 'symlink_path_rejected')
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077, 'private_directory_required')
    return path


def private_write(path, data):
    path = Path(path)
    private_dir(path.parent)
    temp = path.parent / ('.write-' + secrets.token_hex(12))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data); out.flush(); os.fsync(out.fileno())
        require(not path.is_symlink(), 'symlink_path_rejected')
        os.replace(temp, path); fsync_dir(path.parent)
    finally:
        if temp.exists(): temp.unlink()


def strict_json(raw):
    def pairs(items):
        value = {}
        for k, v in items:
            require(k not in value, 'duplicate_json_key'); value[k] = v
        return value
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError('invalid_json')))
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError('invalid_json') from None


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_private_file(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and
                not info.st_mode & 0o077 and info.st_size <= limit, 'unsafe_local_file')
        with os.fdopen(fd, 'rb', closefd=False) as f:
            raw = f.read(limit + 1)
        require(len(raw) <= limit, 'local_file_too_large')
        return raw
    finally:
        os.close(fd)


def no_symlinks(path):
    path = Path(path).expanduser().absolute()
    require(not any(p.is_symlink() for p in [path, *path.parents]), 'symlink_path_rejected')
    return path


def save(path, value):
    private_write(path, canonical(value))


def read(path, limit=4*1024*1024):
    no_symlinks(path)
    return strict_json(read_private_file(path, limit))


@contextlib.contextmanager
def private_lock(path):
    path = no_symlinks(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and not info.st_mode & 0o077 and info.st_nlink == 1, 'unsafe_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProtocolError('operation_in_progress') from None
        yield
    finally:
        os.close(fd)
