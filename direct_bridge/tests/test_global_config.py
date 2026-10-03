"""Synthetic private-home transaction tests; no user home or model calls."""
from pathlib import Path
import json
import os
import stat
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from direct_bridge import global_config as cfg
from dots_lite import config_transaction as tx
from dots_lite.protocol import ProtocolError


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / 'codex'
        self.home.mkdir(mode=0o700)
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.path = self.home / 'config.toml'
        self.pair = {'model': 'gpt-6-astra', 'reasoning_effort': 'xhigh'}
        self.allowed = [self.pair]
        self.catalog_path = self.state / 'catalog.json'
        cfg.write_catalog(self.catalog_path, self.allowed, self.pair)
        self.info = {'base_url': 'http://127.0.0.1:54321/v1', 'config_id': 'direct-fixture-id',
                     'local_bearer': 'synthetic-local-bearer-not-an-api-key',
                     'catalog_path': str(self.catalog_path), 'selection': cfg.default_selection()}
        self.ready_count = 0

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, raw, mode=0o600):
        self.path.write_bytes(raw)
        self.path.chmod(mode)

    def ready(self):
        self.ready_count += 1
        return True

    def apply(self, **kwargs):
        p = cfg.preview(self.state, self.home, self.info)
        options = dict(expected_before_hash=p['before_hash'], expected_after_hash=p['after_hash'],
                       confirm=True, check_ready=self.ready)
        options.update(kwargs)
        return cfg.apply(self.state, self.home, self.info, **options)

    def active_id(self):
        return cfg.status(self.state, self.home)['active_transaction']['transaction_id']

    def test_real_toml_parser_and_roundtrip_preserve_unrelated(self):
        import tomlkit
        self.assertEqual(tomlkit.__version__, '0.13.3')
        original = (b'# keep my comment\nmodel = "prior" # selected\n'
                    b'model_provider = "openai"\nweb_search = "disabled" # manual\n'
                    b'approval_policy = "on-request"\nsandbox_mode = "workspace-write"\n'
                    b'[profiles.daily]\nmodel = "some-other"\n'
                    b'[projects."/sample"]\ntrust_level = "trusted"\n'
                    b'[model_providers.other]\nname = "Custom"\nbase_url = "http://localhost:55"\n')
        self.write(original)
        result = self.apply()
        parsed = tomllib.loads(self.path.read_text())
        self.assertEqual(parsed['model'], 'gpt-6-astra')
        self.assertEqual(parsed['model_reasoning_effort'], 'xhigh')
        self.assertEqual(parsed['model_provider'], cfg.PROVIDER)
        self.assertEqual(parsed['web_search'], 'disabled')
        self.assertEqual(parsed['profiles']['daily']['model'], 'some-other')
        self.assertEqual(parsed['projects']['/sample']['trust_level'], 'trusted')
        provider = parsed['model_providers'][cfg.PROVIDER]
        self.assertFalse(provider['requires_openai_auth'])
        self.assertEqual(provider['request_max_retries'], 1)
        self.assertEqual(provider['stream_max_retries'], 0)
        self.assertFalse(provider['supports_websockets'])
        self.assertNotIn('env_key', provider)
        self.assertEqual(provider['http_headers']['Authorization'], 'Bearer ' + self.info['local_bearer'])
        self.assertIn('# keep my comment', self.path.read_text())
        self.assertIn('# manual', self.path.read_text())
        self.assertEqual(self.ready_count, 2)
        cfg.restore(self.state, result['transaction_id'], confirm=True)
        self.assertEqual(self.path.read_bytes(), original)

    def test_search_absent_or_enabled_is_not_owned(self):
        for text in ('', 'web_search = "live"\n', 'web_search = "disabled"\n'):
            with self.subTest(text=text):
                self.write(text.encode())
                result = self.apply()
                self.assertEqual(tomllib.loads(self.path.read_text()).get('web_search'), tomllib.loads(text).get('web_search'))
                cfg.restore(self.state, result['transaction_id'], confirm=True)

    def test_preview_redacts_bearer_and_prior_values(self):
        self.write(b'model = "prior-sensitive-value"\n')
        p = cfg.preview(self.state, self.home, self.info)
        rendered = json.dumps(p)
        self.assertNotIn(self.info['local_bearer'], rendered)
        self.assertNotIn('prior-sensitive-value', rendered)
        self.assertTrue(p['credential_persistence_required'])
        self.assertFalse(p['write_performed'])
        self.assertFalse((self.state / 'config-transactions').exists())

    def test_private_apply_and_backups_and_permission_approval(self):
        self.write(b'model = "prior"\n', mode=0o644)
        p = cfg.preview(self.state, self.home, self.info)
        self.assertTrue(p['privacy_change_required'])
        with self.assertRaisesRegex(ProtocolError, 'explicit_private_config_permission_confirmation_required'):
            self.apply()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)
        result = self.apply(approve_private_config=True)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        directory = self.state / 'config-transactions'
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for file in directory.iterdir():
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        manifest = next(directory.glob('*.json')).read_text()
        self.assertNotIn(self.info['local_bearer'], manifest)
        self.assertNotIn(self.info['local_bearer'], json.dumps(cfg.status(self.state)))
        cfg.restore(self.state, result['transaction_id'], confirm=True)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_duplicate_start_keeps_original_restore_point(self):
        self.write(b'model = "prior"\n')
        first = self.apply()
        second = self.apply()
        self.assertEqual(first['transaction_id'], second['transaction_id'])
        self.assertFalse(second['write_performed'])
        self.assertEqual(len(list((self.state / 'config-transactions').glob('*.json'))), 1)
        cfg.restore(self.state, first['transaction_id'], confirm=True)
        self.assertEqual(self.path.read_bytes(), b'model = "prior"\n')

    def test_duplicate_start_requires_approval_to_repair_relaxed_permissions(self):
        self.write(b'model = "prior"\n')
        first = self.apply()
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ProtocolError, 'explicit_private_config_permission_confirmation_required'):
            self.apply()
        second = self.apply(approve_private_config=True)
        self.assertEqual(first['transaction_id'], second['transaction_id'])
        self.assertTrue(second['private_permissions_repaired'])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        cfg.restore(self.state, first['transaction_id'], confirm=True)
        self.assertEqual(self.path.read_bytes(), b'model = "prior"\n')

    def test_another_state_cannot_claim_direct_provider(self):
        self.write(b'model = "prior"\n')
        self.apply()
        second = self.root / 'another-state'
        second.mkdir(mode=0o700)
        changed = dict(self.info, config_id='other-direct-id', base_url='http://127.0.0.1:54322/v1')
        p = cfg.preview(second, self.home, changed)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ProtocolError, 'unowned_direct_provider_requires_review'):
            cfg.apply(second, self.home, changed, expected_before_hash=p['before_hash'],
                      expected_after_hash=p['after_hash'], confirm=True, check_ready=self.ready)
        self.assertEqual(self.path.read_bytes(), before)

    def test_apply_without_confirmation_or_readiness_refused(self):
        p = cfg.preview(self.state, self.home, self.info)
        args = dict(expected_before_hash=p['before_hash'], expected_after_hash=p['after_hash'])
        with self.assertRaisesRegex(ProtocolError, 'explicit_direct_config_confirmation_required'):
            cfg.apply(self.state, self.home, self.info, **args, check_ready=self.ready)
        with self.assertRaisesRegex(ProtocolError, 'local_services_recheck_required'):
            cfg.apply(self.state, self.home, self.info, **args, confirm=True)
        def not_ready():
            raise ProtocolError('fixture_not_ready')
        with self.assertRaisesRegex(ProtocolError, 'fixture_not_ready'):
            cfg.apply(self.state, self.home, self.info, **args, confirm=True, check_ready=not_ready)
        for value in (False, None, {}, {'ready': False}):
            with self.subTest(readiness=value):
                with self.assertRaisesRegex(ProtocolError, 'local_services_not_verified_ready'):
                    cfg.apply(self.state, self.home, self.info, **args, confirm=True, check_ready=lambda: value)
        self.assertFalse(self.path.exists())

    def test_preview_edit_and_final_race_refused(self):
        self.write(b'model = "prior"\n')
        p = cfg.preview(self.state, self.home, self.info)
        self.write(b'model = "later"\n')
        with self.assertRaisesRegex(ProtocolError, 'preview_outdated'):
            cfg.apply(self.state, self.home, self.info, expected_before_hash=p['before_hash'],
                      expected_after_hash=p['after_hash'], confirm=True, check_ready=self.ready)
        with self.assertRaisesRegex(ProtocolError, 'config_concurrent_edit'):
            self.apply(before_commit=lambda: self.write(b'model = "raced"\n'))
        self.assertEqual(self.path.read_bytes(), b'model = "raced"\n')

    def test_three_way_restore_preserves_unrelated_later_edits(self):
        self.write(b'model = "prior"\nweb_search = "disabled"\n[profiles.daily]\nmodel = "profile"\n')
        result = self.apply()
        with self.path.open('a') as output:
            output.write('\n[projects."/new"]\ntrust_level = "trusted"\n')
        result = cfg.restore(self.state, result['transaction_id'], confirm=True)
        self.assertEqual(result['mode'], 'three_way_owned_values')
        parsed = tomllib.loads(self.path.read_text())
        self.assertEqual(parsed['model'], 'prior')
        self.assertEqual(parsed['projects']['/new']['trust_level'], 'trusted')
        self.assertNotIn(cfg.PROVIDER, parsed.get('model_providers', {}))

    def test_owned_value_or_syntax_conflict_is_not_overwritten(self):
        for changed in ('model = "user-change"', 'model = "gpt-6-astra" # user-comment'):
            with self.subTest(changed=changed):
                # Separate fixture state because a conflict intentionally stays active.
                if self.path.exists():
                    self.tearDown()
                    self.setUp()
                self.write(b'model = "prior"\n')
                result = self.apply()
                text = self.path.read_text().replace('model = "gpt-6-astra"', changed)
                self.write(text.encode())
                with self.assertRaisesRegex(ProtocolError, 'restore_owned_(value|syntax)_conflict'):
                    cfg.restore(self.state, result['transaction_id'], confirm=True)
                self.assertEqual(self.path.read_text(), text)
                self.assertEqual(cfg.status(self.state)['active_transaction']['phase'], 'committed')

    def test_new_file_restore_deletes_only_unchanged_owned_file(self):
        result = self.apply()
        self.assertTrue(self.path.exists())
        cfg.restore(self.state, result['transaction_id'], confirm=True)
        self.assertFalse(self.path.exists())
        self.assertFalse(cfg.restore(self.state, result['transaction_id'], confirm=True)['write_performed'])

    def test_exact_restore_needs_neither_credentials_nor_tomlkit(self):
        self.write(b'model = "prior"\n')
        result = self.apply()
        with mock.patch.object(tx, 'parser', side_effect=AssertionError('parser must not be needed')):
            cfg.restore(self.state, result['transaction_id'], confirm=True)
        self.assertEqual(self.path.read_bytes(), b'model = "prior"\n')

    def test_apply_crash_before_write_reconciles_aborted(self):
        self.write(b'model = "prior"\n')
        def crash():
            raise RuntimeError('simulated crash')
        with self.assertRaisesRegex(RuntimeError, 'simulated crash'):
            self.apply(before_commit=crash)
        tid = self.active_id()
        self.assertEqual(cfg.reconcile(self.state, tid)['phase'], 'aborted')
        self.assertEqual(self.path.read_bytes(), b'model = "prior"\n')
        self.assertEqual(self.apply()['phase'], 'committed')

    def test_apply_crash_after_replace_reconciles_committed(self):
        self.write(b'model = "prior"\n')
        def crash():
            raise RuntimeError('simulated crash')
        with self.assertRaisesRegex(RuntimeError, 'simulated crash'):
            self.apply(after_replace=crash)
        tid = self.active_id()
        self.assertEqual(cfg.reconcile(self.state, tid)['phase'], 'committed')
        cfg.restore(self.state, tid, confirm=True)
        self.assertEqual(self.path.read_bytes(), b'model = "prior"\n')

    def test_restore_crash_before_write_is_resumable(self):
        self.write(b'model = "prior"\n')
        tid = self.apply()['transaction_id']
        def crash():
            raise RuntimeError('simulated crash')
        with self.assertRaises(RuntimeError):
            cfg.restore(self.state, tid, confirm=True, before_commit=crash)
        self.assertEqual(cfg.reconcile(self.state, tid)['phase'], 'committed')
        cfg.restore(self.state, tid, confirm=True)
        self.assertEqual(self.path.read_bytes(), b'model = "prior"\n')

    def test_restore_crash_after_replace_reconciles_restored(self):
        self.write(b'model = "prior"\n')
        tid = self.apply()['transaction_id']
        def crash():
            raise RuntimeError('simulated crash')
        with self.assertRaises(RuntimeError):
            cfg.restore(self.state, tid, confirm=True, after_replace=crash)
        self.assertEqual(cfg.reconcile(self.state, tid)['phase'], 'restored')
        self.assertEqual(self.path.read_bytes(), b'model = "prior"\n')

    def test_symlink_and_remote_origin_rejected(self):
        alternate = self.root / 'alternate'
        alternate.write_text('model = "untouched"\n')
        self.path.symlink_to(alternate)
        with self.assertRaisesRegex(ProtocolError, 'unsafe_config_file'):
            cfg.preview(self.state, self.home, self.info)
        self.path.unlink()
        for url in ('http://localhost:123/v1', 'https://remote.example/v1', 'http://127.0.0.1:55/v1?token=x'):
            with self.assertRaisesRegex(ProtocolError, 'direct_loopback_base_url_required'):
                cfg.preview(self.state, self.home, dict(self.info, base_url=url))

    def test_crlf_roundtrip(self):
        original = b'# original\r\nmodel = "prior"\r\nweb_search = "disabled"\r\n'
        self.write(original)
        result = self.apply()
        self.assertNotIn(b'\n', self.path.read_bytes().replace(b'\r\n', b''))
        cfg.restore(self.state, result['transaction_id'], confirm=True)
        self.assertEqual(self.path.read_bytes(), original)

    def test_catalog_is_immutable_and_multiple_pairs_are_exact(self):
        pairs = [self.pair, {'model': 'gpt-6-astra', 'reasoning_effort': 'high'},
                 {'model': 'gpt-6.1-sol', 'reasoning_effort': 'xhigh'}]
        value = cfg.catalog_for_pairs(pairs, self.pair)
        self.assertEqual(len(value['models']), 2)
        first = value['models'][0]
        self.assertEqual(first['default_reasoning_level'], 'xhigh')
        self.assertEqual([x['effort'] for x in first['supported_reasoning_levels']], ['xhigh', 'high'])
        self.assertTrue(first['supports_search_tool'])
        self.assertEqual(first['input_modalities'], ['text', 'image'])
        self.assertFalse(first['supports_reasoning_effort_updates'])
        before = self.catalog_path.read_bytes()
        with self.assertRaisesRegex(ProtocolError, 'direct_catalog_selection_mismatch'):
            cfg.write_catalog(self.catalog_path, pairs, self.pair)
        self.assertEqual(self.catalog_path.read_bytes(), before)
        with self.assertRaisesRegex(ProtocolError, 'max_effort_removed'):
            cfg.catalog_for_pairs([{'model': 'gpt-6-astra', 'reasoning_effort': 'max'}], self.pair)


if __name__ == '__main__':
    unittest.main()
