"""Offline mode/configuration/lifecycle coverage. Fake CLI and local fixtures only."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT.parent), str(ROOT)]
from direct_bridge import global_launcher as g
from direct_bridge import session_client as sc
from direct_bridge.tests import test_global_launcher as fixtures
from dots_lite.private_io import save, read, private_dir


class SessionLifecycleTests(unittest.TestCase):
    setUp = fixtures.LifecycleTests.setUp
    tearDown = fixtures.LifecycleTests.tearDown

    def snapshot_home(self):
        return {p.relative_to(self.home).as_posix(): (p.read_bytes(), p.stat().st_mode & 0o777)
                for p in self.home.rglob('*') if p.is_file()}

    def no_transactions(self):
        stack = contextlib.ExitStack()
        for name in ('apply', 'restore', 'reconcile', 'preview'):
            stack.enter_context(patch.object(self.launcher, '_apply_config', side_effect=AssertionError('global write')) if name == 'apply'
                                else patch.object(self.launcher, '_restore_config', side_effect=AssertionError('global restore')) if name == 'restore'
                                else patch.object(self.launcher.tx, name, side_effect=AssertionError('global transaction')))
        return stack

    def fake_cli(self, code=0):
        capture = Path(self.temp.name) / 'cli-capture.json'
        binary = Path(self.temp.name) / 'fixture codex'
        binary.write_text('#!' + sys.executable + '\nimport os,sys,json\n'
                          'if "--version" in sys.argv:\n print("codex-cli 0.159.2")\n sys.exit(0)\n'
                          'with open(' + repr(str(capture)) + ', "w") as f:\n'
                          ' json.dump({"argv":sys.argv,"env":dict(os.environ),"cwd":os.getcwd()}, f)\n'
                          'sys.exit(' + str(code) + ')\n')
        binary.chmod(0o700)
        return binary, Path(self.temp.name), capture

    def test_session_start_stop_never_changes_config_bytes_permissions_or_transactions(self):
        (self.home / 'config.toml').chmod(0o644)
        before = self.snapshot_home()
        with self.no_transactions():
            result = self.launcher.start(self.credential, mode='session', timeout=10)
            self.assertEqual(result['configuration_scope'], 'session')
            self.assertEqual(self.snapshot_home(), before)
            self.assertFalse((self.state / 'config-consent.json').exists())
            self.assertFalse((self.state / 'config-transactions').exists())
        self.launcher.stop()
        self.assertEqual(self.snapshot_home(), before)
        self.launcher.start(self.credential, mode='session', timeout=10)
        self.assertEqual(self.snapshot_home(), before)

    def test_session_with_no_config_file_does_not_create_one(self):
        (self.home / 'config.toml').unlink()
        with self.no_transactions():
            self.launcher.start(self.credential, mode='session', timeout=10)
        self.assertEqual(self.snapshot_home(), {})

    def test_session_reuses_global_without_changing_any_transaction_or_service_owner(self):
        self.launcher.start(self.credential, timeout=10)
        runtime = self.launcher.current()
        owners = self.launcher.records(runtime)
        before = self.snapshot_home()
        transactions = {p.name: p.read_bytes() for p in (self.state / 'config-transactions').iterdir()}
        with self.no_transactions(), patch.object(self.launcher, '_stop_services', side_effect=AssertionError('stop')):
            result = self.launcher.start(self.credential, mode='session', timeout=10)
        self.assertTrue(result['already_running'])
        self.assertEqual(self.launcher.records(runtime), owners)
        self.assertEqual(self.snapshot_home(), before)
        self.assertEqual({p.name: p.read_bytes() for p in (self.state / 'config-transactions').iterdir()}, transactions)

    def test_session_reuses_session_service(self):
        self.launcher.start(self.credential, mode='session', timeout=10)
        runtime = self.launcher.current()
        result = self.launcher.start(self.credential, mode='session', timeout=10)
        self.assertTrue(result['already_running'])
        self.assertEqual(runtime, self.launcher.current())
        self.assertEqual(self.snapshot_home()['config.toml'][0], self.original)

    def test_session_live_package_conflict_preserves_existing_service_and_settings(self):
        self.launcher.start(self.credential, timeout=10)
        runtime = self.launcher.current()
        before = (self.state / 'settings.json').read_bytes(), self.snapshot_home()
        with self.no_transactions(), patch.object(self.launcher, '_stop_services', side_effect=AssertionError('stop')):
            with self.assertRaisesRegex(g.ProtocolError, 'session_service_conflict'):
                self.launcher.start(self.credential, mode='session', package_sha256='c' * 64)
        self.assertEqual(((self.state / 'settings.json').read_bytes(), self.snapshot_home()), before)
        self.assertEqual(runtime, self.launcher.current())
        self.assertTrue(self.launcher.status()['ready'])

    def test_session_live_unhealthy_conflict_never_repairs_or_restores(self):
        self.launcher.start(self.credential, timeout=10)
        before = self.snapshot_home()
        with self.no_transactions(), patch.object(self.launcher.ports, 'ready', side_effect=g.ProtocolError('unhealthy')), \
             patch.object(self.launcher, '_stop_services', side_effect=AssertionError('stop')):
            with self.assertRaisesRegex(g.ProtocolError, 'session_service_conflict'):
                self.launcher.start(self.credential, mode='session', timeout=.1)
        self.assertEqual(self.snapshot_home(), before)

    def test_session_fresh_failure_does_not_restore_preexisting_global_transaction(self):
        self.launcher.start(self.credential, timeout=10)
        before = self.snapshot_home()
        self.launcher._stop_services(self.launcher.current())  # Leave global transaction intact.
        with self.no_transactions(), patch.object(self.launcher.ports, 'ready', side_effect=g.ProtocolError('unhealthy')):
            with self.assertRaisesRegex(g.ProtocolError, 'readiness_timeout'):
                self.launcher.start(self.credential, mode='session', timeout=.3)
        self.assertEqual(self.snapshot_home(), before)
        self.assertTrue(self.launcher.tx.status(self.state)['active_transaction'])

    def test_session_occupied_port_does_not_change_config(self):
        import socket
        with socket.socket() as guard:
            guard.bind(('127.0.0.1', self.settings['http_port']))
            with self.no_transactions(), self.assertRaisesRegex(g.ProtocolError, 'service_port_occupied'):
                self.launcher.start(self.credential, mode='session', timeout=.1)
        self.assertIsNone(self.launcher.current())
        self.assertEqual(self.snapshot_home()['config.toml'][0], self.original)

    def test_session_timeout_stops_only_created_service_never_changes_config(self):
        with self.no_transactions(), patch.object(self.launcher.ports, 'ready', side_effect=g.ProtocolError('not_ready')):
            with self.assertRaisesRegex(g.ProtocolError, 'readiness_timeout'):
                self.launcher.start(self.credential, mode='session', timeout=.3)
        self.assertFalse(any(g.alive(r['pid']) for r in self.launcher.records(self.launcher.current()).values()))
        self.assertEqual(self.snapshot_home()['config.toml'][0], self.original)

    def test_global_can_promote_healthy_session_service_only_after_apply_consent(self):
        self.launcher.start(self.credential, mode='session', timeout=10)
        runtime = self.launcher.current()
        self.ui.approve = False
        with self.assertRaises(g.Cancelled):
            self.launcher.start(self.credential, timeout=10)
        self.assertEqual(self.snapshot_home()['config.toml'][0], self.original)
        self.assertTrue(self.launcher.status()['ready'])
        self.ui.approve = True
        result = self.launcher.start(self.credential, timeout=10)
        self.assertTrue(result['already_running'])
        self.assertEqual(runtime, self.launcher.current())
        self.assertEqual([word for _, word in self.ui.prompts], ['APPLY', 'APPLY'])
        self.launcher.stop()
        self.assertEqual(self.snapshot_home()['config.toml'][0], self.original)

    def test_active_session_reservation_blocks_implicit_global_restart(self):
        self.launcher.start(self.credential, mode='session', session_id='1' * 32, timeout=10)
        runtime = self.launcher.current()
        with patch.object(self.launcher, '_stop_services', side_effect=AssertionError('stop')):
            with self.assertRaisesRegex(g.ProtocolError, 'session_clients_active'):
                self.launcher.start(self.credential, package_sha256='c' * 64)
        self.assertEqual(runtime, self.launcher.current())
        self.assertEqual(len(self.launcher.active_session_clients(runtime)), 1)

    def test_stale_and_reused_session_pids_do_not_lock_out_restart(self):
        self.launcher.start(self.credential, mode='session', session_id='1' * 32, timeout=10)
        runtime = self.launcher.current()
        path = runtime / 'sessions' / ('1' * 32 + '.json')
        value = read(path)
        value['launcher']['process_identity'] = 'stale or reused PID'
        value['client'] = {'pid': 999999999, 'process_identity': 'dead'}
        save(path, value)
        self.assertEqual(self.launcher.active_session_clients(runtime), [])
        self.launcher.start(self.credential, package_sha256='c' * 64, timeout=10)
        self.assertNotEqual(runtime, self.launcher.current())

    def test_live_cli_survives_launcher_exit_record_and_blocks_implicit_restart(self):
        self.launcher.start(self.credential, mode='session', timeout=10)
        runtime = self.launcher.current()
        directory = private_dir(runtime / 'sessions', create=True)
        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
        try:
            save(directory / ('2' * 32 + '.json'), {'contract': g.CONTRACT, 'run_id': runtime.name,
                 'session_id': '2' * 32, 'stage': 'RUNNING', 'client': g.owner_record('client', runtime.name, child.pid)})
            with self.assertRaisesRegex(g.ProtocolError, 'session_clients_active'):
                self.launcher.start(self.credential, package_sha256='c' * 64)
            self.launcher.stop()
            self.assertIsNone(child.poll())  # Explicit Stop never signals client applications.
        finally:
            child.terminate(); child.wait(timeout=5)

    def test_multiple_session_records_and_status(self):
        for ident in ('1' * 32, '2' * 32):
            self.launcher.start(self.credential, mode='session', session_id=ident, timeout=10)
        self.assertEqual(self.launcher.status()['active_session_clients'], 2)

    def test_real_fake_cli_exit_code_env_and_shared_service_persistence(self):
        binary, project, capture = self.fake_cli(7)
        before = self.snapshot_home()
        with self.no_transactions(), patch.dict(os.environ, {'TERM': 'xterm-256color',
             'CODEX_HOME': '/wrong/home', 'CONTROL_PLANE_API_KEY': 'must-not-pass', 'OPENAI_API_KEY': 'must-not-pass'}):
            outcome = self.launcher.session(self.credential, (str(binary), project), timeout=10)
        self.assertEqual(outcome['exit_code'], 7)
        self.assertEqual(self.snapshot_home(), before)
        actual = json.loads(capture.read_text())
        self.assertEqual(actual['env']['CODEX_HOME'], str(self.home))
        self.assertEqual(actual['env']['TERM'], 'xterm-256color')
        self.assertEqual(actual['env'][sc.BEARER_NAME], self.credential.http_bearer)
        self.assertNotIn('CONTROL_PLANE_API_KEY', actual['env'])
        self.assertNotIn('OPENAI_API_KEY', actual['env'])
        self.assertNotIn(self.credential.http_bearer, repr(actual['argv']))
        self.assertEqual(actual['cwd'], str(project))
        self.assertTrue(self.launcher.status()['ready'])
        self.assertEqual(self.launcher.active_session_clients(self.launcher.current()), [])

    def test_reused_global_service_cli_failure_does_not_restore_or_stop(self):
        self.launcher.start(self.credential, timeout=10)
        runtime = self.launcher.current()
        before = self.snapshot_home()
        with self.no_transactions(), patch.object(g.subprocess, 'Popen', side_effect=OSError('fixture launch failure')):
            with self.assertRaises(OSError):
                self.launcher.session(self.credential, ('/fixture/codex', Path(self.temp.name)))
        self.assertEqual(self.snapshot_home(), before)
        self.assertTrue(self.launcher.status()['ready'])
        self.assertEqual(runtime, self.launcher.current())
        self.assertEqual(self.launcher.active_session_clients(runtime), [])

    def test_global_already_active_is_disclosed_without_isolation_claim(self):
        self.launcher.start(self.credential, timeout=10)
        binary, project, _ = self.fake_cli()
        messages = []
        self.ui.say = messages.append
        self.launcher.session(self.credential, (str(binary), project), timeout=10)
        self.assertTrue(any('不会撤销' in text for text in messages))
        self.assertTrue(any('共享服务会留在后台' in text for text in messages))

    def test_stop_between_reservation_and_spawn_cancels_client(self):
        start = self.launcher.start
        def stop_after_start(*args, **kwargs):
            outcome = start(*args, **kwargs)
            self.launcher.stop()
            return outcome
        self.launcher.start(self.credential, mode='session', timeout=10)
        with patch.object(self.launcher, 'start', side_effect=stop_after_start), \
             patch.object(g.subprocess, 'Popen', side_effect=AssertionError('must not spawn')):
            with self.assertRaisesRegex(g.ProtocolError, 'cancelled_by_stop'):
                self.launcher.session(self.credential, ('/fixture/codex', Path(self.temp.name)))
        self.assertEqual(self.snapshot_home()['config.toml'][0], self.original)

    def test_stop_during_session_readiness_cancels_without_config_transaction(self):
        wait = self.launcher.ports.wait
        calls = []
        def stop_first(seconds):
            if not calls:
                calls.append(True)
                g.Launcher(self.state, ui=self.ui).stop()
            wait(seconds)
        with self.no_transactions(), patch.object(self.launcher.ports, 'wait', side_effect=stop_first):
            with self.assertRaisesRegex(g.ProtocolError, 'cancelled_by_stop'):
                self.launcher.start(self.credential, mode='session', timeout=10)
        self.assertEqual(self.snapshot_home()['config.toml'][0], self.original)

    def test_post_spawn_save_failure_recovers_and_waits_with_child_tracked(self):
        self.launcher.start(self.credential, mode='session', timeout=10)
        real_save = g.save
        failures = []
        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
        real_wait = child.wait
        def fail_once(path, value):
            if path.parent.name == 'sessions' and value.get('stage') == 'RUNNING' and not failures:
                failures.append(True)
                raise OSError('fixture one-shot disk error')
            return real_save(path, value)
        def finish_client(*args, **kwargs):
            self.assertEqual(len(self.launcher.active_session_clients(self.launcher.current())), 1)
            with self.assertRaisesRegex(g.ProtocolError, 'session_clients_active'):
                self.launcher.start(self.credential, package_sha256='c' * 64)
            child.terminate()
            return real_wait(timeout=5)
        try:
            with patch.object(g.subprocess, 'Popen', return_value=child), patch.object(g, 'save', side_effect=fail_once), \
                 patch.object(child, 'wait', side_effect=finish_client):
                result = self.launcher.session(self.credential, ('/fixture/codex', Path(self.temp.name)))
            self.assertEqual(result['stage'], 'SESSION_EXITED')
            self.assertEqual(self.launcher.active_session_clients(self.launcher.current()), [])
            self.assertTrue(self.launcher.status()['ready'])
        finally:
            if child.poll() is None:
                child.terminate(); real_wait(timeout=5)

    def test_unavailable_live_client_identity_keeps_reservation_until_exit(self):
        self.launcher.start(self.credential, mode='session', timeout=10)
        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
        real_identity, real_wait = g.process_identity, child.wait
        def identity(pid):
            return None if pid == child.pid else real_identity(pid)
        def finish_client(*args, **kwargs):
            self.assertEqual(len(self.launcher.active_session_clients(self.launcher.current())), 1)
            with self.assertRaisesRegex(g.ProtocolError, 'session_clients_active'):
                self.launcher.start(self.credential, package_sha256='c' * 64)
            child.terminate()
            return real_wait(timeout=5)
        try:
            with patch.object(g.subprocess, 'Popen', return_value=child), patch.object(g, 'process_identity', side_effect=identity), \
                 patch.object(child, 'wait', side_effect=finish_client):
                result = self.launcher.session(self.credential, ('/fixture/codex', Path(self.temp.name)))
            self.assertEqual(result['stage'], 'SESSION_EXITED')
            self.assertEqual(self.launcher.active_session_clients(self.launcher.current()), [])
            self.assertTrue(self.launcher.status()['ready'])
        finally:
            if child.poll() is None:
                child.terminate(); real_wait(timeout=5)

    def test_repeated_tracking_write_failure_rolls_back_only_new_cli(self):
        self.launcher.start(self.credential, mode='session', timeout=10)
        runtime = self.launcher.current()
        real_save = g.save
        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
        def fail_after_spawn(path, value):
            if path.parent.name == 'sessions' and value.get('stage') == 'RUNNING':
                raise OSError('fixture persistent disk error')
            return real_save(path, value)
        try:
            with patch.object(g.subprocess, 'Popen', return_value=child), patch.object(g, 'save', side_effect=fail_after_spawn):
                with self.assertRaises(OSError):
                    self.launcher.session(self.credential, ('/fixture/codex', Path(self.temp.name)))
            self.assertIsNotNone(child.poll())
            self.assertEqual(runtime, self.launcher.current())
            self.assertTrue(self.launcher.status()['ready'])
            self.assertEqual(self.launcher.active_session_clients(runtime), [])
        finally:
            if child.poll() is None:
                child.terminate(); child.wait(timeout=5)

    def test_unicode_quoted_catalog_path_is_valid_toml(self):
        info = self.launcher._config_info(self.settings, self.credential)
        path = self.state / '中文 "quoted" catalog.json'
        self.launcher.tx.write_catalog(path, info['allowed_pairs'], info['selection'])
        info['catalog_path'] = str(path)
        argv = sc.codex_argv('/fixture/codex', Path(self.temp.name), info, '1' * 32)
        parsed = tomllib.loads('\n'.join(argv[i + 1] for i, arg in enumerate(argv) if arg == '-c'))
        self.assertEqual(parsed['model_catalog_json'], str(path))

    def test_generated_overrides_are_valid_toml_unique_and_keep_safety_config(self):
        info = self.launcher._config_info(self.settings, self.credential)
        vectors = [sc.codex_argv('/a path/codex', Path(self.temp.name), info, ident * 32) for ident in ('1', '2')]
        self.assertNotEqual(vectors[0], vectors[1])
        for argv in vectors:
            self.assertEqual(argv[:4], ['/a path/codex', '--no-daemon', '-C', self.temp.name])
            parsed = tomllib.loads('\n'.join(argv[index + 1] for index, value in enumerate(argv) if value == '-c'))
            provider = parsed['model_providers'][parsed['model_provider']]
            self.assertEqual(provider['env_key'], sc.BEARER_NAME)
            self.assertNotIn('http_headers', provider)
            self.assertEqual(provider['base_url'], info['base_url'])
            self.assertFalse(provider['requires_openai_auth'])
            self.assertEqual(provider['request_max_retries'], 1)
            self.assertEqual(provider['stream_max_retries'], 0)
            self.assertFalse(provider['supports_websockets'])
            self.assertEqual(parsed['model'], 'gpt-6-astra')
            self.assertEqual(parsed['model_reasoning_effort'], 'xhigh')
            self.assertNotIn('web_search', parsed)
            self.assertNotIn('sandbox_mode', parsed)
            self.assertNotIn('--sandbox', argv)
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', argv)
            self.assertEqual(set(parsed), {'model_provider', 'model', 'model_reasoning_effort', 'model_catalog_json', 'model_providers'})


class SessionCommandTests(unittest.TestCase):
    def test_mode_choice_defaults_to_session_and_can_cancel(self):
        ui = fixtures.UserInterface()
        ui.text = lambda prompt, default='': default
        self.assertEqual(g.choose_mode(ui), 'session')
        for value, expected in (('1', 'session'), ('session', 'session'), ('2', 'global'), ('global', 'global')):
            ui.text = lambda *args, value=value: value
            self.assertEqual(g.choose_mode(ui), expected)
        ui.text = lambda *args: '0'
        with self.assertRaises(g.Cancelled):
            g.choose_mode(ui)

    def test_mode_choice_reprompts_invalid_option(self):
        ui = fixtures.UserInterface()
        answers = iter(['wrong', '2'])
        ui.text = lambda *args: next(answers)
        self.assertEqual(g.choose_mode(ui), 'global')

    def test_preflight_checks_pinned_version_and_project_before_start(self):
        ui = fixtures.UserInterface()
        ui.text = lambda *args: '/tmp'
        with patch.object(sc, 'existing_codex', return_value='/fixture/codex'), \
             patch.object(sc.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='codex-cli 0.159.2\n')) as run:
            self.assertEqual(sc.preflight(ui), ('/fixture/codex', Path('/tmp')))
            self.assertEqual(run.call_args.args[0], ['/fixture/codex', '--version'])
        with patch.object(sc, 'existing_codex', return_value='/fixture/codex'), \
             patch.object(sc.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='codex-cli unknown')):
            with self.assertRaisesRegex(g.ProtocolError, 'requires_codex'):
                sc.preflight(ui)

    def test_preflight_project_cancel(self):
        ui = fixtures.UserInterface()
        ui.text = lambda *args: '0'
        with patch.object(sc, 'existing_codex', return_value='/fixture/codex'), \
             patch.object(sc.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='codex-cli 0.159.2')):
            with self.assertRaises(g.Cancelled):
                sc.preflight(ui)

    def test_explicit_missing_binary_never_falls_back(self):
        with patch.dict(os.environ, {'CODEX_BIN': '/fixture/missing/codex'}), patch.object(sc.shutil, 'which', return_value=sys.executable):
            with self.assertRaisesRegex(g.ProtocolError, 'existing_codex_cli_missing'):
                sc.existing_codex()

    def test_reexec_preserves_codex_binary_and_terminal_env(self):
        with patch.dict(os.environ, {'CODEX_BIN': '/fixture/codex', 'TERM': 'xterm', 'OPENAI_API_KEY': 'exclude'}):
            env = g.base_environment()
        self.assertEqual(env['CODEX_BIN'], '/fixture/codex')
        self.assertEqual(env['TERM'], 'xterm')
        self.assertNotIn('OPENAI_API_KEY', env)

    def test_session_status_never_claims_global_applied(self):
        ui = fixtures.UserInterface()
        messages = []
        ui.say = messages.append
        g.print_status({'stage': 'RUNNING', 'configuration_scope': 'session'}, ui)
        self.assertFalse(any('全局配置已启用' in line for line in messages))
        self.assertTrue(any('全局配置保持原样' in line for line in messages))

    def test_main_session_exit_code_and_no_desktop_open(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state'
            ui = fixtures.UserInterface()
            with patch.object(g, 'UI', return_value=ui), patch.object(g.sys.stdin, 'isatty', return_value=True), \
                 patch.object(sc, 'preflight', return_value=('/fixture/codex', Path('/tmp'))), \
                 patch.object(g, 'verify_package', return_value='b' * 64), patch.object(g, 'setup', return_value=object()), \
                 patch.object(g, 'load_settings', return_value={'bundle_root': str(g.BUNDLE_ROOT), 'python': sys.executable}), \
                 patch.object(g, 'prerequisites', return_value=True), \
                 patch.object(g.Launcher, 'session', return_value={'stage': 'SESSION_EXITED', 'exit_code': 7}) as session, \
                 patch.object(g, 'open_desktop', side_effect=AssertionError('desktop')):
                self.assertEqual(g.main(['session', '--state-dir', str(state)]), 7)
                session.assert_called_once()

    def test_start_and_global_commands_remain_global(self):
        for command in ('start', 'global'):
            with tempfile.TemporaryDirectory() as directory:
                ui = fixtures.UserInterface()
                with patch.object(g, 'UI', return_value=ui), patch.object(g.sys.stdin, 'isatty', return_value=True), \
                     patch.object(g, 'verify_package', return_value='b' * 64), patch.object(g, 'setup', return_value=object()), \
                     patch.object(g, 'load_settings', return_value={'bundle_root': str(g.BUNDLE_ROOT), 'python': sys.executable}), \
                     patch.object(g, 'prerequisites', return_value=True), patch.object(g.sys, 'platform', 'linux'), \
                     patch.object(g.Launcher, 'start', return_value={'stage': 'RUNNING'}) as start, \
                     patch.object(g.Launcher, 'session', side_effect=AssertionError('session')):
                    self.assertEqual(g.main([command, '--state-dir', str(Path(directory) / 'state')]), 0)
                    start.assert_called_once()

    def test_first_setup_reexec_preserves_session_command_and_explicit_codex(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state'
            credential = fixtures.Credentials('fixture_key', 'fixture_bearer_123456789')
            with patch.dict(os.environ, {'CODEX_BIN': '/chosen/codex'}), \
                 patch.object(g, 'UI', return_value=fixtures.UserInterface()), \
                 patch.object(g.sys.stdin, 'isatty', return_value=True), \
                 patch.object(sc, 'preflight', return_value=('/chosen/codex', Path('/tmp'))), \
                 patch.object(g, 'verify_package', return_value='b' * 64), \
                 patch.object(g, 'setup', return_value=credential), \
                 patch.object(g, 'load_settings', return_value={'bundle_root': str(g.BUNDLE_ROOT), 'python': '/private/python'}), \
                 patch.object(g, 'prerequisites', return_value=True), \
                 patch.object(g.os, 'execve', side_effect=KeyboardInterrupt) as execute:
                self.assertEqual(g.main(['session', '--state-dir', str(state)]), 130)
            self.assertEqual(execute.call_args.args[1][-3:], ['session', '--state-dir', str(state)])
            self.assertEqual(execute.call_args.args[2]['CODEX_BIN'], '/chosen/codex')

    def test_menu_mode_cancel_leaves_settings_and_services_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state'
            ui = fixtures.UserInterface()
            answers = iter(['1', '0', '0'])
            ui.text = lambda *args: next(answers)
            with patch.object(g, 'UI', return_value=ui), patch.object(g.sys.stdin, 'isatty', return_value=True), \
                 patch.object(g, 'setup', side_effect=AssertionError('setup')), \
                 patch.object(g.Launcher, 'start', side_effect=AssertionError('start')):
                self.assertEqual(g.main(['--state-dir', str(state)]), 0)
            self.assertFalse(state.exists())

    def test_stop_with_active_session_can_be_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            ui = fixtures.UserInterface(approve=False)
            with patch.object(g, 'UI', return_value=ui), \
                 patch.object(g.Launcher, 'active_session_clients', return_value=[{}]), \
                 patch.object(g.Launcher, 'stop', side_effect=AssertionError('stop')):
                self.assertEqual(g.main(['stop', '--state-dir', str(Path(directory) / 'state')]), 130)
            self.assertEqual([word for _, word in ui.prompts], ['STOP'])


if __name__ == '__main__':
    unittest.main()
