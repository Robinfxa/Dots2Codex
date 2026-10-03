"""Offline launcher tests: no installs, tunnel connection, credentials, or Mac use."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("direct_setup_fixture", ROOT / "scripts/direct.py")
direct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(direct)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="direct fixture ")
        self.state = Path(self.temp.name) / "private state"
        self.config = direct.route_config(self.state, 4, time.time())
        self.settings = {"python": str(Path(sys.executable).absolute()), "http_port": 18765,
                         "created_at": time.time()}
        self.output = io.StringIO()
        self.capture = contextlib.redirect_stdout(self.output)
        self.capture.__enter__()

    def tearDown(self):
        self.capture.__exit__(None, None, None)
        self.temp.cleanup()

    def prepared(self):
        direct.commit_setup(self.state, self.config, self.settings)

    def test_config_has_unique_logical_ids_and_finite_window(self):
        other = direct.route_config(self.state, 4, 100)
        self.assertNotEqual(self.config["binding"], other["binding"])
        self.assertEqual(other["not_before"], 100)
        self.assertEqual(other["expires_at"], 14500)
        self.assertEqual(other["binding"]["model"], "gpt-6-astra")
        self.assertEqual(other["binding"]["reasoning_effort"], "xhigh")
        self.assertFalse(Path(self.config["db_path"]).exists())

    def test_commit_private_and_readable_with_spaces(self):
        self.prepared()
        self.assertEqual(direct.read_setup(self.state), (self.config, self.settings))
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        for path in (self.state / "route").iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.state / ".setup.lock").exists())

    def test_repeat_commit_never_resets_route_or_database(self):
        self.prepared()
        db = self.state / "route/state.sqlite3"
        db.write_bytes(b"uncertain-execution-state")
        prior = (self.state / "route/config.json").read_bytes()
        with self.assertRaisesRegex(direct.SetupError, "Existing route preserved"):
            direct.commit_setup(self.state, direct.route_config(self.state, 1, time.time()), self.settings)
        self.assertEqual(prior, (self.state / "route/config.json").read_bytes())
        self.assertEqual(db.read_bytes(), b"uncertain-execution-state")

    def test_failure_before_commit_removes_partial_config(self):
        with patch.object(direct.json, "dump", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.prepared()
        self.assertEqual(list(self.state.iterdir()), [])

    def test_conflicting_setup_lock_is_preserved(self):
        self.state.mkdir(mode=0o700)
        (self.state / ".setup.lock").mkdir()
        with self.assertRaisesRegex(direct.SetupError, "Another setup"):
            self.prepared()
        self.assertTrue((self.state / ".setup.lock").exists())

    def test_symlink_and_source_state_paths_rejected(self):
        target = Path(self.temp.name) / "target"
        target.mkdir(mode=0o700)
        self.state.symlink_to(target, target_is_directory=True)
        with self.assertRaises(direct.SetupError):
            direct.check_state_path(self.state)
        with self.assertRaises(direct.SetupError):
            direct.check_state_path(ROOT / "runtime")

    def test_nonprivate_state_and_config_rejected(self):
        self.state.mkdir(mode=0o755)
        with self.assertRaises(direct.SetupError):
            direct.check_state_path(self.state)
        self.state.chmod(0o700)
        self.prepared()
        (self.state / "route/config.json").chmod(0o644)
        with self.assertRaises(direct.SetupError):
            direct.read_setup(self.state)

    def test_malformed_or_secret_config_rejected_without_echo(self):
        self.prepared()
        self.config["http_bearer"] = "fixture-secret-never-echo"
        (self.state / "route/config.json").write_text(json.dumps(self.config))
        with self.assertRaises(direct.SetupError) as caught:
            direct.read_setup(self.state)
        self.assertNotIn("fixture-secret", str(caught.exception))
        self.assertNotIn("fixture-secret", self.output.getvalue())

    def test_confirmation_default_and_eof_deny(self):
        with patch("builtins.input", return_value=""):
            with self.assertRaises(direct.SetupError):
                direct.confirm("Prepare route", "SETUP")
        with patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(EOFError):
                direct.confirm("Prepare route", "SETUP")
        self.assertFalse(self.state.exists())

    def test_noninteractive_setup_run_denied(self):
        with patch.object(direct.sys.stdin, "isatty", return_value=False):
            for action in (direct.setup, direct.run):
                with self.assertRaises(direct.SetupError):
                    action(self.state)
        self.assertFalse(self.state.exists())

    def test_cancel_setup_before_mutation(self):
        with patch.object(direct, "require_interactive"), patch.object(direct, "choose_python", return_value=sys.executable), \
             patch("builtins.input", side_effect=["4", "18765", ""]):
            with self.assertRaises(direct.SetupError):
                direct.setup(self.state)
        self.assertFalse(self.state.exists())

    def test_setup_without_dependencies_never_installs_unapproved(self):
        with patch.object(direct, "sdk_ready", return_value=False), patch("builtins.input", return_value=""), \
             patch.object(direct.subprocess, "run") as run:
            with self.assertRaises(direct.SetupError):
                direct.choose_python(self.state)
            run.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_mock_install_uses_private_python_and_official_index_no_secrets(self):
        with patch.dict(os.environ, {direct.BEARER_NAME: "fixture-bearer-private", direct.KEY_NAME: "fixture-key-private", "PIP_INDEX_URL": "https://secret.invalid/"}):
            with patch.object(direct, "sdk_ready", side_effect=[False, True]), \
                 patch("builtins.input", return_value="INSTALL"), patch.object(direct.subprocess, "run") as run:
                result = direct.choose_python(self.state)
            self.assertEqual(result, str(self.state / ".venv/bin/python"))
            self.assertEqual(run.call_count, 2)
            self.assertIn("https://pypi.org/simple", run.call_args.args[0])
            self.assertNotIn(direct.KEY_NAME, run.call_args.kwargs["env"])
            self.assertNotIn(direct.BEARER_NAME, run.call_args.kwargs["env"])
            self.assertNotIn("PIP_INDEX_URL", run.call_args.kwargs["env"])
            self.assertNotIn("fixture-key", self.output.getvalue())

    def test_missing_sdk_probe_is_quiet(self):
        self.assertFalse(direct.sdk_ready(self.state / "not a python"))

    def test_mcp_command_roundtrips_paths_with_spaces_and_quotes(self):
        self.settings["python"] = "/tmp/Python's env/bin/python"
        expected = direct.mcp_command(self.state, self.settings)
        args = shlex.split(expected)
        self.assertEqual(args[0], "/usr/bin/env")
        self.assertEqual(args[1], "PYTHON_BIN=" + self.settings["python"])
        self.assertEqual(args[3], str(ROOT / "scripts/start_mcp.sh"))
        self.assertEqual(args[5], str(self.state / "route/config.json"))
        self.assertNotIn(direct.KEY_NAME, expected)
        self.assertNotIn(direct.BEARER_NAME, expected)

    def test_environment_value_check_without_values(self):
        for value in (None, "short", "a" * 16 + "\n", "é" * 20):
            self.assertFalse(direct.valid_bearer(value))
        self.assertTrue(direct.valid_bearer("fixture-only-long-bearer"))

    def test_run_missing_credentials_stops_before_execution(self):
        self.prepared()
        with patch.object(direct, "require_interactive"), patch.object(direct, "sdk_ready", return_value=True), \
             patch.object(direct, "tunnel_binary", return_value="/fixture/tunnel-client"), \
             patch.dict(os.environ, {}, clear=True), patch.object(direct.os, "execve") as execute:
            with self.assertRaisesRegex(direct.SetupError, direct.BEARER_NAME):
                direct.run(self.state)
            execute.assert_not_called()

    def test_run_reviews_profile_then_executes_official_foreground_shape(self):
        self.prepared()
        values = {direct.BEARER_NAME: "fixture-only-long-bearer", direct.KEY_NAME: "fixture-only-key"}
        with patch.object(direct, "require_interactive"), patch.object(direct, "sdk_ready", return_value=True), \
             patch.object(direct, "tunnel_binary", return_value="/fixture/tunnel client"), \
             patch.object(direct, "port_available", return_value=True), patch.dict(os.environ, values), \
             patch("builtins.input", side_effect=["direct-fixture", "RUN"]), patch.object(direct.os, "execve") as execute:
            direct.run(self.state)
            args = execute.call_args.args
            self.assertEqual(args[:2], ("/fixture/tunnel client", ["/fixture/tunnel client", "run", "--profile", "direct-fixture"]))
            self.assertEqual(args[2][direct.BEARER_NAME], values[direct.BEARER_NAME])
            self.assertEqual(args[2][direct.KEY_NAME], values[direct.KEY_NAME])
        for value in values.values():
            self.assertNotIn(value, self.output.getvalue())
            for file in (self.state / "route").iterdir():
                self.assertNotIn(value, file.read_text())

    def test_expired_route_stops_and_is_not_renewed(self):
        self.config["not_before"] = 1
        self.config["expires_at"] = 2
        self.prepared()
        with patch.object(direct, "require_interactive"), patch.object(direct.os, "execve") as execute:
            with self.assertRaisesRegex(direct.SetupError, "not active"):
                direct.run(self.state)
            execute.assert_not_called()
        self.assertEqual(direct.read_setup(self.state)[0]["expires_at"], 2)

    def test_occupied_loopback_port_is_not_claimed_ready(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.settings["http_port"] = sock.getsockname()[1]
            self.prepared()
            direct.status(self.state)
        self.assertIn("identity/readiness not established", self.output.getvalue())

    def test_read_only_status_does_not_create_state(self):
        direct.status(self.state)
        self.assertFalse(self.state.exists())
        self.assertIn("not set up", self.output.getvalue())

    def test_mcp_shell_strips_tunnel_key_before_interpreter(self):
        fake = Path(self.temp.name) / "fake python"
        fake.write_text("#!/bin/sh\n[ -z \"${CONTROL_PLANE_API_KEY+x}\" ] || exit 45\n"
                        "[ \"$DOTS_BRIDGE_HTTP_BEARER\" = 'fixture-only-bearer' ] || exit 46\nexit 0\n")
        fake.chmod(0o700)
        result = subprocess.run([str(ROOT / "scripts/start_mcp.sh"), "--config", "/fixture/config"],
            env={**os.environ, "PYTHON_BIN": str(fake), direct.BEARER_NAME: "fixture-only-bearer",
                 direct.KEY_NAME: "fixture-only-key"}, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")

    def test_check_reports_pinned_codex_without_printing_unknown_output(self):
        with patch.object(direct, "sdk_ready", return_value=True), \
             patch.object(direct, "tunnel_binary", return_value="/fixture/tunnel-client"), \
             patch.object(direct, "existing_codex", return_value="/fixture/codex"), \
             patch.object(direct, "codex_version", return_value="fixture-untrusted-private-output"):
            direct.check(self.state)
        self.assertIn("different/unverified version", self.output.getvalue())
        self.assertNotIn("fixture-untrusted-private-output", self.output.getvalue())
        self.assertFalse(self.state.exists())

    def test_parent_components_and_top_level_bundle_children_rejected(self):
        for path in (ROOT.parent / "runtime", ROOT.parent / "anything/../direct_bridge/private"):
            with self.assertRaises(direct.SetupError):
                direct.check_state_path(path)

    def test_huge_timestamp_fails_cleanly(self):
        self.config["expires_at"] = 1e200
        self.prepared()
        with self.assertRaises(direct.SetupError):
            direct.status(self.state)

    def codex_patches(self, *, version="codex-cli 0.159.2", inputs=None, execute_error=None):
        fake = Path(self.temp.name) / "codex cli"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o700)
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(direct, "require_interactive"))
        stack.enter_context(patch.object(direct, "port_available", return_value=False))
        stack.enter_context(patch.dict(os.environ, {"CODEX_BIN": str(fake),
            direct.BEARER_NAME: "fixture-only-long-bearer", direct.KEY_NAME: "fixture-tunnel-key",
            "OPENAI_API_KEY": "fixture-unrelated-key"}))
        stack.enter_context(patch.object(direct.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, version + "\n")))
        stack.enter_context(patch("builtins.input", side_effect=inputs or [self.temp.name, "CODEX"]))
        execute = stack.enter_context(patch.object(direct.os, "execve", side_effect=execute_error))
        return stack, execute

    def test_codex_session_has_scoped_provider_no_secret_argv_or_control_key(self):
        self.prepared()
        stack, execute = self.codex_patches()
        with stack:
            direct.codex(self.state)
        binary, argv, env = execute.call_args.args
        self.assertEqual(argv[1:4], ["--no-daemon", "--sandbox", "read-only"])
        provider = json.loads(next(value.split('=', 1)[1] for value in argv if value.startswith('model_provider=')))
        self.assertTrue(provider.startswith('dots_direct_'))
        self.assertNotEqual(provider, 'dots_direct')
        prefix = 'model_providers.' + provider
        self.assertIn(prefix + '.request_max_retries=0', argv)
        self.assertIn(prefix + '.stream_max_retries=0', argv)
        self.assertIn(prefix + '.requires_openai_auth=false', argv)
        self.assertEqual(argv[-4:], ["-m", "gpt-6-astra", "-C", self.temp.name])
        self.assertEqual(env[direct.BEARER_NAME], "fixture-only-long-bearer")
        self.assertNotIn(direct.KEY_NAME, env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("fixture-only-long-bearer", " ".join(argv))
        self.assertNotIn("fixture-only-long-bearer", self.output.getvalue())
        self.assertEqual((self.state / "route/codex-started.json").stat().st_mode & 0o777, 0o600)

    def test_codex_provider_is_route_unique(self):
        first = direct.codex_argv("/fixture/codex", self.settings, self.temp.name, "route-a")
        second = direct.codex_argv("/fixture/codex", self.settings, self.temp.name, "route-b")
        provider = lambda args: next(item for item in args if item.startswith("model_provider="))
        self.assertNotEqual(provider(first), provider(second))
        self.assertNotIn('model_provider="dots_direct"', first)

    def test_codex_wrong_version_blocks_before_marker(self):
        self.prepared()
        stack, execute = self.codex_patches(version="codex-cli 0.0.0")
        with stack:
            with self.assertRaisesRegex(direct.SetupError, "requires codex-cli"):
                direct.codex(self.state)
            execute.assert_not_called()
        self.assertFalse((self.state / "route/codex-started.json").exists())

    def test_codex_cancel_does_not_reserve_session(self):
        self.prepared()
        stack, execute = self.codex_patches(inputs=[self.temp.name, ""])
        with stack:
            with self.assertRaises(direct.SetupError):
                direct.codex(self.state)
            execute.assert_not_called()
        self.assertFalse((self.state / "route/codex-started.json").exists())

    def test_codex_second_session_blocked(self):
        self.prepared()
        stack, execute = self.codex_patches()
        with stack:
            direct.codex(self.state)
            with self.assertRaisesRegex(direct.SetupError, "already started"):
                direct.codex(self.state)
            self.assertEqual(execute.call_count, 1)

    def test_codex_exec_failure_removes_only_unexecuted_marker(self):
        self.prepared()
        stack, execute = self.codex_patches(execute_error=OSError("fixture-sensitive-error"))
        with stack:
            with self.assertRaisesRegex(direct.SetupError, "could not start") as caught:
                direct.codex(self.state)
        self.assertNotIn("fixture-sensitive", str(caught.exception))
        self.assertFalse((self.state / "route/codex-started.json").exists())
        self.assertTrue((self.state / "route/config.json").exists())

    def test_codex_noninteractive_denied(self):
        with patch.object(direct.sys.stdin, "isatty", return_value=False):
            with self.assertRaises(direct.SetupError):
                direct.codex(self.state)
        self.assertFalse(self.state.exists())

    def test_profile_init_template_has_unique_profile_and_no_secrets(self):
        direct.show_connection(self.state, self.config, self.settings)
        output = self.output.getvalue()
        self.assertIn("tunnel-client init --sample sample_mcp_stdio_local --profile dots-direct-", output)
        self.assertIn("REPLACE_WITH_APPROVED_EXISTING_TUNNEL_ID", output)
        self.assertNotIn(direct.BEARER_NAME, output)
        self.assertNotIn(direct.KEY_NAME, output)


    def test_global_shell_entrypoint_help_from_path_with_spaces(self):
        # Source extraction itself is tested by release verification; copy launcher UI here.
        bundle = Path(self.temp.name) / "bundle with spaces"
        (bundle / "direct_bridge/scripts").mkdir(parents=True)
        command = ROOT.parent / "DIRECT.command"
        if not command.exists():
            self.skipTest("Root launcher is not part of this standalone test layout")
        import shutil
        shutil.copy2(command, bundle / "DIRECT.command")
        shutil.copy2(ROOT / "global_launcher.py", bundle / "direct_bridge/global_launcher.py")
        shutil.copytree(ROOT.parent / "dots_lite", bundle / "dots_lite", ignore=shutil.ignore_patterns("__pycache__"))
        result = subprocess.run([str(bundle / "DIRECT.command"), "help", "--state-dir", str(self.state)],
            capture_output=True, text=True, timeout=10, env={**os.environ, "PYTHON_BIN": sys.executable})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DIRECT：单会话 / 全局", result.stdout)
        self.assertIn("session", result.stdout)
        self.assertIn("global", result.stdout)
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
