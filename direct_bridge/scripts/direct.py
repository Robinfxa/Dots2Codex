#!/usr/bin/env python3
"""User-operated setup UI. Standard library only; no inference or secret storage.

Configuration is committed as one private directory. Tunnel/profile access is
reviewed separately; this script never invokes init or edits global settings.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import stat
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote
import uuid

ROOT = Path(__file__).resolve().parents[1]
SDK_VERSION = "1.29.0"
GUIDE = "https://developers.openai.com/api/docs/guides/secure-mcp-tunnels"
DEFAULT_STATE = Path.home() / "Library/Application Support/Dots2Codex Direct"
BEARER_NAME = "DOTS_BRIDGE_HTTP_BEARER"
KEY_NAME = "CONTROL_PLANE_API_KEY"


class SetupError(ValueError):
    pass


def say(message):
    print(message, flush=True)


def ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    answer = input(prompt + suffix + ": ").strip()
    return answer or default or ""


def confirm(message, word):
    say(message)
    if input(f"Type {word} to continue, or press Return to cancel: ").strip() != word:
        raise SetupError("Cancelled. No new configuration was committed.")


def check_state_path(state):
    state = Path(state).expanduser()
    if ".." in state.parts:
        raise SetupError("Use a state directory without parent-path (..) components.")
    state = state.absolute()
    if state == ROOT.parent or ROOT.parent in state.parents:
        raise SetupError("Choose a private state directory outside the source bundle.")
    # Do not follow user-controlled symlinks into unrelated state.
    if any(part.is_symlink() for part in (state, *state.parents) if part != Path("/")):
        raise SetupError("State directory must not contain symbolic links.")
    if state.exists():
        if not state.is_dir() or state.stat().st_uid != os.getuid():
            raise SetupError("State directory must be a directory owned by your user.")
        if state.stat().st_mode & 0o077:
            raise SetupError("State directory must be private (mode 700). Choose a new directory or fix its permissions yourself.")
    return state


def require_interactive():
    if not sys.stdin.isatty():
        raise SetupError("Setup/run needs an interactive Terminal for review. No piped approvals or unattended changes.")


@contextmanager
def setup_lock(state):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = state / ".setup.lock"
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError:
        raise SetupError("Another setup may be running. Do not remove .setup.lock until that setup has stopped.") from None
    try:
        yield
    finally:
        lock.rmdir()


def safe_env():
    """Build/probe subprocesses get no application, tunnel, or model credentials."""
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"}}
    env.update(PYTHONDONTWRITEBYTECODE="1", PIP_CONFIG_FILE=os.devnull)
    return env


def sdk_ready(python):
    try:
        result = subprocess.run([str(python), "-c", "import sys, importlib.metadata; "
            "import mcp, anyio, jsonschema; "
            "raise SystemExit(sys.version_info < (3,10) or "
            f"importlib.metadata.version('mcp') != '{SDK_VERSION}')"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=safe_env(), timeout=15)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def choose_python(state):
    private = state / ".venv/bin/python"
    if private.exists() and sdk_ready(private):
        return str(private)
    current = str(Path(sys.executable).absolute())
    if sdk_ready(current):
        return current
    target = state / ".venv"
    if target.exists():
        raise SetupError("The private .venv is incomplete or has a different SDK. It was preserved. Use a prepared Python via PYTHON_BIN, or review/remove that unused .venv manually and rerun setup.")
    confirm(f"Install Python MCP SDK {SDK_VERSION} and its dependencies from PyPI into\n"
            f"  {target}\nThis downloads packages and creates only this private environment.", "INSTALL")
    with setup_lock(state):
        try:
            subprocess.run([current, "-m", "venv", str(target)], check=True,
                           env=safe_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run([str(private), "-m", "pip", "--isolated", "install", "--disable-pip-version-check",
                            "--index-url", "https://pypi.org/simple", "-r", str(ROOT / "requirements-mcp.txt")],
                           check=True, env=safe_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not sdk_ready(private):
                raise SetupError("Installed environment did not pass the SDK check.")
        except (OSError, subprocess.CalledProcessError):
            raise SetupError("Dependency preparation failed. No route was created. The incomplete .venv was preserved for review; see help.") from None
    return str(private)


def route_config(state, hours, now):
    token = uuid.uuid4().hex
    return {"trust_mode": "single_owner_stdio", "db_path": str(state / "route/state.sqlite3"),
            "binding": {"grant_id": "direct-grant-" + token, "route_id": "direct-route-" + token,
                        "session_id": "direct-session-" + token, "thread_id": "direct-thread-" + token,
                        "model": "gpt-6-astra", "reasoning_effort": "xhigh"},
            "client_actor": "direct-mac-" + token, "worker_actor": "direct-native-" + token,
            "context_epoch": "direct-epoch-" + token,
            "approval_ref": "local-interactive-setup-" + token,
            "not_before": now, "expires_at": now + hours * 3600,
            "max_wait_ms": 5000, "http_wait_ms": 30000}


def commit_setup(state, config, settings):
    """Never replaces an old route or its durable state, including on retry."""
    with setup_lock(state):
        destination = state / "route"
        if destination.exists() or destination.is_symlink():
            raise SetupError("Existing route preserved. Setup cannot reset or renew durable state.")
        staging = Path(tempfile.mkdtemp(prefix=".prepare-", dir=state))
        try:
            for name, value in (("config.json", config), ("launcher.json", settings)):
                path = staging / name
                with path.open("x", encoding="utf-8") as stream:
                    os.chmod(path, 0o600)
                    json.dump(value, stream, indent=2, allow_nan=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            os.rename(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def read_setup(state):
    try:
        paths = [state / "route/config.json", state / "route/launcher.json"]
        route = state / "route"
        if route.is_symlink() or any(path.is_symlink() for path in paths):
            raise SetupError("Symlinked configuration is not accepted.")
        if route.stat().st_mode & 0o077 or route.stat().st_uid != os.getuid():
            raise SetupError("Route directory must be private and owned by your user.")
        if any(path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid() for path in paths):
            raise SetupError("Configuration files must be private (mode 600).")
        if any(path.stat().st_size > 65536 for path in paths):
            raise SetupError("Configuration is too large.")
        config, settings = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
        if set(settings) != {"python", "http_port", "created_at"}:
            raise ValueError
        if not isinstance(settings["python"], str) or not Path(settings["python"]).is_absolute():
            raise ValueError
        if type(settings["http_port"]) is not int or not 1024 <= settings["http_port"] <= 65535:
            raise ValueError
        if config["db_path"] != str(state / "route/state.sqlite3"):
            raise ValueError
        # Validate with a fixed non-credential fixture; no real bearer is required for status.
        sys.path.insert(0, str(ROOT))
        from mcp_adapter.config import load_config
        from transport import Binding
        loaded = load_config(paths[0], environ={BEARER_NAME: "configuration-validation-only"})
        Binding(**loaded["binding"]).validate()
        for key in ("not_before", "expires_at"):
            datetime.fromtimestamp(loaded[key], timezone.utc)
        if loaded["binding"]["model"] != "gpt-6-astra" or loaded["binding"]["reasoning_effort"] != "xhigh":
            raise ValueError
        return config, settings
    except FileNotFoundError:
        raise SetupError("No complete local setup found. Choose setup first.") from None
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        raise SetupError("Local setup is invalid or unreadable. Files were preserved; do not reset state to replay an uncertain action.") from None


def mcp_command(state, settings):
    return shlex.join(["/usr/bin/env", "PYTHON_BIN=" + settings["python"], "PYTHONDONTWRITEBYTECODE=1",
                       str(ROOT / "scripts/start_mcp.sh"), "--config", str(state / "route/config.json"),
                       "--http-port", str(settings["http_port"])])


def show_connection(state, config, settings):
    say("\nExpected dedicated tunnel stdio command (contains no credentials):\n" + mcp_command(state, settings))
    say(f"Local Responses base URL: http://127.0.0.1:{settings['http_port']}/v1")
    say("Model: gpt-6-astra; reasoning effort: xhigh. These are logical bindings, not native model attestation.")
    say("Set up a dedicated profile using the official guide, then choose run. Keep any Lean/other profile unchanged.")
    say("Tunnel profile/key creation is a separate permission step; this launcher never creates or edits either.")
    say(GUIDE)
    profile = "dots-direct-" + config["binding"]["route_id"].rsplit("-", 1)[-1]
    template = shlex.join(["tunnel-client", "init", "--sample", "sample_mcp_stdio_local",
                          "--profile", profile, "--tunnel-id", "REPLACE_WITH_APPROVED_EXISTING_TUNNEL_ID",
                          "--mcp-command", mcp_command(state, settings)])
    say("After separately reviewing/approving creation of this new dedicated profile, use this template.")
    say("Replace only the tunnel-ID placeholder; do not reuse another project's profile name:")
    say(template)


def setup(state):
    require_interactive()
    if (state / "route").exists():
        config, settings = read_setup(state)
        say("Existing setup preserved. Re-running setup does not reset IDs, extend authorization, or erase requests.")
        show_connection(state, config, settings)
        return
    python = choose_python(state)
    try:
        hours = int(ask("Authorize this new, single-owner trial for how many hours (1–24)", "4"))
        port = int(ask("Loopback HTTP port (1024–65535)", "18765"))
    except ValueError:
        raise SetupError("Use whole numbers for duration and port; no route was created.") from None
    if not 1 <= hours <= 24 or not 1024 <= port <= 65535:
        raise SetupError("Duration or port is out of range; no route was created.")
    confirm(f"Create a NEW private, single-owner route under\n  {state / 'route'}\n"
            f"Use gpt-6-astra / xhigh for {hours} hours with a fresh state database.\n"
            "Only your intended native controller may access its dedicated tunnel.\n"
            "This stores logical IDs and a time-limited approval record, no credentials.\n"
            "Do not use a new route to retry an unresolved action from another route.", "SETUP")
    now = time.time()
    config = route_config(state, hours, now)
    settings = {"python": python, "http_port": port, "created_at": now}
    commit_setup(state, config, settings)
    say("Local route prepared. The state database will be created on first bridge startup.")
    show_connection(state, config, settings)


def tunnel_binary():
    candidate = os.environ.get("TUNNEL_CLIENT_BIN") or shutil.which("tunnel-client")
    if not candidate:
        for path in ("/opt/homebrew/bin/tunnel-client", "/usr/local/bin/tunnel-client"):
            if Path(path).is_file() and os.access(path, os.X_OK):
                candidate = path
                break
    if not candidate or not Path(candidate).is_file() or not os.access(candidate, os.X_OK):
        raise SetupError("tunnel-client is missing. Install it yourself from the official guide; this launcher never downloads binaries. " + GUIDE)
    return str(Path(candidate).absolute())


def valid_bearer(value):
    return isinstance(value, str) and len(value) >= 16 and value.isascii() and all(33 <= ord(c) <= 126 for c in value)


def port_available(port):
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def run(state):
    require_interactive()
    config, settings = read_setup(state)
    now = time.time()
    if not config["not_before"] <= now < config["expires_at"]:
        raise SetupError("Route authorization is not active. Do not change the timestamps or erase its database. Reconcile any pending action before starting a separately authorized trial.")
    if not sdk_ready(settings["python"]):
        raise SetupError("The selected Python environment needs the pinned MCP SDK. Run check; nothing was started.")
    binary = tunnel_binary()
    if not valid_bearer(os.environ.get(BEARER_NAME)):
        raise SetupError("An already authorized local bearer must be supplied securely as DOTS_BRIDGE_HTTP_BEARER in this Terminal environment. Never paste it into chat, command arguments, or shell history. No credential was generated or stored.")
    if not os.environ.get(KEY_NAME):
        raise SetupError("The existing authorized tunnel runtime key must be supplied securely as CONTROL_PLANE_API_KEY in this Terminal environment. Create/configure a key only after separately approving that access; never paste it into chat or command arguments.")
    if not port_available(settings["http_port"]):
        raise SetupError("The configured loopback port is occupied. Check the running process; do not start a second bridge or kill another project's process.")
    show_connection(state, config, settings)
    profile = ask("Existing dedicated tunnel profile name (no credentials)")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", profile):
        raise SetupError("Use a nonempty profile name containing only letters, digits, dots, dashes, and underscores.")
    confirm(f"Run existing profile {profile!r} with {binary}?\n"
            "Confirm you reviewed that profile: its tunnel is approved for this workspace,\n"
            "its stdio command exactly matches the command above, and its admin listener is loopback-only.\n"
            "The selected tunnel receives this dedicated bridge's MCP traffic.\n"
            "The tunnel owns the bridge's stdin. It stays in the foreground; Ctrl-C stops it.", "RUN")
    say("Starting the selected tunnel in the foreground. Verify its readiness in the official client before making requests.")
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    os.execve(binary, [binary, "run", "--profile", profile], env)


CODEX_VERSION = "0.159.2"


def existing_codex():
    binary = os.environ.get("CODEX_BIN") or shutil.which("codex")
    if not binary or not Path(binary).is_file() or not os.access(binary, os.X_OK):
        raise SetupError("An existing Codex CLI is required. Install/update it yourself from the official source, then run check.")
    return str(Path(binary).absolute())


def codex_version(binary):
    try:
        return subprocess.run([binary, "--version"], check=True, capture_output=True, text=True,
                              env=safe_env(), timeout=10).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise SetupError("Could not verify the existing Codex CLI version.") from None


def codex_argv(binary, settings, workdir, route_id):
    # Do not inherit auth/headers from an unrelated global provider of a fixed name.
    provider = "dots_direct_" + hashlib.sha256(route_id.encode("utf-8")).hexdigest()[:24]
    prefix = "model_providers." + provider
    overrides = {
        "model_provider": provider,
        prefix + ".name": "Dots2Codex Direct",
        prefix + ".base_url": f"http://127.0.0.1:{settings['http_port']}/v1",
        prefix + ".env_key": BEARER_NAME,
        prefix + ".wire_api": "responses",
        prefix + ".requires_openai_auth": False,
        prefix + ".supports_websockets": False,
        prefix + ".request_max_retries": 0,
        prefix + ".stream_max_retries": 0,
        "web_search": "disabled", "model_reasoning_effort": "xhigh",
    }
    args = [binary, "--no-daemon", "--sandbox", "read-only"]
    for key, value in overrides.items():
        args.extend(["-c", key + "=" + json.dumps(value)])
    return args + ["-m", "gpt-6-astra", "-C", str(workdir)]


def codex(state):
    require_interactive()
    config, settings = read_setup(state)
    if not config["not_before"] <= time.time() < config["expires_at"]:
        raise SetupError("Route authorization is not active. Reconcile old requests before a separately authorized new trial.")
    marker = state / "route/codex-started.json"
    if marker.exists() or marker.is_symlink():
        raise SetupError("A Codex session was already started for this route. Return to that Terminal; do not resume/fork or start a replacement session after an uncertain outcome.")
    if not valid_bearer(os.environ.get(BEARER_NAME)):
        raise SetupError("Supply the same already authorized DOTS_BRIDGE_HTTP_BEARER securely in this second Terminal environment. Never put it in arguments, chat, or shell history.")
    binary = existing_codex()
    version = codex_version(binary)
    if version != "codex-cli " + CODEX_VERSION:
        raise SetupError("This bridge's CLI compatibility check requires codex-cli " + CODEX_VERSION +
                         ". Other versions must be checked against the wire contract before use.")
    if port_available(settings["http_port"]):
        raise SetupError("No loopback listener detected. Start the reviewed tunnel with run in another Terminal first.")
    workdir = Path(ask("Existing project directory for the read-only Codex trial")).expanduser()
    if not workdir.is_absolute() or not workdir.is_dir():
        raise SetupError("Choose an existing absolute project directory. Nothing was started.")
    args = codex_argv(binary, settings, workdir, config["binding"]["route_id"])
    confirm(f"Start one NEW Codex {CODEX_VERSION} session for this route in\n  {workdir}\n"
            "Use gpt-6-astra / xhigh and the loopback provider with read-only sandbox.\n"
            "Confirm your dedicated tunnel is healthy and the intended native child can call its tools.\n"
            "A listening port alone does not establish that readiness.\n"
            "Provider overrides apply only to this process; no global Codex file is edited.\n"
            "This route cannot launch another Codex session after this one starts.", "CODEX")
    env = dict(os.environ)
    env.pop(KEY_NAME, None)
    env.pop("OPENAI_API_KEY", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # An exclusive durable admission marker prevents a second session or a
    # restart from silently replaying an ambiguous native/Mac action.
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SetupError("Another Codex launch already reserved this route. No second session was started.") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"version": CODEX_VERSION, "workdir": str(workdir), "started_at": time.time()}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    say("Starting one session. Keep the tunnel Terminal open; use the same Codex conversation for the trial.")
    try:
        os.execve(binary, args, env)
    except OSError:
        # exec failed before a child existed, so this reservation is safe to undo.
        marker.unlink()
        raise SetupError("Codex could not start. Its unexecuted admission marker was removed; check the executable.") from None



def private_json_record(path):
    """Read a small owned admission record without following links."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 65536:
            raise ValueError
        raw = path.read_bytes()
        value = json.loads(raw)
        if type(value) is not dict:
            raise ValueError
        return value, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, UnicodeError):
        raise SetupError("Admission record is missing, unsafe, or unreadable. All existing state was preserved.") from None


