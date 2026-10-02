"""Non-secret config validation and foreground launcher checks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mcp_adapter.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config.json"
        self.config = {"trust_mode": "single_owner_stdio", "db_path": "state.sqlite3",
                       "binding": {"grant_id": "fixture-grant", "route_id": "fixture-route",
                                   "session_id": "fixture-session", "thread_id": "fixture-thread",
                                   "model": "gpt-6.1-sol", "reasoning_effort": "xhigh"},
                       "client_actor": "fixture-client", "worker_actor": "fixture-worker",
                       "context_epoch": "fixture-epoch", "approval_ref": "explicit-offline-fixture",
                       "not_before": time.time() - 1, "expires_at": time.time() + 3600}
        self.env = {"DOTS_BRIDGE_HTTP_BEARER": "test-only-not-a-live-bearer"}
        self.write()

    def tearDown(self):
        self.temp.cleanup()

    def write(self):
        self.path.write_text(json.dumps(self.config))

    def test_runtime_env_only_and_relative_database(self):
        value = load_config(self.path, environ=self.env)
        self.assertEqual(value["db_path"], str(Path(self.temp.name) / "state.sqlite3"))
        self.assertEqual(value["http_bearer"], self.env["DOTS_BRIDGE_HTTP_BEARER"])
        self.assertNotIn("trust_mode", value)
        self.assertNotIn(self.env["DOTS_BRIDGE_HTTP_BEARER"], self.path.read_text())

    def test_missing_or_unsafe_environment_bearer_rejected(self):
        for bearer in (None, "short", "x" * 16 + "\n", "x" * 16 + "é"):
            with self.assertRaises(ConfigError):
                load_config(self.path, environ={} if bearer is None else {"DOTS_BRIDGE_HTTP_BEARER": bearer})

    def test_secrets_must_not_be_config_fields(self):
        for key in ("http_bearer", "CONTROL_PLANE_API_KEY", "api_key"):
            self.config[key] = "private-secret-must-never-leak"
            self.write()
            with self.assertRaisesRegex(ConfigError, "unknown_or_secret_config_field"):
                load_config(self.path, environ=self.env)
            self.config.pop(key)

    def test_require_explicit_grant_window_and_single_owner_mode(self):
        for key in ("approval_ref", "not_before", "expires_at", "trust_mode"):
            old = self.config.pop(key)
            self.write()
            with self.assertRaises(ConfigError):
                load_config(self.path, environ=self.env)
            self.config[key] = old
        self.config["trust_mode"] = "public_multiuser"
        self.write()
        with self.assertRaises(ConfigError):
            load_config(self.path, environ=self.env)

    def test_duplicate_keys_nan_and_oversize_rejected(self):
        for raw in ('{"a":1,"a":2}', '{"a":NaN}', 'x' * 65537):
            self.path.write_text(raw)
            with self.assertRaises(ConfigError):
                load_config(self.path, environ=self.env)

    def test_placeholder_example_not_runnable(self):
        with self.assertRaises(ConfigError):
            load_config(ROOT / "mcp_adapter/config.example.json", environ=self.env)

    def test_launcher_failure_is_redacted(self):
        self.config["http_bearer"] = "private-secret-must-never-leak"
        self.write()
        run = subprocess.run([str(ROOT / "scripts/start_mcp.sh"), "--config", str(self.path)],
                             input=b"", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env={**os.environ, **self.env}, timeout=5)
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stdout, b"")
        self.assertIn(b"bridge_mcp:", run.stderr)
        self.assertNotIn(b"private-secret-must-never-leak", run.stderr)
        self.assertNotIn(b"Traceback", run.stderr)


if __name__ == "__main__":
    unittest.main()
