"""Local, explicitly approved credential reuse for the global launcher.

This module does not create keys, search credential stores, source shell files,
connect to a service, or modify a tunnel profile. All values stay in process
memory or in the one user-approved private.env file outside any checkout.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
import getpass
import json
import os
from pathlib import Path
import secrets
import stat
import sys
from typing import Mapping
import warnings

KEY_NAME = "CONTROL_PLANE_API_KEY"
BEARER_NAME = "DOTS_BRIDGE_HTTP_BEARER"
ENV_NAME = "private.env"
BEARER_FILE = "http-bearer"
DEFAULT_STATE = Path.home() / "Library/Application Support/Dots2Codex Direct"
MAX_BYTES = 32768
_ALLOWED = frozenset((KEY_NAME, BEARER_NAME))
_SOURCE = Path(__file__).resolve().parent.parent
_SAFE_ENV = frozenset(("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL",
                       "TERM", "COLORTERM", "SYSTEMROOT", "TZ", "USER", "LOGNAME",
                       "SHELL", "NO_COLOR", "CODEX_HOME"))


class CredentialError(ValueError):
    """Messages describe the failure, never a supplied credential or file line."""


def _valid(name: str, value: object) -> bool:
    minimum = 16 if name == BEARER_NAME else 1
    return (type(value) is str and minimum <= len(value) <= 8192
            and value.isascii() and all(33 <= ord(char) <= 126 for char in value))


def _validate(name: str, value: object) -> str:
    if name not in _ALLOWED or not _valid(name, value):
        raise CredentialError("Invalid credential data; values were not displayed.")
    return value


def parse_private_env(text: str) -> dict[str, str]:
    """Parse a deliberately small DATA format; never eval/source/interpolate it.

    A line is NAME=JSON_STRING (preferred) or NAME=unquoted-visible-ASCII.
    Only the two exact names above are accepted. Comments and empty lines are
    ignored. Shell exports, duplicate names, multiline values and other fields
    are rejected. '$' and backticks in a value remain literal bytes.
    """
    try:
        valid = type(text) is str and len(text.encode("utf-8")) <= MAX_BYTES
    except UnicodeError:
        valid = False
    if not valid:
        raise CredentialError("Invalid private.env data.")
    values = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, separator, encoded = line.partition("=")
        if not separator or name not in _ALLOWED or name in values:
            raise CredentialError("Invalid private.env format or unexpected field.")
        try:
            value = json.loads(encoded) if encoded.startswith('"') else encoded
        except (ValueError, TypeError):
            raise CredentialError("Invalid private.env format.") from None
        values[name] = _validate(name, value)
    return values


def _serialize(values: Mapping[str, str]) -> bytes:
    if not set(values) <= _ALLOWED:
        raise CredentialError("Unexpected credential field.")
    text = "# Dots2Codex private data. Do not source this file in a shell.\n"
    for name in sorted(values):
        text += name + "=" + json.dumps(_validate(name, values[name]), ensure_ascii=True) + "\n"
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise CredentialError("Private credential data is too large.")
    return text.encode("utf-8")


def _git_metadata(marker: Path) -> bool:
    # Empty .git placeholders are not repositories (some test sandboxes mount
    # them on /tmp). Real repositories/worktrees have a gitdir file or metadata.
    return (marker.is_symlink() or marker.is_file() or
            any((marker / name).exists() for name in ("HEAD", "config", "objects")))


def private_state_path(state_dir: os.PathLike | str) -> Path:
    path = Path(state_dir).expanduser()
    if ".." in path.parts:
        raise CredentialError("Use a private directory without parent-path components.")
    path = path.absolute()
    if path == Path("/") or path == _SOURCE or _SOURCE in path.parents:
        raise CredentialError("Credential storage must be outside the source bundle.")
    if any(_git_metadata(parent / ".git") for parent in (path, *path.parents)):
        raise CredentialError("Credential storage must be outside every Git checkout.")
    return path


@contextmanager
def _directory(path: Path, *, create: bool = False):
    """Walk with openat/O_NOFOLLOW; bind all later operations to a directory fd."""
    fd = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open("/", flags)
        for index, part in enumerate(path.parts[1:]):
            final = index == len(path.parts) - 2
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                os.mkdir(part, 0o700, dir_fd=fd)
                next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            info = os.fstat(fd)
            if info.st_uid not in (0, os.getuid()):
                raise CredentialError("Private storage has an untrusted directory owner.")
            if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
                raise CredentialError("Private storage has a writable parent directory.")
            if final and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
                raise CredentialError("Credential directory must be owned by you with mode 700.")
        yield fd
    except OSError:
        raise CredentialError("Private credential directory is unsafe or unavailable; check ownership, permissions and symlinks.") from None
    finally:
        if fd is not None:
            os.close(fd)


@dataclass(frozen=True, repr=False)
class _Snapshot:
    data: bytes = field(repr=False)
    identity: tuple = field(repr=False)


def _stat_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _check_file_info(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or info.st_size > MAX_BYTES):
        raise CredentialError("Credential files must be regular, unlinked, owned by you, mode 600, and size-bounded.")


def _read_file(directory: int | None, name: str) -> _Snapshot | None:
    if directory is None:
        return None
    fd = None
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        try:
            fd = os.open(name, flags, dir_fd=directory)
        except FileNotFoundError:
            return None
        before = os.fstat(fd)
        _check_file_info(before)
        data = b""
        while len(data) <= MAX_BYTES:
            block = os.read(fd, min(8192, MAX_BYTES + 1 - len(data)))
            if not block:
                break
            data += block
        after = os.fstat(fd)
        if len(data) > MAX_BYTES or _stat_identity(before) != _stat_identity(after):
            raise CredentialError("Credential file changed while being read; retry after resolving the change.")
        return _Snapshot(data, _stat_identity(after))
    except OSError:
        raise CredentialError("Credential file is unsafe or unreadable; no values were displayed.") from None
    finally:
        if fd is not None:
            os.close(fd)


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeError:
        raise CredentialError("Credential file is not valid UTF-8 data.") from None


def _sources(path: Path):
    with _directory(path) as directory:
        saved = _read_file(directory, ENV_NAME)
        bearer_file = _read_file(directory, BEARER_FILE)
    values = parse_private_env(_decode(saved.data)) if saved is not None else {}
    bearer = None
    if bearer_file is not None:
        # Allow one conventional final newline, not surrounding whitespace.
        bearer = _decode(bearer_file.data)
        if bearer.endswith("\n"):
            bearer = bearer[:-1]
        bearer = _validate(BEARER_NAME, bearer)
    return values, bearer, saved


def safe_child_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Minimal OS environment; drop shell hooks and unrelated model credentials."""
    source = os.environ if environ is None else environ
    result = {key: value for key, value in source.items()
              if key in _SAFE_ENV or key.startswith("LC_")}
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    result["PYTHONNOUSERSITE"] = "1"
    return result


