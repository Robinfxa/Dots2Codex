"""Independent real tomlkit config restore and stopped-old migration gates."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
REPO = Path(os.environ["LITE_REPO"]) if "LITE_REPO" in os.environ else next(parent for parent in ROOT.parents if (parent / "dots_lite").is_dir())
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(ROOT))
from dots_lite import config_transaction as tx, client_catalog as catalog, protocol as p
from dots_lite.launcher import legacy_inspection


class MigrationAudit(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.home=self.root/'codex-home';self.home.mkdir(mode=0o700)
        self.state=self.root/'state';self.state.mkdir(mode=0o700)
        self.info={'protocol':p.PROTOCOL,'generation':'a'*32,
                   'selection':catalog.select(catalog.load_catalog(),'gpt-6-astra','xhigh'),
                   'catalog_path':str(self.root/'catalog.json'),
                   'base_url':'http://127.0.0.1:44321/activations/'+'a'*32+'/v1'}
    def config(self,raw):
        f=self.home/'config.toml';f.write_bytes(raw);f.chmod(0o600);return f
    def apply(self):
        view=tx.preview(self.state,self.home,self.info)
        self.assertFalse(view['real_roundtrip_verified']);self.assertFalse(view['client_compatibility_verified'])
        return tx.apply(self.state,self.home,self.info,expected_before_hash=view['before_hash'],
            expected_after_hash=view['after_hash'],confirm=True,check_ready=lambda:None)['transaction_id']
    def save(self,path,value):
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700);path.write_text(json.dumps(value));path.chmod(0o600)

    def test_exact_restore_preserves_original_comments_crlf_and_model(self):
        raw=b'# original note\r\nmodel = "gpt-6.1-sol" # keep model\r\nmodel_reasoning_effort = "high"\r\n'
        file=self.config(raw);tid=self.apply();self.assertNotEqual(file.read_bytes(),raw)
        result=tx.restore(self.state,tid,confirm=True);self.assertEqual(result['phase'],'restored')
        self.assertEqual(file.read_bytes(),raw)

    def test_owned_restore_preserves_unrelated_concurrent_edit(self):
        file=self.config(b'# original\nmodel = "gpt-6.1-sol"\n');tid=self.apply()
        file.write_bytes(file.read_bytes()+b'\n[unrelated_extension]\ncolor = "green" # user edit\n')
        tx.restore(self.state,tid,confirm=True);raw=file.read_text()
        self.assertIn('model = "gpt-6.1-sol"',raw);self.assertIn('color = "green" # user edit',raw)
        self.assertNotIn('dots2codex_lite',raw)

    def test_owned_field_conflict_never_overwrites_user_change(self):
        file=self.config(b'model = "gpt-6.1-sol"\n');tid=self.apply()
        file.write_text(file.read_text().replace('model = "gpt-6-astra"','model = "user-edited-model"'))
        before=file.read_bytes()
        with self.assertRaises(p.ProtocolError):tx.restore(self.state,tid,confirm=True)
        self.assertEqual(file.read_bytes(),before)

    def test_absent_config_restore_removes_only_owned_created_file(self):
        tid=self.apply();self.assertTrue((self.home/'config.toml').exists())
        tx.restore(self.state,tid,confirm=True);self.assertFalse((self.home/'config.toml').exists())

    def test_unverified_old_queue_and_live_pid_block_new_activation(self):
        legacy=self.root/'legacy';active=self.root/'active.json'
        self.save(active,{'process_stopped':False,'closed':False})
        with patch('dots_lite.launcher.alive',return_value=False):
            result=legacy_inspection(legacy,active,self.home)
        self.assertFalse(result['safe_to_activate'])
        self.save(active,{'process_stopped':True,'closed':True,'facade_pid':43210})
        with patch('dots_lite.launcher.alive',return_value=True):self.assertFalse(legacy_inspection(legacy,active,self.home)['safe_to_activate'])
        with patch('dots_lite.launcher.alive',return_value=False):
            result=legacy_inspection(legacy,active,self.home)
        self.assertTrue(result['safe_to_activate']);self.assertFalse(result['native_children_stopped']);self.assertFalse(result['old_requests_migrated'])

    def test_old_unrestored_transaction_and_selected_provider_block_migration(self):
        legacy=self.root/'legacy';active=self.root/'absent-active.json';manifest=legacy/'global/runs/run1/gateway/config-transactions/tx.json'
        self.save(manifest,{'phase':'committed'})
        result=legacy_inspection(legacy,active,self.home);self.assertFalse(result['safe_to_activate'])
        self.save(manifest,{'phase':'restored'});self.config(b'model_provider="dots2codex_global"\n')
        result=legacy_inspection(legacy,active,self.home);self.assertIn('legacy_provider_still_selected',result['blockers'])


if __name__=='__main__':unittest.main(verbosity=2)