def write_admission_record(path, value):
    """One-use, durable, append-only admission; never replace a prior attempt."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SetupError("An unused-reopen attempt already reserved this route. Its history was preserved; no replacement was started.") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def require_clients_stopped(config, settings, *, include_bridge):
    """Supplementary current-route scan, not proof of a legacy client's exit.

    Legacy markers have no PID. The owner's explicit exit confirmation remains
    required. Only this user's exact route provider/endpoint/config is relevant;
    unrelated Codex CLI projects, app servers and helpers may keep running.
    Process arguments are inspected locally and never printed or persisted.
    """
    provider = "dots_direct_" + hashlib.sha256(config["binding"]["route_id"].encode("utf-8")).hexdigest()[:24]
    endpoint = f"http://127.0.0.1:{settings['http_port']}/v1"
    bridge_config = str(Path(config["db_path"]).parent / "config.json")
    # Match complete identities rather than broad executable names or the word
    # 'codex' in an unrelated argument. Provider field suffixes and endpoint
    # subpaths still identify this route; lookalike IDs and ports do not.
    provider_pattern = re.compile(r"(?<![\w-])" + re.escape(provider) + r"(?![\w-])")
    endpoint_pattern = re.compile(r"(?:^|[\s'\"=])" + re.escape(endpoint) + r"(?=$|[\s'\"/])")
    config_pattern = re.compile(r"(?:^|[\s'\"=])" + re.escape(bridge_config) + r"(?=$|[\s'\"])")
    try:
        result = subprocess.run(["/bin/ps", "-ww", "-axo", "uid=,pid=,command="],
            check=True, capture_output=True, text=True, env=safe_env(), timeout=10)
        lines = result.stdout.splitlines()
        if not lines:
            raise ValueError
        for line in lines:
            uid, pid, command = line.strip().split(None, 2)
            if not uid.isdecimal() or not pid.isdecimal():
                raise ValueError
            if int(uid) != os.getuid() or int(pid) == os.getpid():
                continue
            client = provider_pattern.search(command) or endpoint_pattern.search(command)
            bridge = config_pattern.search(command)
            # A same-route identity always wins, even on a process called an
            # app-server/daemon. The restarted bridge is expected after READY.
            if client or (include_bridge and bridge):
                raise SetupError("A process still references this Direct route's provider, endpoint, or bridge configuration. Exit only that trial process normally and retry; no process was killed.")
    except SetupError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise SetupError("Could not inspect local processes safely. No unused-reopen admission was made.") from None


@contextmanager
def paused_ingress_guard(port):
    """Reserve the stopped listener's address throughout the first zero audit."""
    with socket.socket() as guard:
        try:
            # Do not use SO_REUSEADDR/PORT: a competing listener must fail closed.
            guard.bind(("127.0.0.1", port))
        except OSError:
            raise SetupError("Stop the existing foreground tunnel/bridge first and wait for its process to exit. The loopback port must be free.") from None
        yield