@dataclass(frozen=True, repr=False)
class Credentials:
    control_plane_api_key: str = field(repr=False)
    http_bearer: str = field(repr=False)
    sources: Mapping[str, str] = field(default_factory=dict, repr=False)
    _base_env: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        _validate(KEY_NAME, self.control_plane_api_key)
        _validate(BEARER_NAME, self.http_bearer)
        if self.control_plane_api_key == self.http_bearer:
            raise CredentialError("The local HTTP bearer must be separate from the tunnel control-plane key.")

    def __repr__(self):
        return "Credentials(<redacted>)"

    @property
    def local_bearer(self) -> str:
        return self.http_bearer

    def bridge_env(self, base_env: Mapping[str, str] | None = None) -> dict[str, str]:
        env = safe_child_env(self._base_env if base_env is None else base_env)
        env[BEARER_NAME] = self.http_bearer
        return env

    def codex_env(self, base_env: Mapping[str, str] | None = None) -> dict[str, str]:
        return self.bridge_env(base_env)

    def tunnel_env(self, base_env: Mapping[str, str] | None = None) -> dict[str, str]:
        # Official tunnel owns stdio and passes the bearer to its MCP child.
        # start_mcp.sh MUST unset KEY_NAME before invoking the Python interpreter.
        env = self.bridge_env(base_env)
        env[KEY_NAME] = self.control_plane_api_key
        return env


