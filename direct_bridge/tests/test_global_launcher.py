"""Offline lifecycle acceptance: real owned OS processes and loopback; no APIs."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT.parent), str(ROOT)]
from direct_bridge import global_launcher as g
from direct_bridge import global_config as tx
from direct_bridge.global_credentials import Credentials
from dots_lite.private_io import save, private_dir


class UserInterface:
    def __init__(self, approve=True):
        self.approve = approve
        self.prompts = []
    def confirm(self, text, word='YES'):
        self.prompts.append((text, word))
        return self.approve
    def say(self, text):
        pass


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='global launcher spaces ')
        self.state = private_dir(Path(self.temp.name) / 'private state', create=True)
        self.home = private_dir(Path(self.temp.name) / 'codex home', create=True)
        self.original = b'# existing config\nmodel = "original"\nweb_search = "disabled"\n[sandbox_workspace_write]\nnetwork_access = false\n'
        (self.home / 'config.toml').write_bytes(self.original)
        (self.home / 'config.toml').chmod(0o600)
        self.credential = Credentials('fixture_tunnel_key', 'fixture_local_bearer_123456789')
        (self.state / 'private.env').write_text('CONTROL_PLANE_API_KEY="fixture_tunnel_key"\nDOTS_BRIDGE_HTTP_BEARER="fixture_local_bearer_123456789"\n')
        (self.state / 'private.env').chmod(0o600)
        profile_dir = private_dir(self.state / 'tunnel-profile', create=True)
        (profile_dir / 'direct-global.yaml').write_text('synthetic fixture only\n')
        (profile_dir / 'direct-global.yaml').chmod(0o600)
        executable = Path(self.temp.name) / 'fixture tunnel'
        executable.write_text('#!' + sys.executable + '\nimport runpy\nrunpy.run_path(' + repr(str(ROOT / 'tests/global_service_fixture.py')) + ', run_name="__main__")\n')
        executable.chmod(0o700)
        self.settings = {'contract': g.CONTRACT, 'python': sys.executable, 'http_port': free_port(),
            'admin_port': free_port(), 'tunnel_binary': str(executable), 'profile_dir': str(profile_dir),
            'profile': 'direct-global', 'profile_sha256': hashlib.sha256((profile_dir / 'direct-global.yaml').read_bytes()).hexdigest(),
            'codex_home': str(self.home), 'config_id': 'a' * 32, 'bundle_root': str(ROOT.parent), 'package_sha256': 'b' * 64, 'credential_dir': str(self.state)}
        save(self.state / 'settings.json', self.settings)
        private_dir(self.state / 'bridge', create=True)
        save(self.state / 'bridge/config.json', {'config_id': 'a' * 32,
             'allowed_pairs': [{'model': 'gpt-6-astra', 'reasoning_effort': 'xhigh'}]})
        self.ui = UserInterface()
        self.launcher = g.Launcher(self.state, ui=self.ui)

    def tearDown(self):
        try:
            self.launcher.stop()
        except Exception:
            pass
        self.temp.cleanup()

    def test_real_start_stop_restart_retains_state_and_global_config(self):
        durable = self.state / 'bridge/uncertain-action.fixture'
        durable.write_bytes(b'unknown execution outcome must survive')
        result = self.launcher.start(self.credential, timeout=10)
        self.assertEqual(result['stage'], 'RUNNING')
        first = self.launcher.current()
        owners = self.launcher.records(first)
        self.assertTrue(all(g.owned(owners[role]) for role in ('owner', 'tunnel', 'bridge')))
        self.assertIn(b'dots2codex_direct_global', (self.home / 'config.toml').read_bytes())
        self.assertEqual(len(self.ui.prompts), 1)
        repeated = self.launcher.start(self.credential, timeout=10)
        self.assertTrue(repeated['already_running'])
        self.assertEqual(first, self.launcher.current())
        self.assertEqual(len(self.ui.prompts), 1)
        self.assertTrue(self.launcher.status()['ready'])
        self.launcher.stop()
        self.assertEqual((self.home / 'config.toml').read_bytes(), self.original)
        self.assertFalse(any(g.alive(record['pid']) for record in owners.values()))
        self.assertEqual(durable.read_bytes(), b'unknown execution outcome must survive')
        self.launcher.start(self.credential, timeout=10)
        self.assertNotEqual(first, self.launcher.current())
        self.assertEqual(len(self.ui.prompts), 1)  # Existing scoped config permission.
        self.assertEqual(durable.read_bytes(), b'unknown execution outcome must survive')

    def test_cancel_after_readiness_stops_owned_services_and_never_applies(self):
        self.ui.approve = False
        with self.assertRaises(g.Cancelled):
            self.launcher.start(self.credential, timeout=10)
        self.assertEqual((self.home / 'config.toml').read_bytes(), self.original)
        self.assertFalse(any(g.alive(r['pid']) for r in self.launcher.records(self.launcher.current()).values()))

    def test_occupied_port_does_not_start_or_touch_configuration(self):
        with socket.socket() as guard:
            guard.bind(('127.0.0.1', self.settings['http_port']))
            with self.assertRaisesRegex(g.ProtocolError, 'service_port_occupied'):
                self.launcher.start(self.credential, timeout=.3)
        self.assertIsNone(self.launcher.current())
        self.assertEqual((self.home / 'config.toml').read_bytes(), self.original)

    def test_timeout_rolls_back_partially_started_processes(self):
        with patch.object(self.launcher.ports, 'ready', side_effect=g.ProtocolError('not_ready')):
            with self.assertRaisesRegex(g.ProtocolError, 'readiness_timeout'):
                self.launcher.start(self.credential, timeout=.5)
        self.assertFalse(any(g.alive(r['pid']) for r in self.launcher.records(self.launcher.current()).values()))
        self.assertEqual((self.home / 'config.toml').read_bytes(), self.original)

    def test_restore_does_not_require_credentials_or_google(self):
        self.launcher.start(self.credential, timeout=10)
        (self.state / 'private.env').unlink()
        with patch('direct_bridge.global_credentials.load_credentials', side_effect=AssertionError('must not load')):
            self.launcher.restore()
        self.assertEqual((self.home / 'config.toml').read_bytes(), self.original)

    def test_stop_refuses_reused_pid_without_signalling(self):
        record = {'pid': os.getpid(), 'process_identity': 'not this process'}
        with patch.object(g.os, 'kill') as kill:
            with self.assertRaisesRegex(g.ProtocolError, 'unowned'):
                g.stop_owned(record, timeout=.01)
        kill.assert_not_called()

    def test_start_output_cannot_include_tunnel_or_bearer_logs(self):
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
            self.launcher.start(self.credential, timeout=10)
        self.assertNotIn(self.credential.control_plane_api_key, capture.getvalue())
        self.assertNotIn(self.credential.http_bearer, capture.getvalue())
        self.assertFalse(list(self.state.rglob('*.log')))

    def test_partial_owned_service_failure_recovers_same_state_on_start(self):
        self.launcher.start(self.credential, timeout=10)
        first = self.launcher.current()
        durable = self.state / 'bridge/unknown.fixture'
        durable.write_bytes(b'preserve')
        record = self.launcher.records(first)['bridge']
        os.kill(record['pid'], signal.SIGTERM)
        until = time.monotonic() + 3
        while g.alive(record['pid']) and time.monotonic() < until:
            time.sleep(.05)
        self.launcher.start(self.credential, timeout=10)
        self.assertNotEqual(first, self.launcher.current())
        self.assertEqual(durable.read_bytes(), b'preserve')

    def test_same_path_package_update_restarts_only_transport(self):
        self.launcher.start(self.credential, timeout=10)
        first = self.launcher.current()
        self.launcher.start(self.credential, timeout=10, package_sha256='c' * 64)
        self.assertNotEqual(first, self.launcher.current())
        self.assertEqual(g.load_settings(self.state)['package_sha256'], 'c' * 64)
        self.assertEqual(len(self.ui.prompts), 1)

    def test_stop_cancels_start_while_apply_prompt_is_open(self):
        other = g.Launcher(self.state, ui=UserInterface())
        def stop_at_prompt(*args):
            other.stop()
            return True
        self.ui.confirm = stop_at_prompt
        with self.assertRaisesRegex(g.ProtocolError, 'cancelled_by_stop'):
            self.launcher.start(self.credential, timeout=10)
        self.assertEqual((self.home / 'config.toml').read_bytes(), self.original)
        self.assertFalse(any(g.alive(r['pid']) for r in self.launcher.records(self.launcher.current()).values()))

    def test_mcp_command_quotes_spaces_without_credentials(self):
        import shlex
        command = g.mcp_command(self.state, "/tmp/Python's environment/python", 18765)
        argv = shlex.split(command)
        self.assertEqual(argv[1], "PYTHON_BIN=/tmp/Python's environment/python")
        self.assertEqual(argv[-3], str(self.state / 'bridge/config.json'))
        self.assertNotIn('CONTROL_PLANE_API_KEY', command)

    def test_private_venv_symlink_reexec_keeps_its_environment(self):
        selected = self.state / 'venv python'
        selected.symlink_to(sys.executable)
        self.settings['python'] = str(selected)
        save(self.state / 'settings.json', self.settings)
        with patch.object(g.os, 'execve', side_effect=KeyboardInterrupt) as execute, \
             contextlib.redirect_stdout(io.StringIO()):
            g.main(['status', '--state-dir', str(self.state)])
        self.assertEqual(execute.call_args.args[0], str(selected))
        self.assertEqual(execute.call_args.args[1][-3:], ['status', '--state-dir', str(self.state)])

    def test_health_requires_expected_owner_identity(self):
        with patch.object(g, 'loopback_get', return_value=json.dumps({'mode': 'global', 'listener_ready': True,
             'config_id': 'wrong', 'instance_id': 'wrong'}).encode()):
            with self.assertRaisesRegex(g.ProtocolError, 'identity_mismatch'):
                g.Ports().ready(self.settings, 'a' * 32, self.credential.http_bearer)

    def test_profile_changes_are_not_silently_adopted(self):
        (Path(self.settings['profile_dir']) / 'direct-global.yaml').write_text('changed')
        with self.assertRaisesRegex(g.ProtocolError, 'profile_changed'):
            self.launcher.start(self.credential)


class FirstUseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='first use spaces ')
        self.root = Path(self.temp.name)
        self.state = self.root / 'new global state'
        self.home = private_dir(self.root / 'codex', create=True)
        self.credentials = private_dir(self.root / 'existing credentials', create=True)
        self.bundle = Credentials('fixture_key', 'fixture_bearer_123456789')
        self.ui = UserInterface()
        self.ui.text = lambda prompt, default='': next(self.answers)
        self.answers = iter(['tunnel_' + '1' * 32, str(self.home), str(self.credentials)])
    def tearDown(self):
        self.temp.cleanup()
    def test_first_use_persists_separate_credential_dir_and_no_secret_profile_args(self):
        calls = []
        def initialize(argv, **kwargs):
            calls.append((argv, kwargs))
            directory = Path(argv[argv.index('--profile-dir') + 1])
            (directory / 'direct-global.yaml').write_text('synthetic reviewed profile')
            return subprocess.CompletedProcess(argv, 0)
        with patch.object(g.sys.stdin, 'isatty', return_value=True), \
             patch.object(g, 'verify_package', return_value='b' * 64), \
             patch.object(g, 'existing_tunnel_binary', return_value='/fixture/tunnel-client'), \
             patch.object(g, 'port_available', return_value=True), \
             patch.object(g, 'choose_python', return_value=sys.executable), \
             patch('direct_bridge.global_credentials.prepare_credentials', return_value=self.bundle) as prepare, \
             patch.object(g.subprocess, 'run', side_effect=initialize):
            result = g.setup(self.state, self.ui)
        settings = g.load_settings(self.state)
        self.assertEqual(settings['credential_dir'], str(self.credentials))
        prepare.assert_called_once_with(self.credentials, interactive=True)
        self.assertIs(result, self.bundle)
        self.assertNotIn('--force', calls[0][0])
        self.assertNotIn(self.bundle.control_plane_api_key, repr(calls))
        self.assertNotIn(self.bundle.http_bearer, repr(calls))
        self.assertEqual(calls[0][1]['stdout'], subprocess.DEVNULL)
        config = json.loads((self.state / 'bridge/config.json').read_text())
        self.assertIsNone(config['expires_at'])
        self.assertEqual(config['config_id'], settings['config_id'])
        self.assertEqual([word for _, word in self.ui.prompts], ['SETUP', 'PROFILE'])
    def test_cancel_before_setup_leaves_no_new_state(self):
        self.ui.approve = False
        with patch.object(g.sys.stdin, 'isatty', return_value=True), \
             patch.object(g, 'verify_package', return_value='b' * 64), \
             patch.object(g, 'existing_tunnel_binary', return_value='/fixture/tunnel-client'), \
             patch.object(g, 'port_available', return_value=True):
            with self.assertRaises(g.Cancelled):
                g.setup(self.state, self.ui)
        self.assertFalse(self.state.exists())
    def test_known_direct_profile_only_extracts_id(self):
        path = self.root / 'dots-direct-existing.yaml'
        path.write_text('control_plane:\n  tunnel_id: "tunnel_' + '2' * 32 + '"\n  api_key: "private-value-never-echo"\n')
        self.assertEqual(g.reviewed_tunnel_id(str(path)), 'tunnel_' + '2' * 32)
        path.rename(self.root / 'Lean.yaml')
        with self.assertRaises(g.ProtocolError):
            g.reviewed_tunnel_id(str(self.root / 'Lean.yaml'))


class ProcessIdentityTests(unittest.TestCase):
    def identity(self, stat='S', uid='501', started='Fri Oct 2 12:34:56 2026', command='/Applications/tool --profile direct-global'):
        result = subprocess.CompletedProcess([], 0, stdout=f'{uid} {started} {stat} {command}\n', stderr='')
        with patch.object(g.sys, 'platform', 'darwin'), patch.object(g.subprocess, 'run', return_value=result) as run:
            value = g.process_identity(12345)
        self.assertEqual(run.call_args.args[0], ['/bin/ps', '-p', '12345', '-o', 'uid=', '-o', 'lstart=', '-o', 'stat=', '-o', 'command='])
        return value

    def test_darwin_identity_ignores_transient_scheduler_and_foreground_flags(self):
        expected = self.identity('S')
        self.assertIsNotNone(expected)
        for state in ('R', 'S+', 'Ss', 'Rs+', 'T', 'U'):
            with self.subTest(state=state):
                self.assertEqual(self.identity(state), expected)

    def test_darwin_identity_rejects_zombie_states(self):
        for state in ('Z', 'Z+', 'Zs'):
            with self.subTest(state=state):
                self.assertIsNone(self.identity(state))

    def test_darwin_identity_changes_for_uid_start_time_or_command(self):
        expected = self.identity()
        self.assertNotEqual(self.identity(uid='502'), expected)
        self.assertNotEqual(self.identity(started='Fri Oct 2 12:34:57 2026'), expected)
        self.assertNotEqual(self.identity(command='/Applications/other --profile direct-global'), expected)

    def test_darwin_identity_rejects_incomplete_process_record(self):
        result = subprocess.CompletedProcess([], 0, stdout='501 Fri Oct 2 12:34:56 2026 S\n', stderr='')
        with patch.object(g.sys, 'platform', 'darwin'), patch.object(g.subprocess, 'run', return_value=result):
            self.assertIsNone(g.process_identity(12345))


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.file = self.root / 'file.py'
        self.file.write_text('pass\n')
        self.file.chmod(0o644)
        self.files = {'file.py': {'bytes': self.file.stat().st_size, 'sha256': hashlib.sha256(self.file.read_bytes()).hexdigest(), 'mode': '0644'}}
        self.manifest = self.root / 'GLOBAL_DIRECT_PACKAGE_MANIFEST.json'
        self.write_manifest()
    def write_manifest(self):
        digest = hashlib.sha256(json.dumps(self.files, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        self.manifest.write_text(json.dumps({'contract': 'dots2codex-global-direct-package/1', 'files': self.files, 'source_tree_sha256': digest}))
    def tearDown(self):
        self.temp.cleanup()
    def test_package_matches_and_pycache_is_ignored(self):
        (self.root / '__pycache__').mkdir()
        (self.root / '__pycache__/file.pyc').write_bytes(b'fixture')
        self.assertTrue(g.verify_package(self.root))
    def test_extra_file_rejected(self):
        (self.root / 'injected.py').write_text('pass')
        with self.assertRaises(g.ProtocolError):
            g.verify_package(self.root)
    def test_declared_entry_removal_rejected(self):
        self.files = {}
        self.write_manifest()
        with self.assertRaises(g.ProtocolError):
            g.verify_package(self.root)
    def test_permission_change_rejected(self):
        self.file.chmod(0o666)
        with self.assertRaises(g.ProtocolError):
            g.verify_package(self.root)
    def test_symlink_rejected(self):
        (self.root / 'link').symlink_to(self.file)
        with self.assertRaises(g.ProtocolError):
            g.verify_package(self.root)
    def test_digest_tampering_rejected(self):
        data = json.loads(self.manifest.read_text())
        data['source_tree_sha256'] = '0' * 64
        self.manifest.write_text(json.dumps(data))
        with self.assertRaises(g.ProtocolError):
            g.verify_package(self.root)


if __name__ == '__main__':
    unittest.main()
