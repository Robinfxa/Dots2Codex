#!/usr/bin/env python3
"""Experimental, local-only Codex Responses bridge. No model implementation."""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone

VERSION = 1
MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 128 * 1024
MAX_TURNS = 32
ID_RE = re.compile(r"^[a-f0-9]{32}$")
DEFAULT_CODEX = Path(__file__).resolve().parent.parent / "codex-cli/bin/codex"


class BridgeError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def decode(data):
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError("duplicate JSON key")
            obj[key] = value
        return obj
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))


def read_json(path, limit=MAX_REQUEST + 16384):
    with Path(path).open("rb") as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise BridgeError("payload_too_large", f"File exceeds {limit} bytes", 413)
    try:
        return decode(data)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise BridgeError("invalid_json", "Invalid JSON document") from exc


def atomic_json(path, value, exclusive=False):
    """Fsync complete bytes then atomically publish. Never expose partial JSON."""
    path = Path(path)
    data = encode(value)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        dfd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def checked_id(value):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise BridgeError("invalid_id", "Expected a UUID hex request ID")
    return value


def request_tools(request):
    result = {}
    sources = list(request.get("tools") or [])
    for item in request.get("input", []):
        if item.get("type") == "additional_tools":
            sources.extend(item.get("tools") or [])
    def visit(tool, namespace=None):
        if not isinstance(tool, dict):
            raise BridgeError("invalid_tool", "Tool declaration must be an object")
        kind, name = tool.get("type"), tool.get("name")
        if kind == "namespace":
            if namespace is not None:
                raise BridgeError("unsupported_tool", "Nested tool namespaces are unsupported")
            if not isinstance(name, str) or not isinstance(tool.get("tools"), list):
                raise BridgeError("invalid_tool", "Invalid namespace declaration")
            for child in tool["tools"]:
                visit(child, name)
        elif kind in ("function", "custom"):
            if not isinstance(name, str) or not name:
                raise BridgeError("invalid_tool", "Tool requires a name")
            key = (namespace, name)
            if key in result:
                raise BridgeError("duplicate_tool", "Duplicate advertised tool identity")
            result[key] = tool
        # Other advertised tools remain visible to the broker but cannot be returned.
    for tool in sources:
        visit(tool)
    return result


def validate_request(request):
    if not isinstance(request, dict):
        raise BridgeError("invalid_request", "Request must be a JSON object")
    if request.get("stream") is not True:
        raise BridgeError("unsupported_mode", "This prototype requires stream:true")
    if not isinstance(request.get("model"), str) or not request["model"]:
        raise BridgeError("invalid_model", "model must be a nonempty string")
    if request["model"] != "native-subagent-bridge":
        raise BridgeError("unsupported_model", "Only native-subagent-bridge is supported; no model fallback")
    if not isinstance(request.get("input"), list):
        raise BridgeError("invalid_input", "input must be an array")
    if request.get("previous_response_id"):
        raise BridgeError("unsupported_state", "previous_response_id is unsupported; send full history")
    if request.get("tools") is not None and not isinstance(request["tools"], list):
        raise BridgeError("invalid_tools", "tools must be an array")
    if request.get("instructions") is not None and not isinstance(request["instructions"], str):
        raise BridgeError("invalid_instructions", "instructions must be a string")
    supported = {"message", "function_call", "function_call_output", "custom_tool_call",
                 "custom_tool_call_output", "additional_tools"}
    unknown = []
    for i, item in enumerate(request["input"]):
        if not isinstance(item, dict):
            raise BridgeError("invalid_input", f"input[{i}] must be an object")
        kind = item.get("type", "message" if "role" in item else None)
        if kind not in supported:
            unknown.append({"index": i, "type": kind})
        if kind == "message":
            content = item.get("content")
            if not isinstance(content, list):
                raise BridgeError("unsupported_content", "Message content must be a text content array")
            for part in content:
                if not isinstance(part, dict) or part.get("type") not in ("input_text", "output_text") or not isinstance(part.get("text"), str):
                    raise BridgeError("unsupported_content", "Only text message content is supported")
    request_tools(request)
    # Unknown items are preserved verbatim, recorded, and must be rejected by broker.
    return unknown


