"""Offline launcher checks: no native admissions, inference, Google writes or installs."""
import copy
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from remote_transport import operator as op
from remote_transport import router_mac as router
from remote_transport.model import ProtocolError, hash_bytes
from remote_transport.selection import load_catalog, select, admission_receipt, spawn_arguments
from remote_transport.router_bootstrap import forward_probe_bytes
from remote_tests import test_router_mac as router_mac_tests


class ModelSelectionLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = load_catalog()
        self.selection = select(self.catalog, 'gpt-6.1-sol', 'xhigh')
        self.config = router_mac_tests.RouterMacPureTests().config(self.root)
        self.args = types.SimpleNamespace(model=None, effort=None, catalog=None)

    def tearDown(self):
        self.temp.cleanup()

    def test_old_config_remains_explicit_unverified_legacy(self):
        self.assertEqual(router._validate_config(self.config), self.config)
        self.assertIsNone(router._resolve_selection(self.args, self.config))
        command = op.codex_command('http://127.0.0.1:8123/v1', self.root)
        self.assertEqual(command[command.index('--model') + 1], 'native-subagent-bridge')
        self.assertNotIn('model_supports_reasoning=true', command)
        self.assertEqual(op.selection_status(None)['underlying_model'], 'unknown')
        self.assertEqual(op.selection_status(None)['selection_mode'], 'legacy_unverified')

    def test_explicit_pair_overrides_stored_selection_only_for_new_start(self):
        config = {**self.config, 'model_selection': self.selection}
        self.assertEqual(router._resolve_selection(self.args, config), self.selection)
        self.args.model = 'gpt-6-luna'; self.args.effort = 'medium'
        self.assertEqual(router._resolve_selection(self.args, config),
                         select(self.catalog, 'gpt-6-luna', 'medium'))
        self.assertEqual(config['model_selection'], self.selection)

    def test_partial_selection_does_not_inherit_or_default(self):
        for model, effort in [('gpt-6.1-sol', None), (None, 'xhigh')]:
            self.args.model = model; self.args.effort = effort
            with self.assertRaisesRegex(ProtocolError, 'model_and_effort_required_together'):
                router._resolve_selection(self.args, {**self.config, 'model_selection': self.selection})

    def test_catalog_alone_does_not_select_a_model(self):
        path = self.root / 'snapshot.json'; path.write_text(json.dumps(self.catalog))
        self.args.catalog = str(path)
        self.assertIsNone(router._resolve_selection(self.args, self.config))
        self.assertEqual(router.models(self.args)['catalog_sha256'], self.selection['catalog_sha256'])
        self.assertFalse(router.models(self.args)['live_availability_verified'])

    def test_modified_snapshot_is_not_native_entitlement(self):
        bad = copy.deepcopy(self.catalog)
        bad['models']['invented-model'] = next(iter(bad['models'].values()))
        path = self.root / 'snapshot.json'; path.write_text(json.dumps(bad))
        self.args.catalog = str(path)
        with self.assertRaisesRegex(ProtocolError, 'catalog_not_supported'):
            router._resolve_selection(self.args, self.config)

    def test_invalid_pair_fails_before_google_clients_or_other_external_actions(self):
        self.args.config = 'unused'; self.args.model = 'gpt-6.1-sol'; self.args.effort = 'ultra'
        with patch.object(router, '_load_config', return_value=self.config), \
             patch.object(router, '_set_google_env') as env, \
             patch.object(router.subprocess, 'run') as run, \
             patch('examples.google_clients.create_docs_client') as docs:
            with self.assertRaisesRegex(ProtocolError, 'unsupported_model_effort_pair'):
                router._start_locked(self.args, self.root / 'active.json', 'intent')
            env.assert_not_called(); run.assert_not_called(); docs.assert_not_called()

    def test_selected_router_version_cannot_be_bypassed_by_user_config(self):
        self.args.config = 'unused'; self.args.model = 'gpt-6.1-sol'; self.args.effort = 'xhigh'
        config = {**self.config, 'expected_codex_version': 'codex-cli 0.160.0'}
        with patch.object(router, '_load_config', return_value=config), \
             patch.object(router, '_set_google_env'), \
             patch.object(router.subprocess, 'run', return_value=types.SimpleNamespace(stdout='codex-cli 0.160.0')), \
             patch('examples.google_clients.create_docs_client') as docs:
            with self.assertRaisesRegex(ProtocolError, 'selected_codex_version_requires_live_acceptance'):
                router._start_locked(self.args, self.root / 'active.json', 'intent')
            docs.assert_not_called()

    def test_selected_command_has_exact_pair_local_catalog_and_original_safety(self):
        from remote_transport.codex_catalog import write_catalog
        path = write_catalog(self.root / 'codex-models.json', self.selection)
        command = op.codex_command('http://127.0.0.1:8123/v1', self.root,
                                   self.selection, catalog_path=path)
        self.assertEqual(command[command.index('--model') + 1], 'gpt-6.1-sol')
        self.assertIn('model_reasoning_effort="xhigh"', command)
        self.assertNotIn('model_supports_reasoning=true', command)
        self.assertIn('model_catalog_json=' + json.dumps(str(Path(path).resolve())), command)
        for flag in ('on-request', 'workspace-write', 'features.multi_agent=false',
                     'features.multi_agent_v2=false', 'features.unbounded_connection_retries=false'):
            self.assertIn(flag, command)
        self.assertIn('request_max_retries=0', ' '.join(command))
        self.assertIn('stream_max_retries=0', ' '.join(command))
        self.assertIn('requires_openai_auth=false', ' '.join(command))

    def test_selected_command_cannot_omit_or_mutate_catalog(self):
        from remote_transport.codex_catalog import write_catalog
        with self.assertRaisesRegex(ProtocolError, 'selected_codex_catalog_required'):
            op.codex_command('http://127.0.0.1:8123/v1', self.root, self.selection)
        path = write_catalog(self.root / 'codex-models.json', self.selection)
        value = json.loads(Path(path).read_text())
        value['models'][0]['use_responses_lite'] = True
        Path(path).write_text(json.dumps(value))
        with self.assertRaises(Exception):
            op.codex_command('http://127.0.0.1:8123/v1', self.root, self.selection, catalog_path=path)

    def test_operator_flags_assert_binding_and_reject_pair_change(self):
        ready = {'selection': self.selection}
        self.assertEqual(op.ready_selection(ready, model='gpt-6.1-sol', effort='xhigh'), self.selection)
        for model, effort in [('gpt-6-luna', 'xhigh'), ('gpt-6.1-sol', 'high')]:
            with self.assertRaisesRegex(ProtocolError, 'immutable_start_new_session'):
                op.ready_selection(ready, model=model, effort=effort)
        with self.assertRaisesRegex(ProtocolError, 'legacy_session_has_no_model_binding'):
            op.ready_selection({}, model='gpt-6.1-sol', effort='xhigh')
        self.assertEqual(ready['selection'], self.selection)

    def test_selected_operator_version_check_requires_exact_tested_cli(self):
        from remote_transport.codex_catalog import write_catalog
        path = write_catalog(self.root / 'codex-models.json', self.selection)
        ready = self.root / 'ready.json'
        router._private_json(ready, {'base_url': 'http://127.0.0.1:8123/v1',
                                    'selection': self.selection, 'codex_model_catalog': path})
        argv = ['operator', 'check-codex', '--ready', str(ready)]
        with patch('sys.argv', argv), patch.object(op.subprocess, 'run',
                return_value=types.SimpleNamespace(stdout='codex-cli 0.160.0')) as run:
            with self.assertRaisesRegex(ProtocolError, 'selected_codex_version_requires_live_acceptance'):
                op.main()
            self.assertEqual(run.call_count, 1)
        with patch('sys.argv', argv), patch.object(op.subprocess, 'run', side_effect=[
                types.SimpleNamespace(stdout='codex-cli 0.159.2'),
                types.SimpleNamespace(stdout='--config --model --cd --sandbox --ask-for-approval')]):
            result = op.main()
        self.assertTrue(result['required_flags_present'])
        self.assertFalse(result['provider_transport_tested'])
        self.assertFalse(result['underlying_model_verified'])

    def test_status_and_stop_flags_cannot_silently_change_selection(self):
        for operation in ('status', 'stop'):
            with patch('sys.argv', ['router', operation, '--model', 'gpt-6-luna', '--effort', 'high']), \
                 patch.object(router, operation) as handler:
                with self.assertRaisesRegex(ProtocolError, 'selection_flags_require_new_session_start'):
                    router.main()
                handler.assert_not_called()

    def test_config_rejects_unknown_fields_and_invalid_saved_selection(self):
        with self.assertRaisesRegex(ProtocolError, 'invalid_router_config'):
            router._validate_config({**self.config, 'model': 'gpt-6.1-sol'})
        bad = {**self.selection, 'reasoning_effort': 'ultra'}
        with self.assertRaises(ProtocolError):
            router._validate_config({**self.config, 'model_selection': bad})
        with self.assertRaisesRegex(ProtocolError, 'absolute_catalog_path_required'):
            router._validate_config({**self.config, 'catalog': 'relative.json'})

    def test_selected_join_message_explains_real_admission(self):
        message = router._join_message({'bootstrap_document_id': 'doc', 'bootstrap_tab_id': 't.0',
                                       'model_selection': self.selection}, 'a' * 32)
        self.assertIn('required_model=gpt-6.1-sol', message)
        self.assertIn('required_reasoning_effort=xhigh', message)
        self.assertIn('MODEL_SELECTION.zh-CN.md', message)

    def test_router_carries_required_pair_and_observed_admission_into_new_pin(self):
        self.args.config = 'unused'; self.args.model = 'gpt-6.1-sol'; self.args.effort = 'xhigh'
        active_path = self.root / 'active.json'
        native_id = '/root/model_worker'
        receipt = admission_receipt(self.selection, spawn_arguments(self.selection, 'model_worker', 'fixture'), native_id)
        initial_calls = []
        original_initial = router.bootstrap_initial_state
        def initial(**kwargs):
            initial_calls.append(kwargs)
            return original_initial(**kwargs)
        def create_doc(drive, docs, path, active, config, label):
            return {**active, label.lower() + '_document_id': label.lower(),
                    label.lower() + '_tab_id': 't.0'}
        def probe(drive, active, config):
            boot = active['bootstrap_id']; nonce = 'n' * 32
            return {'file_id': 'forward', 'name': 'dots2codex-router-forward-probe-' + boot + '.json',
                    'nonce': nonce, 'sha256': hash_bytes(forward_probe_bytes(boot, nonce))}
        with patch.object(router, '_load_config', return_value=self.config), \
             patch.object(router, '_set_google_env'), \
             patch.object(router.subprocess, 'run', return_value=types.SimpleNamespace(stdout='codex-cli 0.159.2')), \
             patch('examples.google_clients.create_docs_client'), \
             patch('examples.google_clients.create_drive_client'), \
             patch.object(router, '_create_doc_once', side_effect=create_doc), \
             patch.object(router, '_create_forward_probe', side_effect=probe), \
             patch.object(router, 'bootstrap_initial_state', side_effect=initial), \
             patch.object(router, '_initialize_bootstrap'), \
             patch.object(router, '_copy_clipboard', return_value=False), \
             patch.object(router, '_wait_for', return_value=types.SimpleNamespace(state={'worker': {'admission': receipt}})), \
             patch.object(router, 'verify_worker_admission', return_value=native_id), \
             patch.object(router, '_verify_reverse_probe'), \
             patch.object(router, 'deployment', side_effect=RuntimeError('stop-before-pin-write')) as deploy, \
             patch.object(router, '_cleanup'), patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError, 'stop-before-pin-write'):
                router._start_locked(self.args, active_path, 'intent')
        self.assertEqual(initial_calls[0]['required_selection'], self.selection)
        self.assertEqual(deploy.call_args.kwargs['inference'], {'selection': self.selection, 'admission': receipt})
        self.assertEqual(router._load_private_json(active_path)['model_selection'], self.selection)


if __name__ == '__main__':
    unittest.main()