def unused_database_snapshot(config):
    """Read live SQLite/WAL state; never create, reset, repair, or modify it."""
    database = Path(config["db_path"])
    try:
        info = database.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(database) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                side = sidecar.lstat()
                if not stat.S_ISREG(side.st_mode) or side.st_uid != os.getuid():
                    raise ValueError
        # mode=ro intentionally does NOT use immutable=1, which could ignore WAL.
        with sqlite3.connect("file:" + quote(str(database), safe="/") + "?mode=ro", uri=True, timeout=2) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            if db.execute("PRAGMA integrity_check").fetchall()[0][0] != "ok":
                raise ValueError
            objects = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'").fetchall()
            if {(row["type"], row["name"]) for row in objects} != {
                    ("table", "routes"), ("table", "schemas"), ("table", "requests")}:
                raise ValueError
            expected_columns = {
                "routes": "route_id:TEXT authorization:BLOB binding:BLOB client_actor:TEXT worker_actor:TEXT not_before:REAL expires_at:REAL last_clock:REAL revoked:INTEGER",
                "schemas": "route_id:TEXT name:TEXT digest:TEXT body:BLOB",
                "requests": "request_id:TEXT route_id:TEXT seq:INTEGER fingerprint:TEXT payload:BLOB schema_refs:BLOB state:TEXT cancelled:INTEGER execution_reserved:INTEGER result_id:TEXT result_hash:TEXT result:BLOB",
            }
            for table, columns in expected_columns.items():
                actual = [(column["name"], column["type"].upper()) for column in db.execute("PRAGMA table_info(" + table + ")")]
                if actual != [tuple(column.split(":")) for column in columns.split()]:
                    raise ValueError
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError
            # Every state counts, including settled/cancelled requests, schemas,
            # execution reservations and committed results. Never filter by state.
            if db.execute("SELECT count(*) FROM requests").fetchone()[0] or db.execute("SELECT count(*) FROM schemas").fetchone()[0]:
                raise SetupError("This route has durable request or schema activity. Unused-only reopening is refused; reconcile the existing session instead.")
            routes = db.execute("SELECT * FROM routes").fetchall()
            if len(routes) != 1:
                raise ValueError
            row = dict(routes[0])
            authorization = {key: config[key] for key in ("binding", "client_actor", "worker_actor", "not_before", "expires_at", "approval_ref")}
            canonical = lambda value: json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            expected = {"route_id": config["binding"]["route_id"],
                        "authorization": canonical(authorization), "binding": canonical(config["binding"]),
                        **{key: config[key] for key in ("client_actor", "worker_actor", "not_before", "expires_at")},
                        "revoked": 0}
            if set(row) != set(expected) | {"last_clock"} or any(row[key] != value for key, value in expected.items()):
                raise ValueError
            now = time.time()
            if not config["not_before"] <= now < config["expires_at"] or not isinstance(row["last_clock"], (float, int)) or not config["not_before"] <= row["last_clock"] <= now + 1:
                raise ValueError
            return {"device": info.st_dev, "inode": info.st_ino,
                    "authorization_sha256": hashlib.sha256(canonical(authorization)).hexdigest(),
                    "requests": 0, "schemas": 0}
    except SetupError:
        raise
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, OverflowError, IndexError):
        raise SetupError("Unused route could not be verified from the actual database and authorization. Missing, changed, expired, dirty, or unreadable state was preserved.") from None


