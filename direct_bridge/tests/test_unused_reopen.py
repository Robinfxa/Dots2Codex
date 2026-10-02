"""Offline one-shot unused-reopen tests; no real clients, tunnels, or credentials."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from transport import Queue, Binding, RouteAuthorization

spec = importlib.util.spec_from_file_location("unused_reopen_fixture", ROOT / "scripts/direct.py")
direct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(direct)


class UnusedReopenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="unused-reopen-fixture-")
        self.state = Path(self.temp.name) / "state"
        self.config = direct.route_config(self.state, 4, time.time() - 1)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.settings = {"python": sys.executable, "http_port": port, "created_at": time.time()}
        direct.commit_setup(self.state, self.config, self.settings)
        self.route = self.state / "route"
        self.database = Path(self.config["db_path"])
        self.queue = Queue(self.database)
        self.queue.install_route(RouteAuthorization(Binding(**self.config["binding"]),
            self.config["client_actor"], self.config["worker_actor"], self.config["not_before"],
            self.config["expires_at"], self.config["approval_ref"]))
        self.marker = self.route / "codex-started.json"
        direct.write_admission_record(self.marker, {"version": direct.CODEX_VERSION,
            "workdir": self.temp.name, "started_at": time.time()})
        self.before = {name: (self.route / name).read_bytes() for name in
            ("codex-started.json", "config.json", "launcher.json", "state.sqlite3")}
        self.prepared = self.route / "codex-unused-reopen-prepared.json"
        self.consumed = self.route / "codex-unused-reopen-consumed.json"
        self.output = io.StringIO()
        self.capture = contextlib.redirect_stdout(self.output)
        self.capture.__enter__()

    def tearDown(self):
        self.capture.__exit__(None, None, None)
        self.temp.cleanup()

    def launch(self, *, inputs=None, error=None, stopped_error=None, available=False):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(direct, "require_interactive"))
            stack.enter_context(patch.object(direct, "existing_codex", return_value="/fixture/codex"))
            stack.enter_context(patch.object(direct, "codex_version", return_value="codex-cli " + direct.CODEX_VERSION))
            stopped = stack.enter_context(patch.object(direct, "require_clients_stopped", side_effect=stopped_error))
            stack.enter_context(patch.object(direct, "port_available", return_value=available))
            stack.enter_context(patch.dict(os.environ, {direct.BEARER_NAME: "fixture-only-long-bearer",
                direct.KEY_NAME: "fixture-only-control-key", "OPENAI_API_KEY": "fixture-only-unrelated-key"}))
            stack.enter_context(patch("builtins.input", side_effect=inputs or ["REOPEN", "READY"]))
            execute = stack.enter_context(patch.object(direct.os, "execve", side_effect=error))
            direct.codex_reopen_unused(self.state)
            return execute, stopped

    def insert_request(self, state, *, wal=False):
        connection = sqlite3.connect(self.database)
        if wal:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("INSERT INTO requests(request_id,route_id,seq,fingerprint,payload,schema_refs,state,execution_reserved) VALUES(?,?,?,?,?,?,?,?)",
            ("fixture-request", self.config["binding"]["route_id"], 1, "hash", b"{}", b"{}", state, int(state == "claimed")))
        connection.commit()
        return connection

    def test_empty_route_reopens_once_and_preserves_original_bytes(self):
        execute, stopped = self.launch()
        self.assertEqual(stopped.call_args_list[0].kwargs, {"include_bridge": True})
        self.assertEqual(stopped.call_args_list[1].kwargs, {"include_bridge": False})
        binary, argv, env = execute.call_args.args
        self.assertEqual(binary, "/fixture/codex")
        self.assertEqual(argv[-4:], ["-m", "gpt-6-astra", "-C", self.temp.name])
        self.assertNotIn(direct.KEY_NAME, env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(env[direct.BEARER_NAME], "fixture-only-long-bearer")
        self.assertTrue(self.prepared.exists())
        self.assertTrue(self.consumed.exists())
        for name, value in self.before.items():
            self.assertEqual((self.route / name).read_bytes(), value, name)
        for path in (self.prepared, self.consumed):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("fixture-only", path.read_text())
        self.assertNotIn("fixture-only", self.output.getvalue())
        with self.assertRaisesRegex(direct.SetupError, "already exists"):
            self.launch()

    def test_all_request_states_refused_including_wal_only(self):
        for state in ("queued", "claimed", "completed", "cancelled"):
            with self.subTest(state=state):
                connection = self.insert_request(state, wal=True)
                try:
                    self.assertTrue(Path(str(self.database) + "-wal").exists())
                    with self.assertRaisesRegex(direct.SetupError, "durable request"):
                        self.launch()
                    self.assertFalse(self.prepared.exists())
                finally:
                    connection.execute("DELETE FROM requests")  # Synthetic fixture only.
                    connection.commit()
                    connection.close()

    def test_registered_schema_without_request_refused(self):
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO schemas VALUES(?,?,?,?)", (self.config["binding"]["route_id"], "fixture", "hash", b"{}"))
        with self.assertRaisesRegex(direct.SetupError, "schema activity"):
            self.launch()

    def test_unknown_control_table_fails_closed(self):
        with sqlite3.connect(self.database) as db:
            db.execute("CREATE TABLE execution_records(id TEXT)")
        with self.assertRaisesRegex(direct.SetupError, "could not be verified"):
            self.launch()

    def test_malformed_empty_request_table_refused(self):
        with sqlite3.connect(self.database) as db:
            db.execute("DROP TABLE requests")
            db.execute("CREATE TABLE requests(dummy TEXT)")
        with self.assertRaisesRegex(direct.SetupError, "could not be verified"):
            self.launch()
        self.assertFalse(self.prepared.exists())

    def test_desktop_app_server_does_not_count_as_legacy_cli(self):
        import subprocess
        for command in ("/Applications/Codex.app/Contents/Resources/codex app-server --stdio", "/opt/bin/codex daemon run"):
            with self.subTest(command=command), patch.object(direct.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "999999 " + command + "\n")):
                direct.require_clients_stopped(include_bridge=True)

    def test_missing_database_is_not_created(self):
        self.database.unlink()
        with self.assertRaises(direct.SetupError):
            self.launch()
        self.assertFalse(self.database.exists())

    def test_corrupt_database_preserved(self):
        self.database.write_bytes(b"fixture-corrupt-database")
        with self.assertRaises(direct.SetupError):
            self.launch()
        self.assertEqual(self.database.read_bytes(), b"fixture-corrupt-database")

    def test_database_symlink_refused(self):
        other = self.database.with_name("preserved.sqlite3")
        self.database.rename(other)
        self.database.symlink_to(other)
        with self.assertRaises(direct.SetupError):
            self.launch()

    def test_private_parent_allows_original_default_umask_database(self):
        self.database.chmod(0o644)
        self.launch()
        self.assertEqual(self.database.stat().st_mode & 0o777, 0o644)

    def test_revoked_clock_rollback_and_binding_mismatch_refused(self):
        for statement, value in (("revoked", 1), ("last_clock", time.time() + 100),
                                 ("client_actor", "wrong"), ("authorization", b"{}")):
            with self.subTest(column=statement):
                with sqlite3.connect(self.database) as db:
                    old = db.execute(f"SELECT {statement} FROM routes").fetchone()[0]
                    db.execute(f"UPDATE routes SET {statement}=?", (value,))
                with self.assertRaises(direct.SetupError):
                    self.launch()
                self.assertFalse(self.prepared.exists())
                with sqlite3.connect(self.database) as db:
                    db.execute(f"UPDATE routes SET {statement}=?", (old,))

    def test_live_listener_refused_before_reservation(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", self.settings["http_port"]))
            sock.listen()
            with self.assertRaisesRegex(direct.SetupError, "Stop the existing"):
                self.launch()
        self.assertFalse(self.prepared.exists())

    def test_guard_blocks_competing_listener_during_zero_audit(self):
        audit = direct.unused_database_snapshot
        calls = []
        def wrapped(config):
            if not calls:
                with socket.socket() as competitor:
                    with self.assertRaises(OSError):
                        competitor.bind(("127.0.0.1", self.settings["http_port"]))
            calls.append(True)
            return audit(config)
        with patch.object(direct, "unused_database_snapshot", side_effect=wrapped):
            self.launch()
        self.assertEqual(len(calls), 2)

    def test_running_process_refused(self):
        with self.assertRaises(direct.SetupError):
            self.launch(stopped_error=direct.SetupError("fixture running client"))
        self.assertFalse(self.prepared.exists())

    def test_cancel_before_reservation_allows_no_new_record(self):
        with self.assertRaises(direct.SetupError):
            self.launch(inputs=[""])
        self.assertFalse(self.prepared.exists())

    def test_cancel_after_reservation_burns_attempt(self):
        with self.assertRaises(direct.SetupError):
            self.launch(inputs=["REOPEN", ""])
        self.assertTrue(self.prepared.exists())
        self.assertFalse(self.consumed.exists())
        with self.assertRaisesRegex(direct.SetupError, "already exists"):
            self.launch()

    def test_exec_failure_preserves_all_history(self):
        with self.assertRaisesRegex(direct.SetupError, "Both recovery records"):
            self.launch(error=OSError("fixture-sensitive-error"))
        self.assertTrue(self.prepared.exists())
        self.assertTrue(self.consumed.exists())
        self.assertEqual(self.marker.read_bytes(), self.before["codex-started.json"])
        self.assertNotIn("fixture-sensitive-error", self.output.getvalue())

    def test_activity_after_restart_refused(self):
        def answers(prompt):
            if "READY" in prompt:
                self.insert_request("queued").close()
                return "READY"
            return "REOPEN"
        with self.assertRaisesRegex(direct.SetupError, "durable request"):
            self.launch(inputs=answers)
        self.assertTrue(self.prepared.exists())
        self.assertFalse(self.consumed.exists())

    def test_semantically_identical_config_rewrite_after_ready_refused(self):
        def answers(prompt):
            if "READY" in prompt:
                path = self.route / "config.json"
                path.write_bytes(path.read_bytes() + b"\n")
                return "READY"
            return "REOPEN"
        with self.assertRaisesRegex(direct.SetupError, "history changed"):
            self.launch(inputs=answers)
        self.assertFalse(self.consumed.exists())

    def test_expiry_while_waiting_for_ready_refused(self):
        real_time = time.time
        def answers(prompt):
            if "READY" in prompt:
                direct.time.time = lambda: self.config["expires_at"] + 1
                return "READY"
            return "REOPEN"
        try:
            with self.assertRaises(direct.SetupError):
                self.launch(inputs=answers)
        finally:
            direct.time.time = real_time
        self.assertFalse(self.consumed.exists())

    def test_restart_missing_refuses_after_preserved_reservation(self):
        with self.assertRaisesRegex(direct.SetupError, "listener is missing"):
            self.launch(available=True)
        self.assertTrue(self.prepared.exists())
        self.assertFalse(self.consumed.exists())

    def test_competing_exclusive_consumption_refused(self):
        def answers(prompt):
            if "READY" in prompt:
                direct.write_admission_record(self.consumed, {"fixture": "competitor"})
                return "READY"
            return "REOPEN"
        with self.assertRaisesRegex(direct.SetupError, "already reserved"):
            self.launch(inputs=answers)
        self.assertEqual(json.loads(self.consumed.read_text()), {"fixture": "competitor"})

    def test_cli_and_bridge_process_names_refused_without_echo(self):
        import subprocess
        for command in ("/opt/bin/codex --fixture-secret", "node /opt/bin/codex.js", "/bin/python -m mcp_adapter --config /fixture/config"):
            with self.subTest(command=command), patch.object(direct.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "999999 " + command + "\n")):
                with self.assertRaises(direct.SetupError) as error:
                    direct.require_clients_stopped(include_bridge=True)
                self.assertNotIn(command, str(error.exception))


if __name__ == "__main__":
    unittest.main()