def validate_result(result, request, request_id):
    if not isinstance(result, dict):
        raise BridgeError("invalid_result", "result must be an object", 502)
    kind = result.get("kind")
    if kind == "message":
        if set(result) != {"kind", "text"} or not isinstance(result.get("text"), str) or not result["text"]:
            raise BridgeError("invalid_result", "message needs only kind and nonempty text", 502)
        return {"id": "msg_" + request_id, "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": result["text"]}]}
    if kind not in ("function_call", "custom_tool_call"):
        raise BridgeError("unsupported_result", "Unsupported result kind", 502)
    allowed = {"kind", "name", "namespace", "arguments" if kind == "function_call" else "input"}
    required = allowed - {"namespace"}
    if not required.issubset(result) or set(result) - allowed:
        raise BridgeError("invalid_result", "Unexpected or missing tool-result fields", 502)
    name, namespace = result.get("name"), result.get("namespace")
    if not isinstance(name, str) or (namespace is not None and not isinstance(namespace, str)):
        raise BridgeError("invalid_result", "Tool identity must be strings", 502)
    tool = request_tools(request).get((namespace, name))
    wanted = "function" if kind == "function_call" else "custom"
    if not tool or tool.get("type") != wanted:
        raise BridgeError("unadvertised_tool", "Tool name/type/namespace was not advertised by the CLI", 502)
    item = {"id": "fc_" + request_id, "type": kind, "name": name, "call_id": "call_" + request_id}
    if namespace is not None:
        item["namespace"] = namespace
    if kind == "function_call":
        if not isinstance(result["arguments"], dict):
            raise BridgeError("invalid_arguments", "arguments must be a JSON object, not a JSON string", 502)
        item["arguments"] = encode(result["arguments"]).decode()
    else:
        if not isinstance(result["input"], str):
            raise BridgeError("invalid_arguments", "custom input must be a string", 502)
        item["input"] = result["input"]
    return item


class Queue:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def initialize(self):
        if self.root.exists() and any(self.root.iterdir()):
            raise BridgeError("nonempty_run", "Use a new empty runtime directory for each server")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        for name in ("requests", "responses", "claims", "outcomes"):
            (self.root / name).mkdir(mode=0o700, exist_ok=True)
        if any((self.root / "requests").iterdir()):
            raise BridgeError("nonempty_run", "Use a fresh runtime root for each server")
        try:
            atomic_json(self.root / "server.lock", {"pid": os.getpid(), "created_at": now()}, exclusive=True)
        except FileExistsError as exc:
            raise BridgeError("run_locked", "Runtime root already has a server lock") from exc

    def path(self, section, rid):
        return self.root / section / (checked_id(rid) + ".json")

    def terminal(self, rid):
        return self.path("outcomes", rid).exists()

    def outcome(self, rid, state, **fields):
        try:
            atomic_json(self.path("outcomes", rid), {"version": VERSION, "request_id": rid,
                        "state": state, "at": now(), **fields}, exclusive=True)
        except FileExistsError:
            pass

    def event(self, event, **fields):
        # Deliberately excludes HTTP headers, prompt content and tool output.
        line = encode({"at": now(), "event": event, **fields}) + b"\n"
        fd = os.open(self.root / "events.jsonl", os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

    def claim(self, wait_seconds=0):
        deadline = time.monotonic() + wait_seconds
        while True:
            for path in sorted((self.root / "requests").glob("*.json"), key=lambda p: p.stat().st_mtime):
                rid = checked_id(path.stem)
                if self.terminal(rid) or self.path("responses", rid).exists():
                    continue
                req = read_json(path)
                if req["deadline_unix"] <= time.time():
                    continue
                try:
                    atomic_json(self.path("claims", rid), {"version": VERSION, "request_id": rid,
                                "claimed_at": now(), "claim_id": uuid.uuid4().hex}, exclusive=True)
                except FileExistsError:
                    continue
                return req
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.1)

    def submit(self, rid, result=None, error=None):
        checked_id(rid)
        req = read_json(self.path("requests", rid))
        if self.terminal(rid) or req["deadline_unix"] <= time.time():
            raise BridgeError("request_closed", "Request timed out, was cancelled, or already completed", 409)
        if not self.path("claims", rid).exists():
            raise BridgeError("not_claimed", "Claim request before submission", 409)
        if (result is None) == (error is None):
            raise BridgeError("invalid_response", "Exactly one of result and error is required")
        envelope = {"version": VERSION, "request_id": rid}
        if error is not None:
            if not isinstance(error, dict) or set(error) != {"code", "message"} or not all(isinstance(error[x], str) and error[x] for x in error):
                raise BridgeError("invalid_error", "error requires only nonempty code and message")
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error["code"]):
                raise BridgeError("invalid_error", "Error code must be a short snake_case machine code")
            envelope["error"] = error
        else:
            if req.get("unknown_input_items"):
                raise BridgeError("unsupported_input", "Unknown input items require an explicit error response")
            validate_result(result, req["request"], rid)
            envelope["result"] = result
        if len(encode(envelope)) > MAX_RESPONSE:
            raise BridgeError("payload_too_large", "Response exceeds maximum size", 413)
        try:
            atomic_json(self.path("responses", rid), envelope, exclusive=True)
        except FileExistsError as exc:
            raise BridgeError("duplicate_response", "A response is already committed", 409) from exc
        self.event("response_submitted", request_id=rid, kind=result.get("kind") if result else "error")


class BridgeServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, queue, port=0, timeout=180, max_turns=MAX_TURNS):
        self.queue, self.response_timeout, self.max_turns = queue, timeout, max_turns
        self.inflight = threading.Lock()
        self.session_id = uuid.uuid4().hex
        self.client_session = None
        self.digests = set()
        self.request_count = 0
        super().__init__(("127.0.0.1", port), Handler)
        atomic_json(queue.root / "server.json", {"version": VERSION, "pid": os.getpid(),
                    "session_id": self.session_id, "base_url": self.base_url,
                    "timeout_seconds": timeout, "max_request_bytes": MAX_REQUEST,
                    "max_response_bytes": MAX_RESPONSE, "max_turns": max_turns})

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server_port}/v1"


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_):
        pass

    def json_error(self, error):
        data = encode({"error": {"code": error.code, "message": error.message, "type": "bridge_error"}})
        self.send_response(error.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def check_peer(self):
        if self.headers.get("Origin") or self.headers.get("Authorization"):
            raise BridgeError("forbidden_headers", "Browser origins and authorization headers are not accepted", 403)
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            raise BridgeError("invalid_host", "Expected exact loopback Host header", 403)

    def do_GET(self):
        try:
            self.check_peer()
            if self.path != "/healthz":
                raise BridgeError("unsupported_endpoint", "Only POST /v1/responses and GET /healthz are supported", 404)
            data = encode({"status": "ok", "version": VERSION})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except BridgeError as exc:
            self.json_error(exc)

    def do_POST(self):
        acquired, rid = False, None
        q = self.server.queue
        try:
            self.check_peer()
            if self.path != "/v1/responses":
                raise BridgeError("unsupported_endpoint", "Compaction, WebSockets, and other endpoints are unsupported", 404)
            if self.headers.get("Content-Encoding", "identity") != "identity" or self.headers.get("Transfer-Encoding"):
                raise BridgeError("unsupported_encoding", "Only uncompressed Content-Length JSON is supported", 415)
            if self.headers.get_content_type() != "application/json":
                raise BridgeError("unsupported_media_type", "Expected application/json", 415)
            lengths = self.headers.get_all("Content-Length") or []
            if len(lengths) != 1 or not lengths[0].isdigit():
                raise BridgeError("invalid_length", "Exactly one valid Content-Length is required", 411)
            length = int(lengths[0])
            if length > MAX_REQUEST:
                raise BridgeError("payload_too_large", "Request exceeds 1 MiB", 413)
            # Permit the immediately following turn to wait for prior delivery ledger fsync.
            # Still only one request may reach the queue; true overlap fails promptly.
            acquired = self.server.inflight.acquire(timeout=0.1)
            if not acquired:
                raise BridgeError("request_inflight", "Only one inflight request is allowed", 409)
            body = self.rfile.read(length)
            if len(body) != length:
                raise BridgeError("truncated_body", "Incomplete JSON body")
            try:
                request = decode(body)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise BridgeError("invalid_json", "Invalid JSON request") from exc
            unknown = validate_request(request)
            client_session = self.headers.get("session_id") or self.headers.get("x-client-request-id") or request.get("prompt_cache_key")
            if not isinstance(client_session, str) or not client_session or len(client_session) > 256:
                raise BridgeError("missing_session", "A stable session_id, x-client-request-id or prompt_cache_key is required")
            if self.server.client_session is None:
                self.server.client_session = client_session
            elif client_session != self.server.client_session:
                raise BridgeError("session_mismatch", "Server is bound to a different CLI session", 409)
            digest = hashlib.sha256(encode(request)).hexdigest()
            if digest in self.server.digests:
                raise BridgeError("duplicate_request", "Request replay rejected; no tool result will be repeated", 409)
            if self.server.request_count >= self.server.max_turns:
                raise BridgeError("turn_limit", "Run reached maximum request count", 429)
            self.server.request_count += 1
            self.server.digests.add(digest)
            rid = uuid.uuid4().hex
            start = time.time()
            envelope = {"version": VERSION, "request_id": rid, "session_id": self.server.session_id,
                        "sequence": self.server.request_count, "created_at": now(),
                        "deadline_at": datetime.fromtimestamp(start + self.server.response_timeout, timezone.utc).isoformat(),
                        "deadline_unix": start + self.server.response_timeout, "request_sha256": digest,
                        "unknown_input_items": unknown, "request": request}
            if len(encode(envelope)) > MAX_REQUEST + 16384:
                raise BridgeError("payload_too_large", "Request plus diagnostics exceeds queue envelope limit", 413)
            atomic_json(q.path("requests", rid), envelope, exclusive=True)
            q.event("request_queued", request_id=rid, sequence=self.server.request_count,
                    request_bytes=len(body), unknown_input_count=len(unknown))
            deadline = time.monotonic() + self.server.response_timeout
            while not q.path("responses", rid).exists():
                if time.monotonic() >= deadline:
                    raise BridgeError("broker_timeout", "No broker response before deadline", 504)
                if select.select([self.connection], [], [], 0)[0]:
                    if not self.connection.recv(1, socket.MSG_PEEK):
                        q.outcome(rid, "cancelled", reason="client_disconnected")
                        q.event("client_disconnected", request_id=rid)
                        return
                time.sleep(0.05)
            if time.monotonic() >= deadline:
                raise BridgeError("broker_timeout", "Broker response arrived after deadline", 504)
            answer = read_json(q.path("responses", rid), MAX_RESPONSE)
            if not isinstance(answer, dict) or answer.get("version") != VERSION or answer.get("request_id") != rid or (("result" in answer) == ("error" in answer)) or set(answer) - {"version", "request_id", "result", "error"}:
                raise BridgeError("invalid_broker_response", "Broker response envelope is invalid", 502)
            if "error" in answer:
                err = answer["error"]
                if not isinstance(err, dict) or not isinstance(err.get("code"), str) or not isinstance(err.get("message"), str):
                    raise BridgeError("invalid_broker_response", "Malformed broker error", 502)
                if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", err["code"]):
                    raise BridgeError("invalid_broker_response", "Invalid broker error code", 502)
                raise BridgeError(err["code"], err["message"], 502)
            if unknown:
                raise BridgeError("unsupported_input", "Unknown input items cannot be silently ignored", 422)
            item = validate_result(answer["result"], request, rid)
            events = [{"type": "response.output_item.done", "output_index": 0, "item": item},
                      {"type": "response.completed", "response": {"id": "resp_" + rid,
                       "end_turn": item["type"] == "message"}}]
            data = b"".join(b"event: " + e["type"].encode() + b"\ndata: " + encode(e) + b"\n\n" for e in events)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Request-ID", rid)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
            q.outcome(rid, "delivered", response_id="resp_" + rid, item_type=item["type"],
                      **({"call_id": item["call_id"]} if "call_id" in item else {}))
            q.event("response_delivered", request_id=rid, item_type=item["type"])
        except BridgeError as exc:
            if rid:
                q.outcome(rid, "failed", code=exc.code)
            q.event("request_error", request_id=rid, code=exc.code)
            try:
                self.json_error(exc)
            except (BrokenPipeError, ConnectionError, socket.timeout):
                pass
        except (BrokenPipeError, ConnectionError, socket.timeout):
            if rid:
                q.outcome(rid, "cancelled", reason="client_disconnected")
                q.event("client_disconnected", request_id=rid)
        except Exception as exc:
            if rid:
                q.outcome(rid, "failed", code="internal_error")
            q.event("internal_error", request_id=rid, exception_type=type(exc).__name__)
            try:
                self.json_error(BridgeError("internal_error", "Bridge internal error; see metadata log", 500))
            except (BrokenPipeError, ConnectionError, socket.timeout):
                pass
        finally:
            if acquired:
                self.server.inflight.release()


def config_text(base_url, timeout):
    return f'''# Experimental local native-agent bridge; not an external model service.
model = "native-subagent-bridge"
model_provider = "native_bridge"
approval_policy = "on-request"
sandbox_mode = "read-only"
web_search = "disabled"
model_reasoning_summary = "none"

[features]
multi_agent = false
multi_agent_v2 = false
enable_request_compression = false
unbounded_connection_retries = false
browser_use = false
computer_use = false
image_generation = false
apps = false
plugins = false

[model_providers.native_bridge]
name = "Native subagent bridge (experimental)"
base_url = {json.dumps(base_url)}
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
supports_standalone_web_search = false
request_max_retries = 0
stream_max_retries = 0
stream_idle_timeout_ms = {int((timeout + 30) * 1000)}
'''


def run_cli(root, prompt, codex=DEFAULT_CODEX, timeout=600):
    root = Path(root).resolve()
    info = read_json(root / "server.json")
    if not re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})/v1", info["base_url"]) or int(info["base_url"].split(":")[2].split("/")[0]) > 65535:
        raise BridgeError("invalid_endpoint", "CLI provider must use the fixed IPv4 loopback endpoint")
    home, workspace = root / "codex-home", root / "workspace"
    home.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700, exist_ok=True)
    (root / "home").mkdir(mode=0o700)
    (root / "tmp").mkdir(mode=0o700)
    (home / "config.toml").write_text(config_text(info["base_url"], info["timeout_seconds"]))
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TERM", "TMPDIR") if key in os.environ}
    env.update({"HOME": str(root / "home"), "CODEX_HOME": str(home), "NO_PROXY": "127.0.0.1,localhost", "TMPDIR": str(root / "tmp"), "XDG_RUNTIME_DIR": str(root / "tmp")})
    cmd = [str(Path(codex).resolve()), "exec", "--skip-git-repo-check", "--ephemeral", "--color", "never",
           "--json", "--sandbox", "read-only", "-C", str(workspace), "-o", str(root / "final.txt"), "-"]
    atomic_json(root / "cli-invocation.json", {"argv": cmd, "codex_home": str(home),
                "environment_names": sorted(env), "sandbox": "read-only", "started_at": now()})
    with (root / "cli.stdout.jsonl").open("wb") as out, (root / "cli.stderr.log").open("wb") as err:
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=out, stderr=err, env=env)
        atomic_json(root / "cli-process.json", {"pid": process.pid})
        try:
            process.communicate(prompt.encode(), timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            atomic_json(root / "cli-result.json", {"returncode": process.returncode, "timed_out": True, "finished_at": now()})
            raise BridgeError("cli_timeout", "CLI exceeded wall-clock limit")
    result = {"returncode": process.returncode, "timed_out": False, "finished_at": now()}
    atomic_json(root / "cli-result.json", result)
    return result


def mock_worker(queue, mode):
    """DETERMINISTIC TRANSPORT FIXTURE. This is deliberately not inference."""
    seen = 0
    while seen < (2 if mode == "tool" else 1):
        req = queue.claim(20)
        if req is None:
            raise BridgeError("mock_timeout", "No request arrived for transport fixture")
        rid, request = req["request_id"], req["request"]
        if mode == "tool" and seen == 0:
            toolset = request_tools(request)
            candidate = next(((ns, n) for (ns, n), t in toolset.items() if n in ("shell_command", "shell", "exec_command") and t["type"] == "function"), None)
            if not candidate:
                queue.submit(rid, error={"code": "mock_missing_shell", "message": "No supported shell tool advertised"})
                return
            namespace, name = candidate
            arguments = ({"command": "printf 'TRANSPORT_FIXTURE_NONCE\\n'", "workdir": str(queue.root / "workspace")} if name == "shell_command" else
                         {"command": ["/bin/sh", "-c", "printf 'TRANSPORT_FIXTURE_NONCE\\n'"], "workdir": str(queue.root / "workspace")} if name == "shell" else
                         {"cmd": "printf 'TRANSPORT_FIXTURE_NONCE\\n'", "workdir": str(queue.root / "workspace"), "max_output_tokens": 100})
            result = {"kind": "function_call", "name": name, "arguments": arguments}
            if namespace:
                result["namespace"] = namespace
            queue.submit(rid, result=result)
        else:
            if mode == "tool":
                outputs = [i for i in request["input"] if i.get("type") in ("function_call_output", "custom_tool_call_output")]
                if not outputs or "TRANSPORT_FIXTURE_NONCE" not in encode(outputs).decode():
                    queue.submit(rid, error={"code": "mock_tool_failed", "message": "CLI tool output did not contain fixture nonce"})
                    return
            queue.submit(rid, result={"kind": "message", "text": "DETERMINISTIC_TRANSPORT_TEST_OK"})
        seen += 1


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--root", required=True)
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--timeout", type=float, default=180)
    serve.add_argument("--max-turns", type=int, default=MAX_TURNS)
    claim = sub.add_parser("claim")
    claim.add_argument("--root", required=True)
    claim.add_argument("--wait", type=float, default=0)
    submit = sub.add_parser("submit")
    submit.add_argument("--root", required=True)
    submit.add_argument("--request-id", required=True)
    submit.add_argument("--file", required=True, help="JSON file containing result or {error:{code,message}}; '-' reads stdin")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--root", required=True)
    inspect.add_argument("--request-id", required=True)
    run = sub.add_parser("run-cli")
    run.add_argument("--root", required=True)
    run.add_argument("--codex", default=str(DEFAULT_CODEX))
    run.add_argument("--prompt-file", required=True)
    run.add_argument("--timeout", type=float, default=600)
    smoke = sub.add_parser("mock-smoke", help="DETERMINISTIC TRANSPORT TEST ONLY; no native inference")
    smoke.add_argument("--root", required=True)
    smoke.add_argument("--codex", default=str(DEFAULT_CODEX))
    smoke.add_argument("--mode", choices=("message", "tool"), default="message")
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            if not 1 <= args.timeout <= 1800 or not 1 <= args.max_turns <= 128:
                raise BridgeError("invalid_limit", "Timeout must be 1..1800 seconds; turns 1..128")
            q = Queue(args.root)
            q.initialize()
            server = BridgeServer(q, args.port, args.timeout, args.max_turns)
            print(encode({"base_url": server.base_url, "root": str(q.root)}).decode(), flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        elif args.command == "claim":
            if not 0 <= args.wait <= 1800:
                raise BridgeError("invalid_limit", "Wait must be 0..1800 seconds")
            result = Queue(args.root).claim(args.wait)
            print(encode(result).decode())
            return 0 if result else 2
        elif args.command == "submit":
            if args.file == "-":
                data = sys.stdin.buffer.read(MAX_RESPONSE + 1)
            else:
                with Path(args.file).open("rb") as f:
                    data = f.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                raise BridgeError("payload_too_large", "Response exceeds size limit")
            result = decode(data)
            Queue(args.root).submit(args.request_id, error=result["error"] if isinstance(result, dict) and set(result) == {"error"} else None,
                                    result=None if isinstance(result, dict) and set(result) == {"error"} else result)
            print(encode({"submitted": args.request_id}).decode())
        elif args.command == "inspect":
            q = Queue(args.root)
            print(encode(read_json(q.path("requests", args.request_id))).decode())
        elif args.command == "run-cli":
            prompt = Path(args.prompt_file).read_text()
            result = run_cli(args.root, prompt, args.codex, args.timeout)
            print(encode(result).decode())
            return result["returncode"]
        elif args.command == "mock-smoke":
            q = Queue(args.root)
            q.initialize()
            server = BridgeServer(q, timeout=30)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            worker_errors = []
            def worker():
                try:
                    mock_worker(q, args.mode)
                except Exception as exc:
                    worker_errors.append(str(exc))
            fixture = threading.Thread(target=worker, daemon=True)
            thread.start()
            fixture.start()
            try:
                result = run_cli(q.root, "This is a deterministic transport test. Follow the returned fixture response.", args.codex, 90)
                fixture.join(2)
                final = (q.root / "final.txt").read_text() if (q.root / "final.txt").exists() else ""
                cli_events = [decode(line) for line in (q.root / "cli.stdout.jsonl").read_bytes().splitlines() if line.strip()]
                executed = any(event.get("type") == "item.completed" and
                               event.get("item", {}).get("type") == "command_execution" and
                               event["item"].get("exit_code") == 0 and
                               "TRANSPORT_FIXTURE_NONCE" in event["item"].get("aggregated_output", "").splitlines()
                               for event in cli_events) if args.mode == "tool" else None
                success = result["returncode"] == 0 and final == "DETERMINISTIC_TRANSPORT_TEST_OK" and not worker_errors and (executed if args.mode == "tool" else True)
                print(encode({"test_kind": "deterministic_transport_only", "mode": args.mode, "passed": success,
                            "cli_returncode": result["returncode"], "cli_tool_execution_verified": executed, "worker_errors": worker_errors, "root": str(q.root)}).decode())
                return 0 if success else 1
            finally:
                server.shutdown()
                server.server_close()
    except (BridgeError, OSError, ValueError) as exc:
        print(encode({"error": {"code": getattr(exc, "code", type(exc).__name__), "message": str(exc)}}).decode(), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