def codex_reopen_unused(state):
    """One narrowly bounded legacy recovery, with a fully stopped ingress phase."""
    require_interactive()
    config, settings = read_setup(state)
    route = state / "route"
    original = route / "codex-started.json"
    prepared = route / "codex-unused-reopen-prepared.json"
    consumed = route / "codex-unused-reopen-consumed.json"
    if any(path.exists() or path.is_symlink() for path in (prepared, consumed)):
        raise SetupError("An unused-reopen attempt already exists. Its records were preserved; no repeat admission is allowed.")
    marker, marker_hash = private_json_record(original)
    if set(marker) != {"version", "workdir", "started_at"} or marker["version"] != CODEX_VERSION or not isinstance(marker["started_at"], (int, float)) or not config["not_before"] <= marker["started_at"] < config["expires_at"]:
        raise SetupError("The original Codex admission cannot be verified. No recovery was started.")
    workdir = Path(marker["workdir"]) if isinstance(marker["workdir"], str) else Path(".")
    if not workdir.is_absolute() or not workdir.is_dir():
        raise SetupError("The original project directory is unavailable. No recovery was started.")
    if not valid_bearer(os.environ.get(BEARER_NAME)):
        raise SetupError("Supply the same already authorized DOTS_BRIDGE_HTTP_BEARER securely in this Terminal environment.")
    binary = existing_codex()
    if codex_version(binary) != "codex-cli " + CODEX_VERSION:
        raise SetupError("Unused reopening requires codex-cli " + CODEX_VERSION + ".")
    confirm("Unused-only recovery requires ALL of these facts:\n"
            "- The old Codex CLI has exited completely, before any request or action.\n"
            "- The intended native controller is paused and has never received a request.\n"
            "- The original foreground tunnel AND its bridge child have exited completely.\n"
            "Keep that Terminal open to retain its environment; do not create a new profile.\n"
            "The old marker has no PID: closure is your explicit assertion, not a proven process identity.\n"
            "This checks all durable activity, keeps the original marker, and permits only one attempt.\n"
            "Cancellation or an uncertain launch after reservation preserves and consumes this recovery attempt.", "REOPEN")
    initial_files = {name: hashlib.sha256((route / name).read_bytes()).hexdigest()
                     for name in ("config.json", "launcher.json")}
    with paused_ingress_guard(settings["http_port"]):
        require_clients_stopped(config, settings, include_bridge=True)
        snapshot = unused_database_snapshot(config)
        record = {"version": 1, "prepared_at": time.time(), "launcher_pid": os.getpid(),
                  "original_marker_sha256": marker_hash, "configuration_sha256": initial_files,
                  "database": snapshot, "owner_asserted_old_client_and_bridge_exited": True,
                  "owner_asserted_native_paused": True}
        write_admission_record(prepared, record)
    say("Zero durable activity verified while the loopback port was exclusively reserved. Original state is unchanged.\n"
        "Now restart the SAME approved tunnel profile in its original Terminal using the usual run command.\n"
        "Keep the native controller paused. Do not start another Codex client or send a test HTTP request.")
    confirm("Wait until that same tunnel and bridge are healthy, then continue here.\n"
            "A listening port alone is not proof of tunnel identity or readiness.\n"
            "No setup/init, credential change, new route, or authorization extension is needed.", "READY")
    current_config, current_settings = read_setup(state)
    current_files = {name: hashlib.sha256((route / name).read_bytes()).hexdigest() for name in initial_files}
    if current_config != config or current_settings != settings or current_files != initial_files or private_json_record(original)[1] != marker_hash or private_json_record(prepared)[0] != record:
        raise SetupError("Configuration or admission history changed. Recovery is refused; all records were preserved.")
    require_clients_stopped(config, settings, include_bridge=False)
    if port_available(settings["http_port"]):
        raise SetupError("The restarted loopback listener is missing. Recovery was not launched; the reserved attempt was preserved.")
    if unused_database_snapshot(config) != snapshot:
        raise SetupError("Durable route identity changed. Recovery was refused and all records were preserved.")
    if not config["not_before"] <= time.time() < config["expires_at"]:
        raise SetupError("Route authorization expired before recovery consumption. The attempt was preserved.")
    write_admission_record(consumed, {"version": 1, "consumed_at": time.time(),
        "launcher_pid": os.getpid(), "prepared_sha256": private_json_record(prepared)[1]})
    env = dict(os.environ)
    env.pop(KEY_NAME, None)
    env.pop("OPENAI_API_KEY", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if not config["not_before"] <= time.time() < config["expires_at"]:
        raise SetupError("Route authorization expired before Codex launch. Both recovery records were preserved.")
    say("Starting the one unused-only replacement in the original project directory. Keep this same conversation for the trial.")
    try:
        os.execve(binary, codex_argv(binary, settings, workdir, config["binding"]["route_id"]), env)
    except OSError:
        raise SetupError("Codex could not start. Both recovery records remain preserved; do not delete markers or retry by resetting state.") from None


def check(state):
    say(f"Python: {sys.version.split()[0]}; required >= 3.10")
    say(f"MCP SDK {SDK_VERSION}: " + ("ready in current Python" if sdk_ready(sys.executable) else "not ready in current Python"))
    private = state / ".venv/bin/python"
    if private.exists():
        say("Private Python environment: " + ("ready" if sdk_ready(private) else "incomplete or incompatible"))
    try:
        say("Tunnel client: " + tunnel_binary())
    except SetupError:
        say("Tunnel client: missing; install only from " + GUIDE)
    try:
        version = codex_version(existing_codex())
        say("Codex CLI: " + ("pinned version " + CODEX_VERSION + " available"
            if version == "codex-cli " + CODEX_VERSION else "different/unverified version; this trial requires " + CODEX_VERSION))
    except SetupError:
        say("Codex CLI: missing or version could not be verified; this trial requires " + CODEX_VERSION)
    say(f"Local state: {state}")
    say("Credentials: values are never displayed. check does not contact the network or start a server.")
    status(state)


def status(state):
    if not (state / "route").exists():
        say("Route: not set up")
        return
    config, settings = read_setup(state)
    now = time.time()
    active = config["not_before"] <= now < config["expires_at"]
    say("Route authorization: " + ("active" if active else "not active"))
    say("Expires (UTC): " + datetime.fromtimestamp(config["expires_at"], timezone.utc).isoformat())
    say(f"Responses: http://127.0.0.1:{settings['http_port']}/v1; gpt-6-astra / xhigh")
    say("Loopback port: " + ("free (no listener detected)" if port_available(settings["http_port"]) else "occupied (identity/readiness not established)"))
    database = Path(config["db_path"])
    if database.exists():
        try:
            if database.is_symlink():
                raise ValueError
            with sqlite3.connect("file:" + quote(str(database), safe="/") + "?mode=ro", uri=True) as connection:
                pending = connection.execute("SELECT count(*) FROM requests WHERE state IN ('queued','claimed')").fetchone()[0]
            say(f"Durable queued/claimed requests: {pending}. Never erase this database to retry an uncertain action.")
        except (sqlite3.Error, OSError, ValueError):
            say("State database exists but could not be inspected safely; preserved unchanged.")
    else:
        say("State database: not created yet")
    if (state / "route/codex-started.json").exists():
        say("Codex session: previously admitted (this does not prove it is still running)")
    say("This is local configuration status, not proof of tunnel health, native tool visibility, or model admission.")


def help_text(state):
    say("DIRECT: guided Mac setup for the experimental single-owner bridge\n"
        "  check   Read-only prerequisite and local-state checks\n"
        "  setup   Prepare a private Python environment if needed, then a new route\n"
        "  run     Review an existing dedicated tunnel profile and run it in foreground\n"
        "  codex   Start one pinned Codex CLI session in a second Terminal\n"
        "  codex-reopen-unused  One guarded recovery after a completely unused CLI exit\n"
        "  status  Read local route/port/database status without starting the bridge\n"
        "  help    Show this help and the exact tunnel command after setup\n\n"
        "Double-click DIRECT.command for the menu, or run ./DIRECT.command COMMAND.\n"
        "Optional: --state-dir ABSOLUTE_PATH, PYTHON_BIN (existing Python),\n"
        "TUNNEL_CLIENT_BIN and CODEX_BIN (existing official binaries). Keep state outside the bundle.\n"
        "Use an existing secure environment for DOTS_BRIDGE_HTTP_BEARER and\n"
        "CONTROL_PLANE_API_KEY. Finder may not inherit Terminal credentials.\n"
        "Do not put credentials in arguments, JSON, chat, shell history, or logs.\n"
        "The launcher does not generate/store credentials, initialize tunnel profiles,\n"
        "change global Codex settings, launch a background service, or call inference APIs.\n\n"
        "Cancelled setup leaves no route. An approved private dependency install may remain.\n"
        "A failed dependency install can leave .venv for inspection; use a prepared Python\n"
        "via PYTHON_BIN or remove only that unused .venv yourself before retrying setup.\n"
        "A leftover .setup.lock after force-quit requires confirming setup has stopped\n"
        "before removing that empty lock directory. Never delete route/state.sqlite3.\n"
        "The package is not a signed Mac app; review the source and your Mac's warning.\n"
        "Do not disable Gatekeeper or remove quarantine to bypass a security warning.\n\n"
        "Official tunnel setup: " + GUIDE)
    if (state / "route").exists():
        show_connection(state, *read_setup(state))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("check", "setup", "run", "codex", "codex-reopen-unused", "status", "help"))
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args(argv)
    try:
        state = check_state_path(args.state_dir)
        actions = {"check": check, "setup": setup, "run": run, "codex": codex, "codex-reopen-unused": codex_reopen_unused, "status": status, "help": help_text}
        if args.command:
            actions[args.command](state)
            return 0
        require_interactive()
        while True:
            say("\nDIRECT  1 Check  2 Setup  3 Run tunnel  4 Start Codex  5 Status  6 Help  0 Exit")
            choice = ask("Choose", "0")
            if choice in ("0", "exit", "quit"):
                return 0
            command = {"1": "check", "2": "setup", "3": "run", "4": "codex", "5": "status", "6": "help"}.get(choice, choice)
            if command not in actions:
                say("Choose 0–6 or a command name.")
                continue
            try:
                actions[command](state)
            except SetupError as exc:
                say(str(exc))
    except (EOFError, KeyboardInterrupt):
        say("\nCancelled. Existing route and durable state were preserved.")
        return 130
    except SetupError as exc:
        say(str(exc))
        return 2
    except OSError:
        say("A local file or executable could not be accessed. Existing state was preserved; run check and inspect permissions.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
