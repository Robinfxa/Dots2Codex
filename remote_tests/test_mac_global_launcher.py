"""Offline desktop-mode routing. No real Google, native UI, install, or config writes."""
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from remote_tests.test_mac_launcher import FakeUI, LauncherFixture
from remote_transport import mac_environment as envmod
from remote_transport import mac_launcher as launch
from remote_transport.mac_ui import Cancelled
from remote_transport.model import ProtocolError
from remote_transport.session import _save


class FakeGlobal:
    def __init__(self):
        self.created = []; self.calls = []; self.error = None

    def factory(self, **kwargs):
        self.created.append(kwargs)
        return self

    def _call(self, operation, args):
        self.calls.append((operation, args))
        if self.error: raise self.error
        return {'mode':'global-desktop', 'operation':operation,
                'production_ready':False, 'router_ready':False}

    def start(self, args): return self._call('start', args)
    def status(self, args): return self._call('status', args)
    def stop(self, args): return self._call('stop', args)
    def restore(self, args): return self._call('restore', args)


class GlobalLauncherTests(LauncherFixture):
    def setUp(self):
        super().setUp()
        self.global_backend = FakeGlobal()
        self.launcher.global_factory = self.global_backend.factory
        self.args.operation = 'global-start'
        self.args.codex_home = str(self.root/'desktop home')
        self.args.desktop_codex = '/synthetic/Codex.app/Contents/Resources/codex'

    def configured(self):
        self.configure()
        self.ui.confirmed.clear(); self.ports.calls.clear()

    def test_global_start_uses_distinct_backend_with_approved_config_hash(self):
        self.configured()
        before = self.config.read_bytes()
        result = self.launcher.dispatch(self.args)
        self.assertEqual(result['mode'], 'global-desktop')
        self.assertFalse(result['production_ready'])
        self.assertEqual([c[0] for c in self.ports.calls], ['google'])
        self.assertEqual([c[0] for c in self.global_backend.calls], ['start'])
        self.assertEqual(self.ui.confirmed, [])
        call = self.global_backend.calls[0][1]
        self.assertEqual(call.expected_config_sha256, hashlib.sha256(before).hexdigest())
        self.assertEqual(call.config, str(self.config))
        self.assertEqual(call.codex_home, self.args.codex_home)
        self.assertEqual(call.desktop_codex, self.args.desktop_codex)
        self.assertFalse(hasattr(call, 'launch_codex'))
        self.assertFalse(self.active.exists())

    def test_factory_receives_global_subdirectory_and_shared_config(self):
        sentinel_ports = object(); self.launcher.global_ports = sentinel_ports
        self.args.operation = 'global-status'; self.launcher.dispatch(self.args)
        self.assertEqual(self.global_backend.created, [{
            'root':self.launcher.root, 'state':self.state/'global',
            'config':self.config, 'ui':self.ui, 'ports':sentinel_ports}])
        self.assertFalse(self.state.exists())

    def test_first_global_start_runs_existing_safe_configuration_flow(self):
        self.launcher.dispatch(self.args)
        self.assertTrue(self.launcher.approved(self.launcher.load()))
        self.assertEqual([c[0] for c in self.ports.calls], ['codex','google','google'])
        self.assertEqual([c[0] for c in self.global_backend.calls], ['start'])
        self.assertNotIn(self.secret, str(self.global_backend.created)+str(self.global_backend.calls))
        self.assertIn('exact-diff confirmation', self.ui.confirmed[-1])

    def test_configuration_cancel_never_constructs_global_backend(self):
        self.ui.confirms = [False]
        with self.assertRaises(Cancelled): self.launcher.dispatch(self.args)
        self.assertEqual(self.global_backend.created, [])
        self.assertFalse(self.state.exists()); self.assertFalse(self.config.exists())

    def test_changed_credential_requires_reuse_before_google_or_start(self):
        self.configured()
        credential = dict(self.credential_info, refresh_token='CHANGED_FIXTURE')
        _save(self.credential, credential)
        self.ui.confirms = [False]
        with self.assertRaises(Cancelled): self.launcher.dispatch(self.args)
        self.assertEqual(self.ports.calls, [])
        self.assertEqual(self.global_backend.created, [])

    def test_backend_cancel_preserves_config_and_existing_approval(self):
        self.configured()
        before = (self.config.read_bytes(), self.launcher.consent.read_bytes())
        self.global_backend.error = Cancelled()
        with self.assertRaises(Cancelled): self.launcher.dispatch(self.args)
        self.assertEqual(before, (self.config.read_bytes(), self.launcher.consent.read_bytes()))
        self.assertFalse(self.active.exists())

    def test_google_failure_never_starts_global_backend(self):
        self.configured(); self.ports.google_error = RuntimeError('launcher_google_read_unavailable')
        with self.assertRaisesRegex(RuntimeError, 'google_read_unavailable'):
            self.launcher.dispatch(self.args)
        self.assertEqual(self.global_backend.created, [])

    def test_global_controls_work_with_broken_shared_configuration(self):
        self.config.parent.mkdir(mode=0o700)
        self.config.write_text('broken configuration'); self.config.chmod(0o600)
        for operation in ('global-status', 'global-stop', 'global-restore', 'restore-global'):
            self.args.operation = operation
            self.launcher.dispatch(self.args)
        self.assertEqual([c[0] for c in self.global_backend.calls], ['status','stop','restore','restore'])
        self.assertEqual(self.ports.calls, [])
        self.assertFalse(self.state.exists())

    def test_global_start_does_not_route_existing_single_worker_controls(self):
        self.configured(); self.active_write()
        active_before = self.active.read_bytes()
        self.launcher.dispatch(self.args)
        self.assertEqual([c[0] for c in self.global_backend.calls], ['start'])
        self.assertEqual(self.active.read_bytes(), active_before)
        self.assertEqual([c[0] for c in self.ports.calls], ['google'])

    def test_named_single_start_and_legacy_alias_keep_existing_backend(self):
        self.configured()
        for operation in ('single-start', 'start'):
            self.args.operation = operation
            self.launcher.dispatch(self.args)
        self.assertEqual([c[0] for c in self.ports.calls].count('start'), 2)
        self.assertEqual(self.global_backend.created, [])

    def test_menu_global_start_selects_desktop_backend(self):
        self.configured(); self.args.operation = 'menu'
        self.ui.choices = ['Start Global desktop routing']
        self.launcher.dispatch(self.args)
        self.assertEqual([c[0] for c in self.global_backend.calls], ['start'])
        self.assertEqual([c[0] for c in self.ports.calls], ['google'])

    def test_menu_settings_remains_available_without_global_backend(self):
        self.args.operation = 'menu'; self.ui.choices = ['Settings']
        result = self.launcher.dispatch(self.args)
        self.assertTrue(result['configured']); self.assertFalse(result['global_config_changed'])
        self.assertEqual(self.global_backend.created, [])

    def test_desktop_menu_cancel_has_no_mutations(self):
        self.args.operation = 'menu'; self.ui.choices = [Cancelled()]
        with self.assertRaises(Cancelled): self.launcher.dispatch(self.args)
        self.assertEqual(self.global_backend.created, []); self.assertEqual(self.ports.calls, [])
        self.assertFalse(self.state.exists())