def _resolve(environ, saved, bearer):
    values, sources = {}, {}
    for name in (KEY_NAME, BEARER_NAME):
        if name in environ:
            values[name], sources[name] = _validate(name, environ[name]), "environment"
        elif name in saved:
            values[name], sources[name] = saved[name], "private.env"
        elif name == BEARER_NAME and bearer is not None:
            values[name], sources[name] = bearer, "http-bearer"
    return values, sources


def load_credentials(state_dir=DEFAULT_STATE, environ=None) -> Credentials:
    """Read only: use environment, exact private.env, then exact http-bearer."""
    path = private_state_path(state_dir)
    base = dict(os.environ if environ is None else environ)
    saved, bearer, _ = _sources(path)
    values, sources = _resolve(base, saved, bearer)
    missing = _ALLOWED - values.keys()
    if missing:
        raise CredentialError("Missing " + ", ".join(sorted(missing)) + "; run the interactive first-use credential setup in a Terminal.")
    return Credentials(values[KEY_NAME], values[BEARER_NAME], sources, safe_child_env(base))


def _interactive():
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise CredentialError("Credential entry or storage approval needs an interactive Terminal; piped approvals are not accepted.")


def _hidden(name):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            value = getpass.getpass("Existing authorized " + name + " (hidden; never paste into chat): ")
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        raise CredentialError("Secure credential entry cancelled or unavailable; nothing was saved.") from None
    return _validate(name, value)


def _confirm(message: str, word: str) -> bool:
    print(message, flush=True)
    try:
        return input("Type " + word + " to approve, or press Return to decline: ").strip() == word
    except (EOFError, KeyboardInterrupt):
        raise CredentialError("Credential storage action cancelled; nothing was changed.") from None


@contextmanager
def _storage_lock(directory):
    name = ".credentials.lock"
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory)
    except FileExistsError:
        raise CredentialError("Another credential storage action may be running; existing lock was preserved.") from None
    try:
        os.close(fd)
        yield
    finally:
        os.unlink(name, dir_fd=directory)


