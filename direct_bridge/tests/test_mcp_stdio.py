"""Real OS pipe tests against the official MCP SDK, never native execution."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PipeClient:
    def __init__(self, **env):
        self.proc = subprocess.Popen([sys.executable, "tests/mcp_fixture.py"], cwd=ROOT,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env={**os.environ, **env})
        self.messages = queue.Queue()
        self.raw = []
        self.errors = bytearray()
        def read_output():
            for line in self.proc.stdout:
                self.raw.append(line)
                try:
                    self.messages.put(json.loads(line))
                except Exception:
                    self.messages.put({"NOT_JSON": line.decode(errors="replace")})
        def read_errors():
            self.errors.extend(self.proc.stderr.read())
        self.reader = threading.Thread(target=read_output, daemon=True)
        self.error_reader = threading.Thread(target=read_errors, daemon=True)
        self.reader.start()
        self.error_reader.start()

    def send(self, message):
        self.proc.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        self.proc.stdin.flush()

    def request(self, identifier, method, params=None):
        item = {"jsonrpc": "2.0", "id": identifier, "method": method}
        if params is not None:
            item["params"] = params
        self.send(item)

    def recv(self, identifier, timeout=4):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            item = self.messages.get(timeout=until - time.monotonic())
            assert "NOT_JSON" not in item, item
            if item.get("id") == identifier:
                return item
        raise TimeoutError("missing_response")

    def initialize(self, version="2025-06-18"):
        self.request(1, "initialize", {"protocolVersion": version,
                     "capabilities": {}, "clientInfo": {"name": "offline-pipe-test", "version": "1"}})
        item = self.recv(1)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return item

    def call(self, identifier, name, arguments):
        self.request(identifier, "tools/call", {"name": name, "arguments": arguments})
        return self.recv(identifier)

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                self.proc.wait(timeout=3)
        self.reader.join(timeout=1)
        self.error_reader.join(timeout=1)
        self.proc.stdout.close()
        self.proc.stderr.close()
        if not self.proc.stdin.closed:
            self.proc.stdin.close()


class TestStdio(unittest.TestCase):
    def setUp(self):
        self.clients = []

    def client(self, **env):
        c = PipeClient(**env)
        self.clients.append(c)
        return c

    def tearDown(self):
        for c in self.clients:
            c.close()

    def test_initialize_list_and_call(self):
        c = self.client()
        result = c.initialize()["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["capabilities"]["tools"], {"listChanged": False})
        self.assertFalse(result["capabilities"].get("experimental"))
        c.request(2, "tools/list")
        tools = c.recv(2)["result"]["tools"]
        self.assertEqual(len(tools), 8)
        self.assertEqual({t["name"] for t in tools}, {"bridge_status", "get_request", "discover_tools", "lookup_schema", "submit_action_and_wait_result", "await_result", "finish_request", "cancel_request"})
        call = c.call(3, "bridge_status", {})["result"]
        self.assertFalse(call["isError"])
        self.assertEqual(call["structuredContent"]["name"], "bridge_status")
        for line in c.raw:
            self.assertEqual(json.loads(line)["jsonrpc"], "2.0")

    def test_sdk_version_negotiation(self):
        c = self.client()
        result = c.initialize("2099-01-01")["result"]
        self.assertEqual(result["protocolVersion"], "2025-11-25")
        self.assertNotIn("tasks", result["capabilities"])

    def test_unknown_and_wire_authority_rejected(self):
        c = self.client()
        c.initialize()
        self.assertEqual(c.call(2, "no_such_tool", {})["result"]["structuredContent"]["error"], "unknown_tool")
        for identifier, value in enumerate(({"binding": {}}, {"principal": "fake"}, {"actor_id": "fake"}, {"wait_ms": 6000}, {"wait_ms": True}), 3):
            response = c.call(identifier, "get_request", value)["result"]
            self.assertTrue(response["isError"])
            self.assertEqual(response["structuredContent"]["error"], "invalid_arguments")

    def test_malformed_json_and_method_recover_without_payload_logs(self):
        c = self.client()
        c.initialize()
        c.proc.stdin.write(b'{invalid "private-secret-must-never-leak"}\n')
        c.proc.stdin.flush()
        c.request(2, "unsupported_method", {"private-secret-must-never-leak": "x"})
        self.assertIn("error", c.recv(2))
        self.assertFalse(c.call(3, "bridge_status", {})["result"]["isError"])
        c.close()
        self.assertNotIn(b"private-secret-must-never-leak", c.errors)
        self.assertNotIn(b"private-secret-must-never-leak", b"".join(c.raw))

    def test_runtime_exception_redacted(self):
        c = self.client(MCP_FIXTURE_MODE="error")
        c.initialize()
        self.assertEqual(c.call(2, "bridge_status", {})["result"]["structuredContent"]["error"], "runtime_error")
        c.close()
        self.assertNotIn(b"private-secret-must-never-leak", b"".join(c.raw) + c.errors)

    def test_timeout_releases_runtime_cooperatively(self):
        c = self.client(MCP_FIXTURE_MODE="wait", MCP_CALL_TIMEOUT="0.08", MCP_CALL_LIMIT="1")
        c.initialize()
        self.assertEqual(c.call(2, "get_request", {})["result"]["structuredContent"]["error"], "request_timeout")
        time.sleep(0.03)
        self.assertFalse(c.call(3, "bridge_status", {})["result"]["isError"])

    def test_cancel_does_not_block_following_request(self):
        c = self.client(MCP_FIXTURE_MODE="wait", MCP_CALL_LIMIT="1")
        c.initialize()
        c.request(2, "tools/call", {"name": "get_request", "arguments": {}})
        time.sleep(0.04)
        c.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2}})
        self.assertEqual(c.recv(2)["error"]["message"], "Request cancelled")
        time.sleep(0.03)
        self.assertFalse(c.call(3, "bridge_status", {})["result"]["isError"])

    def test_inflight_limit(self):
        c = self.client(MCP_FIXTURE_MODE="wait", MCP_CALL_LIMIT="1")
        c.initialize()
        c.request(2, "tools/call", {"name": "get_request", "arguments": {}})
        time.sleep(0.05)
        result = c.call(3, "get_request", {})
        self.assertEqual(result["result"]["structuredContent"]["error"], "too_many_inflight_calls")
        c.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2}})
        c.recv(2)

    def test_oversize_input_closes_bounded_stream(self):
        c = self.client(MCP_INPUT_LIMIT="1024")
        c.initialize()
        c.proc.stdin.write(b"z" * 1100 + b"\n")
        c.proc.stdin.flush()
        self.assertEqual(c.proc.wait(timeout=4), 0)
        c.close()
        self.assertIn(b"stdio_limit", c.errors)

    def test_oversize_result_returns_small_error(self):
        c = self.client(MCP_FIXTURE_MODE="oversize")
        c.initialize()
        self.assertEqual(c.call(2, "bridge_status", {})["result"]["structuredContent"]["error"], "result_too_large")
        self.assertLess(len(c.raw[-1]), 1000)

    def test_invalid_utf8_and_partial_eof_are_closed(self):
        for payload in (b"\xff\n", b'{"jsonrpc":"2.0"}'):
            c = self.client()
            c.initialize()
            c.proc.stdin.write(payload)
            c.proc.stdin.flush()
            c.proc.stdin.close()
            self.assertEqual(c.proc.wait(timeout=4), 0)


if __name__ == "__main__":
    unittest.main()