class GlobalMainTests(LauncherFixture):
    def argv(self, operation=None):
        return ([operation] if operation else []) + ['--state', str(self.state),
            '--config', str(self.config), '--active', str(self.active)]

    def test_default_is_desktop_menu_with_distinct_controls(self):
        self.assertEqual(launch.parser().parse_args([]).operation, 'menu')
        self.assertEqual(next(iter(launch.MENU_ACTIONS.values())), 'global-start')
        self.assertEqual(set(launch.MENU_ACTIONS.values()), {
            'global-start','global-status','global-stop','global-restore','settings',
            'single-start','single-status','single-stop','quit'})

    def test_default_menu_cancel_and_quit_never_check_environment_or_backend(self):
        for choice in (Cancelled(), 'Quit'):
            with patch.object(launch, 'UI', return_value=FakeUI(choices=[choice])), \
                 patch.object(launch, 'verify_package'), patch.object(launch, 'Environment') as environment, \
                 patch.object(launch.Launcher, 'dispatch') as dispatch:
                with self.assertRaises(Cancelled): launch.main(self.argv())
                environment.assert_not_called(); dispatch.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_global_status_stop_restore_never_install_or_validate_settings(self):
        for operation in ('global-status', 'global-stop', 'global-restore', 'restore-global'):
            with patch.object(launch, 'UI', return_value=self.ui), patch.object(launch, 'verify_package'), \
                 patch.object(launch, 'Environment') as environment, \
                 patch.object(launch.Launcher, 'load', side_effect=AssertionError('unexpected config load')), \
                 patch.object(launch.Launcher, 'dispatch', return_value={'router_ready':False}) as dispatch:
                environment.return_value.current.return_value = None
                launch.main(self.argv(operation))
                environment.return_value.ensure.assert_not_called()
                self.assertEqual(environment.call_args.kwargs, {'include_global':True})
                self.assertTrue(dispatch.call_args.args[0].operation.startswith('global-'))
        self.assertFalse(self.state.exists())

    def test_broken_environment_cannot_block_global_stop_or_restore(self):
        for operation in ('global-stop', 'global-restore'):
            with patch.object(launch, 'UI', return_value=self.ui), patch.object(launch, 'verify_package'), \
                 patch.object(launch, 'Environment', side_effect=ProtocolError('launcher_unpinned_dependency')), \
                 patch.object(launch.Launcher, 'dispatch', return_value={'router_ready':False}) as dispatch:
                self.assertEqual(launch.main(self.argv(operation)), 0)
                dispatch.assert_called_once()

    def test_menu_global_stop_does_not_install(self):
        with patch.object(launch, 'UI', return_value=FakeUI(choices=['Stop Global routing'])), \
             patch.object(launch, 'verify_package'), patch.object(launch, 'Environment') as environment, \
             patch.object(launch.Launcher, 'dispatch', return_value={'router_ready':False}) as dispatch:
            environment.return_value.current.return_value = None
            launch.main(self.argv())
            environment.return_value.ensure.assert_not_called()
            self.assertEqual(dispatch.call_args.args[0].operation, 'global-stop')

    def test_only_global_start_requests_global_dependencies_settings_stays_base(self):
        for operation, expected in (('global-start',True), ('single-start',False), ('settings',False)):
            with patch.object(launch, 'UI', return_value=self.ui), patch.object(launch, 'verify_package'), \
                 patch.object(launch, 'Environment') as environment, \
                 patch.object(launch.Launcher, 'dispatch', return_value={'router_ready':False}):
                environment.return_value.ensure.return_value = Path(sys.prefix)/'bin/python3'
                launch.main(self.argv(operation))
                self.assertEqual(environment.call_args.kwargs, {'include_global':expected})
                environment.return_value.ensure.assert_called_once_with(self.ui)

    def test_global_dependency_setup_cancel_never_dispatches_or_saves_settings(self):
        with patch.object(launch, 'UI', return_value=self.ui), patch.object(launch, 'verify_package'), \
             patch.object(launch, 'Environment') as environment, patch.object(launch.Launcher, 'dispatch') as dispatch:
            environment.return_value.ensure.side_effect = Cancelled()
            with self.assertRaises(Cancelled): launch.main(self.argv('global-start'))
            dispatch.assert_not_called()
        self.assertFalse(self.state.exists()); self.assertFalse(self.config.exists())

    def test_reexec_forwards_exact_explicit_desktop_paths_and_selection(self):
        python = self.root/'private env/bin/python3'
        args = self.argv('global-start') + ['--codex-home', str(self.root/'desktop home'),
            '--desktop-codex','/synthetic/Codex.app/Contents/Resources/codex',
            '--model','gpt-6.1-sol','--effort','xhigh']
        with patch.object(launch, 'UI', return_value=self.ui), patch.object(launch, 'verify_package'), \
             patch.object(launch, 'Environment') as environment, \
             patch.object(launch.os, 'execve', side_effect=RuntimeError('test_reexec')) as execute, \
             patch.object(launch.Launcher, 'dispatch') as dispatch:
            environment.return_value.ensure.return_value = python
            with self.assertRaisesRegex(RuntimeError, 'test_reexec'): launch.main(args)
            command = execute.call_args.args[1]
            self.assertEqual(command[4], 'global-start')
            for flag in ('--codex-home','--desktop-codex','--model','--effort'):
                self.assertEqual(command[command.index(flag)+1], args[args.index(flag)+1])
            self.assertNotIn('PYTHONPATH', execute.call_args.args[2])
            self.assertNotIn('PYTHONHOME', execute.call_args.args[2])
            dispatch.assert_not_called()