def _store(path: Path, values: Mapping[str, str], expected: _Snapshot | None):
    data = _serialize(values)
    temp_name = ".private-env-" + secrets.token_hex(16)
    with _directory(path, create=True) as directory, _storage_lock(directory):
        if _read_file(directory, ENV_NAME) != expected:
            raise CredentialError("Credential file changed after review; replacement was refused.")
        fd = None
        created = False
        try:
            fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory)
            created = True
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                fd = None
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if _read_file(directory, ENV_NAME) != expected:
                raise CredentialError("Credential file changed after review; replacement was refused.")
            if expected is None:
                # Atomic no-clobber creation. Never overwrite a newly appeared file.
                os.link(temp_name, ENV_NAME, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                os.unlink(temp_name, dir_fd=directory)
            else:
                os.replace(temp_name, ENV_NAME, src_dir_fd=directory, dst_dir_fd=directory)
            created = False
            os.fsync(directory)
        except OSError:
            raise CredentialError("Credential save failed or is unconfirmed; check the named private file locally before retrying.") from None
        finally:
            if fd is not None:
                os.close(fd)
            if created:
                os.unlink(temp_name, dir_fd=directory)


def prepare_credentials(state_dir=DEFAULT_STATE, environ=None, interactive=True) -> Credentials:
    """Reuse first, request only missing values, and offer exact-path local SAVE.

    Declining SAVE retains credentials in this invocation only. Callers starting
    a supervisor must pass tunnel_env() as its process environment, never argv or
    JSON. No key is generated. Existing private files are never silently updated.
    """
    if not interactive:
        return load_credentials(state_dir, environ)
    path = private_state_path(state_dir)
    base = dict(os.environ if environ is None else environ)
    saved, bearer, snapshot = _sources(path)
    values, sources = _resolve(base, saved, bearer)
    desired = dict(saved)
    for name in (KEY_NAME, BEARER_NAME):
        if name not in values:
            _interactive()
            values[name], sources[name] = _hidden(name), "hidden-entry"
        if saved.get(name) == values[name] or (name == BEARER_NAME and name not in saved and bearer == values[name]):
            continue
        desired[name] = values[name]
    if values[KEY_NAME] == values[BEARER_NAME]:
        raise CredentialError("The local HTTP bearer must be separate from the tunnel control-plane key.")
    if desired != saved:
        _interactive()
        changed = sorted(name for name in desired if desired[name] != saved.get(name))
        action = "Replace the reviewed existing file" if snapshot is not None else "Create a new file"
        approved = _confirm(
            action + " at exactly:\n  " + str(path / ENV_NAME) + "\n"
            "Save existing credential values for: " + ", ".join(changed) + ".\n"
            "This enables future launches without re-entry. The directory is mode 700 and the file mode 600.\n"
            "The tunnel key is passed only to the tunnel; it is never written into Codex provider settings.\n"
            "The local HTTP bearer may be copied to private Codex settings only after separate configuration approval.\n"
            "Declining keeps these values in this run only. No credential value is displayed.", "SAVE")
        if approved:
            _store(path, desired, snapshot)
            for name in changed:
                sources[name] = "private.env"
            print("Saved the approved private credential file. No credential values were displayed.", flush=True)
        else:
            print("No credential file was changed; values are available for this run only.", flush=True)
    return Credentials(values[KEY_NAME], values[BEARER_NAME], sources, safe_child_env(base))


def forget_saved_credentials(state_dir=DEFAULT_STATE, *, include_http_bearer=False) -> bool:
    """Explicit local removal only. This neither revokes keys nor runs on stop."""
    path = private_state_path(state_dir)
    names = [ENV_NAME] + ([BEARER_FILE] if include_http_bearer else [])
    with _directory(path) as directory:
        snapshots = {name: _read_file(directory, name) for name in names}
    snapshots = {name: value for name, value in snapshots.items() if value is not None}
    if not snapshots:
        return False
    _interactive()
    if not _confirm("Remove only these saved local credential copies:\n" +
                    "\n".join("  " + str(path / name) for name in snapshots) + "\n"
                    "Keys are NOT revoked at the service. A running process and approved Codex configuration may still have a copy.\n"
                    "Stop the bridge and restore its client settings first if you want to disconnect it.\n"
                    "Future starts may require hidden credential entry again. This cannot be undone by this tool.", "CLEAR"):
        return False
    with _directory(path) as directory, _storage_lock(directory):
        if any(_read_file(directory, name) != snapshot for name, snapshot in snapshots.items()):
            raise CredentialError("Credential file changed after review; removal was refused.")
        for name in snapshots:
            os.unlink(name, dir_fd=directory)
        os.fsync(directory)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local private credential setup; never supply credential values as arguments.")
    parser.add_argument("action", choices=("setup", "check", "clear"))
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE))
    parser.add_argument("--include-http-bearer", action="store_true", help="With clear, separately include the exact http-bearer file")
    args = parser.parse_args(argv)
    if args.include_http_bearer and args.action != "clear":
        parser.error("--include-http-bearer applies only to clear")
    try:
        if args.action == "clear":
            removed = forget_saved_credentials(args.state_dir, include_http_bearer=args.include_http_bearer)
            print("Approved local copies removed; service credentials were not revoked." if removed else "No saved credential copies were removed.")
        else:
            action = prepare_credentials if args.action == "setup" else load_credentials
            action(args.state_dir)
            print("Required credentials are available; values are hidden.")
        return 0
    except CredentialError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("Private credential operation failed; no values were displayed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
