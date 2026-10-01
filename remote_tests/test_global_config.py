"""Temporary fixture CODEX_HOME only. Parser tests honestly skip if unavailable."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from remote_transport import global_config as config
from remote_transport.global_gateway import Store, Gateway, private_dir, private_write
from remote_transport.global_fixture import unused_fixture_port
from remote_transport.model import ProtocolError, canonical, hash_bytes
from remote_transport.selection import select, load_catalog

HAS_TOMLKIT=importlib.util.find_spec('tomlkit') is not None


class ConfigSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.home=private_dir(self.root/'fixture CODEX_HOME 中文',create=True)
        self.path=self.home/'config.toml';self.path.write_bytes(b'# original\nmodel = "old"\n');self.path.chmod(0o600)
        self.selected=select(load_catalog(),'gpt-6.1-sol','high')
        self.store,_=Store.initialize(self.root/'gateway',self.selected,port=unused_fixture_port())
        self.info=config.readiness(self.store.root)
    def tearDown(self):self.tmp.cleanup()
    def error(self,pattern,fn,*args,**kwargs):
        with self.assertRaisesRegex(ProtocolError,pattern):fn(*args,**kwargs)
    def manifest(self,before,after,phase='committed',exists=True):
        directory=private_dir(self.store.root/'config-transactions',create=True);tid='a'*32
        private_write(directory/(tid+'.before'),before);private_write(directory/(tid+'.after'),after)
        value={'contract':config.CONTRACT,'id':tid,'phase':phase,'config_path':str(self.path),'codex_home':str(self.home),
               'backup':str(directory/(tid+'.before')),'postimage':str(directory/(tid+'.after')),
               'before_hash':hash_bytes(before),'after_hash':hash_bytes(after),'before_exists':exists}
        private_write(directory/(tid+'.json'),canonical(value));return tid,value

    def test_snapshot_symlink_hardlink_and_unsafe_permissions(self):
        original=self.path.read_bytes();self.path.unlink();self.path.symlink_to(self.home/'missing')
        self.error('unsafe_config',config.snapshot,self.path)
        self.path.unlink();self.path.write_bytes(original);other=self.home/'hard';os.link(self.path,other)
        self.error('unsafe_config',config.snapshot,self.path);other.unlink();self.path.chmod(0o666)
        self.error('unsafe_config',config.snapshot,self.path)

    def test_explicit_home_no_symlink_or_recursive_discovery(self):
        link=self.root/'home-link';link.symlink_to(self.home)
        self.error('symlink',config.known_home,link);self.error('explicit_codex_home',config.known_home,'')

    def test_preview_version_guard_before_parser(self):
        self.error('unsupported_or_unverified_cli_version',config.preview,self.store.root,self.home,cli_version='codex-cli 999')
        self.assertEqual(self.path.read_bytes(),b'# original\nmodel = "old"\n')

    def test_dependency_missing_fails_closed(self):
        if HAS_TOMLKIT:self.skipTest('tomlkit present; parser integration covered separately')
        self.error('optional_tomlkit_dependency',config.preview,self.store.root,self.home,cli_version=config.CODEX_VERSION)
        self.assertFalse((self.home/'.dots2codex-global.lock').exists())

    def test_offline_gateway_cannot_apply_even_with_confirmation(self):
        with Gateway(self.store):
            self.error('production_gateway_not_ready',config.apply,self.store.root,self.home,
                       cli_version=config.CODEX_VERSION,expected_before_hash=hash_bytes(self.path.read_bytes()),confirm=True)
        self.assertFalse((self.home/'.dots2codex-global.lock').exists());self.assertFalse((self.store.root/'config-transactions').exists())

    def test_explicit_confirmation_required(self):
        self.error('confirmation_required',config.apply,self.store.root,self.home,cli_version=config.CODEX_VERSION,
                   expected_before_hash=hash_bytes(self.path.read_bytes()))
        self.error('confirmation_required',config.restore,self.store.root,'a'*32)

    def test_compare_before_atomic_replace_preserves_concurrent_edit(self):
        before=config.snapshot(self.path)
        self.error('config_concurrent_edit',config.commit,self.path,before,b'proposed',
                   before_commit=lambda:self.path.write_bytes(b'external edit'))
        self.assertEqual(self.path.read_bytes(),b'external edit')
        self.assertFalse(list(self.home.glob('.dots2codex-config-*')))

    def test_atomic_write_reread_and_auth_untouched(self):
        auth=self.home/'auth.json';auth.write_bytes(b'fixture auth bytes');auth.chmod(0o600)
        before=config.snapshot(self.path);written=config.commit(self.path,before,b'new bytes')
        self.assertEqual(written['raw'],b'new bytes');self.assertEqual(auth.read_bytes(),b'fixture auth bytes')
        self.assertEqual(self.path.stat().st_mode&0o777,0o600)

    def test_prepared_crash_after_replace_reconciles_without_rewrite(self):
        before=self.path.read_bytes();after=b'new committed bytes';tid,_=self.manifest(before,after,'prepared')
        snapshot=config.snapshot(self.path)
        with self.assertRaisesRegex(RuntimeError,'crash'):
            config.commit(self.path,snapshot,after,after_replace=lambda:(_ for _ in ()).throw(RuntimeError('crash')))
        before_reconcile=self.path.stat().st_ino
        self.assertEqual(config.reconcile(self.store.root,tid)['phase'],'committed')
        self.assertEqual(self.path.stat().st_ino,before_reconcile)

    def test_prepared_no_commit_aborts_and_unknown_edit_blocks(self):
        before=self.path.read_bytes();tid,_=self.manifest(before,b'after','prepared')
        self.path.write_bytes(b'other editor')
        self.error('manual_reconciliation',config.reconcile,self.store.root,tid)
        self.path.write_bytes(before);self.assertEqual(config.reconcile(self.store.root,tid)['phase'],'aborted')

    def test_exact_restore_preserves_original_raw_bytes_without_parser(self):
        before='# 中文\r\nmodel = "old"\r\n'.encode();after=b'after';tid,_=self.manifest(before,after)
        self.path.write_bytes(after);value=config.restore(self.store.root,tid,confirm=True)
        self.assertEqual(self.path.read_bytes(),before);self.assertEqual(value['mode'],'exact')
        self.assertFalse(config.restore(self.store.root,tid,confirm=True)['write_performed'])

    def test_restore_absent_versus_empty_original(self):
        tid,_=self.manifest(b'',b'after',exists=False);self.path.write_bytes(b'after')
        config.restore(self.store.root,tid,confirm=True);self.assertFalse(self.path.exists())
        tid,_=self.manifest(b'',b'after',exists=True);self.path.write_bytes(b'after')
        config.restore(self.store.root,tid,confirm=True);self.assertTrue(self.path.exists());self.assertEqual(self.path.read_bytes(),b'')

    def test_backup_tampering_blocks_restore(self):
        tid,value=self.manifest(b'before',b'after');self.path.write_bytes(b'after')
        Path(value['backup']).write_bytes(b'corrupt')
        self.error('backup_hash_mismatch',config.restore,self.store.root,tid,confirm=True)
        self.assertEqual(self.path.read_bytes(),b'after')

    def test_catalog_tampering_blocks_preview_before_write(self):
        Path(self.info['catalog_path']).write_bytes(b'{}')
        self.error('global_catalog_changed',config.readiness,self.store.root)


@unittest.skipUnless(HAS_TOMLKIT,'optional tomlkit unavailable; real parser integration NOT VERIFIED')
class ConfigParserIntegrationTests(unittest.TestCase):
    setUp=ConfigSafetyTests.setUp
    tearDown=ConfigSafetyTests.tearDown
    error=ConfigSafetyTests.error
    def fixture_apply(self):
        before_hash=hash_bytes(self.path.read_bytes())
        with patch.object(config,'readiness',return_value=self.info):
            return config.apply(self.store.root,self.home,cli_version=config.CODEX_VERSION,
                                expected_before_hash=before_hash,confirm=True)

    def test_comments_crlf_quoted_keys_inline_tables_and_unicode(self):
        before=('# 用户注释\r\nmodel = "old" # keep\r\n[mcp_servers."a b"]\r\n'
                'env = { X = "中文", Y = "x" }\r\n').encode()
        self.path.write_bytes(before);plan=config.preview(self.store.root,self.home,cli_version=config.CODEX_VERSION)
        self.assertFalse(plan['write_performed']);self.assertEqual(self.path.read_bytes(),before)
        applied=self.fixture_apply();after=self.path.read_bytes()
        self.assertIn(b'# keep',after);self.assertIn(b'\r\n',after);self.assertIn('env = { X = "中文", Y = "x" }'.encode(),after)
        config.restore(self.store.root,applied['transaction_id'],confirm=True);self.assertEqual(self.path.read_bytes(),before)

    def test_merge_restore_preserves_unrelated_new_value_and_comments(self):
        applied=self.fixture_apply();self.path.write_bytes(self.path.read_bytes()+b'\n[unrelated]\nanswer = 42 # added later\n')
        value=config.restore(self.store.root,applied['transaction_id'],confirm=True)
        self.assertEqual(value['mode'],'three_way_owned_values');self.assertIn(b'answer = 42 # added later',self.path.read_bytes())
        self.assertNotIn(b'dots2codex_global',self.path.read_bytes());self.assertIn(b'model = "old"',self.path.read_bytes())

    def test_owned_value_external_change_conflicts(self):
        applied=self.fixture_apply();self.path.write_bytes(self.path.read_bytes().replace(b'model_provider = "dots2codex_global"',b'model_provider = "other"'))
        current=self.path.read_bytes();self.error('restore_owned_value_conflict',config.restore,self.store.root,applied['transaction_id'],confirm=True)
        self.assertEqual(self.path.read_bytes(),current)

    def test_invalid_toml_and_non_table_provider_rejected(self):
        for raw in [b'broken = [',b'model_providers = "string"\n']:
            self.path.write_bytes(raw)
            with self.assertRaises(ProtocolError):config.preview(self.store.root,self.home,cli_version=config.CODEX_VERSION)
            self.assertEqual(self.path.read_bytes(),raw)

    def test_apply_outdated_preview_and_concurrent_editor(self):
        with patch.object(config,'readiness',return_value=self.info):
            self.error('preview_outdated',config.apply,self.store.root,self.home,cli_version=config.CODEX_VERSION,
                       expected_before_hash='0'*64,confirm=True)
            self.error('config_concurrent_edit',config.apply,self.store.root,self.home,cli_version=config.CODEX_VERSION,
                       expected_before_hash=hash_bytes(self.path.read_bytes()),confirm=True,
                       before_commit=lambda:self.path.write_bytes(b'# external concurrent\n'))
        self.assertEqual(self.path.read_bytes(),b'# external concurrent\n')

    def test_repeat_apply_requires_prior_reconciliation(self):
        self.fixture_apply()
        with patch.object(config,'readiness',return_value=self.info):
            self.error('already_matches',config.apply,self.store.root,self.home,cli_version=config.CODEX_VERSION,
                       expected_before_hash=hash_bytes(self.path.read_bytes()),confirm=True)

    def test_new_owned_scalar_comment_is_preserved_or_restore_conflicts(self):
        applied=self.fixture_apply();raw=self.path.read_bytes()
        raw=raw.replace(b'model = "gpt-6.1-sol"',b'model = "gpt-6.1-sol" # user-added scalar note')
        self.path.write_bytes(raw)
        try:config.restore(self.store.root,applied['transaction_id'],confirm=True)
        except ProtocolError:self.assertEqual(self.path.read_bytes(),raw)
        else:self.assertIn(b'user-added scalar note',self.path.read_bytes())

    def test_new_owned_provider_comment_is_preserved_or_restore_conflicts(self):
        applied=self.fixture_apply();raw=self.path.read_bytes()
        marker=b'[model_providers.dots2codex_global]'
        self.assertIn(marker,raw);raw=raw.replace(marker,marker+b'\n# user-added provider note')
        self.path.write_bytes(raw)
        try:config.restore(self.store.root,applied['transaction_id'],confirm=True)
        except ProtocolError:self.assertEqual(self.path.read_bytes(),raw)
        else:self.assertIn(b'user-added provider note',self.path.read_bytes())

    def test_existing_other_providers_profiles_and_safety_settings_preserved(self):
        raw=b'approval_policy = "on-request"\nsandbox_mode = "workspace-write"\n[model_providers.other]\nname = "Other"\n[profiles.legacy]\nmodel = "old"\n'
        self.path.write_bytes(raw);self.fixture_apply();after=self.path.read_bytes()
        self.assertIn(raw.split(b'[model_providers.other]')[1],after)
        self.assertIn(b'approval_policy = "on-request"',after);self.assertIn(b'sandbox_mode = "workspace-write"',after)

if __name__=='__main__':unittest.main()