class GlobalEnvironmentTests(LauncherFixture):
    def test_global_pins_imports_and_pointer_are_separate_from_legacy(self):
        regular = envmod.Environment(launch.ROOT, self.state)
        global_env = envmod.Environment(launch.ROOT, self.state, include_global=True)
        self.assertEqual(global_env.pins, {**regular.pins, 'tomlkit':'0.13.3'})
        self.assertNotIn('tomlkit', regular.imports)
        self.assertIn('tomlkit', global_env.imports)
        self.assertNotEqual(regular.fingerprint, global_env.fingerprint)
        self.assertEqual(regular.pointer.name, 'environment.json')
        self.assertEqual(global_env.pointer.name, 'environment-global.json')
        self.assertFalse(self.state.exists())

    def test_global_install_cancel_has_explicit_pin_notice_and_no_processes(self):
        environment = envmod.Environment(launch.ROOT, self.state, include_global=True)
        self.ui.confirms = [False]
        with patch.object(environment, '_run') as run:
            with self.assertRaises(Cancelled): environment.ensure(self.ui)
            run.assert_not_called()
        self.assertIn('tomlkit==0.13.3', self.ui.confirmed[0])
        self.assertFalse(self.state.exists())

    def test_existing_global_environment_does_not_prompt_or_install(self):
        environment = envmod.Environment(launch.ROOT, self.state, include_global=True)
        path = self.state/'environments/existing'
        path.mkdir(mode=0o700, parents=True); self.state.chmod(0o700)
        _save(environment.pointer, {'contract':1,'path':str(path),'fingerprint':environment.fingerprint})
        with patch.object(environment, 'health', return_value=True), patch.object(environment, '_run') as run:
            self.assertEqual(environment.ensure(self.ui), path/'bin/python3')
            run.assert_not_called()
        self.assertEqual(self.ui.confirmed, [])

    def test_global_health_checks_tomlkit_import_and_exact_pin(self):
        environment = envmod.Environment(launch.ROOT, self.state, include_global=True)
        path = self.root/'venv'; path.mkdir(mode=0o700); (path/'bin').mkdir()
        executable = path/'bin/python3'; executable.write_text('synthetic'); executable.chmod(0o700)
        (path/'pyvenv.cfg').write_text('synthetic')
        with patch.object(environment, '_run', return_value=SimpleNamespace(returncode=0,stdout='healthy\n')) as run:
            self.assertTrue(environment.health(path))
            command = run.call_args_list[0].args[0]
            self.assertEqual(json.loads(command[-2])['tomlkit'], '0.13.3')
            self.assertIn('tomlkit', json.loads(command[-1]))


if __name__ == '__main__': unittest.main()
