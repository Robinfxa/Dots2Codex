"""Existing-install compatibility; installs are mocked, no network is used."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from direct_bridge import global_launcher as g
from direct_bridge import global_config as tx
from direct_bridge.global_credentials import Credentials
from dots_lite.private_io import private_dir, save

class UI:
    def __init__(self, accept=True): self.accept=accept; self.prompts=[]
    def confirm(self, text, word): self.prompts.append((text,word)); return self.accept

class ToolUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.state=private_dir(Path(self.temp.name)/'state',create=True)
    def tearDown(self): self.temp.cleanup()
    def private_python(self):
        binary=self.state/'.venv/bin/python';binary.parent.mkdir(parents=True);binary.write_text('fixture interpreter');binary.chmod(0o700)
        return binary
    def test_catalog_upgrade_uses_new_immutable_content_addressed_file(self):
        bridge=private_dir(self.state/'bridge',create=True)
        pair=tx.default_selection();save(bridge/'config.json',{'allowed_pairs':[pair]})
        old=tx.catalog_for_pairs([pair],pair);old['models'][0]['input_modalities']=['text'];old['models'][0]['supports_search_tool']=False
        oldpath=self.state/'codex-models.json';oldbytes=json.dumps(old).encode();oldpath.write_bytes(oldbytes);oldpath.chmod(0o600)
        launcher=g.Launcher(self.state);settings={'http_port':18765,'config_id':'fixture'}
        first=launcher._config_info(settings,Credentials('fixture-key','fixture-bearer-123456789'))
        self.assertNotEqual(Path(first['catalog_path']),oldpath)
        self.assertEqual(oldpath.read_bytes(),oldbytes)
        new=json.loads(Path(first['catalog_path']).read_text())
        self.assertTrue(new['models'][0]['supports_search_tool']);self.assertIn('image',new['models'][0]['input_modalities'])
        self.assertEqual(first,launcher._config_info(settings,Credentials('fixture-key','fixture-bearer-123456789')))
    def test_owned_private_upgrade_requires_explicit_consent(self):
        binary=self.private_python();ui=UI();marker=self.state/'credential.fixture';marker.write_bytes(b'preserve')
        with patch.object(g,'prerequisites',side_effect=[False,False,True]),patch.object(g.subprocess,'run') as run:
            g.ensure_private_dependencies(self.state,{'python':str(binary)},ui)
        self.assertEqual([p[1] for p in ui.prompts],['UPGRADE']);self.assertEqual(run.call_count,1)
        args=run.call_args.args[0];self.assertEqual(args[0],str(binary));self.assertIn('--isolated',args)
        self.assertIn('https://pypi.org/simple',args);self.assertEqual(marker.read_bytes(),b'preserve')
    def test_decline_upgrade_preserves_environment(self):
        binary=self.private_python();ui=UI(False)
        with patch.object(g,'prerequisites',return_value=False),patch.object(g.subprocess,'run') as run:
            with self.assertRaises(g.Cancelled):g.ensure_private_dependencies(self.state,{'python':str(binary)},ui)
        run.assert_not_called();self.assertEqual(binary.read_text(),'fixture interpreter')
    def test_upgrade_never_mutates_system_or_shared_python(self):
        with patch.object(g,'prerequisites',return_value=False),patch.object(g.subprocess,'run') as run:
            with self.assertRaisesRegex(ValueError,'owned_private_environment'):
                g.ensure_private_dependencies(self.state,{'python':'/usr/bin/python3'},UI())
        run.assert_not_called()
    def test_upgrade_requires_explicit_stop_of_existing_service(self):
        binary=self.private_python()
        with patch.object(g,'prerequisites',return_value=False),patch.object(g.Launcher,'records',return_value={'owner':{'pid':os.getpid()}}),patch.object(g.subprocess,'run') as run:
            with self.assertRaisesRegex(ValueError,'explicit_stop'):
                g.ensure_private_dependencies(self.state,{'python':str(binary)},UI())
        run.assert_not_called()
    def test_failed_upgrade_preserves_owned_environment(self):
        binary=self.private_python()
        with patch.object(g,'prerequisites',return_value=False),patch.object(g.subprocess,'run',side_effect=subprocess.CalledProcessError(1,'fixture')):
            with self.assertRaisesRegex(ValueError,'upgrade_failed_preserved'):
                g.ensure_private_dependencies(self.state,{'python':str(binary)},UI())
        self.assertTrue(binary.exists())
    def test_current_dependencies_require_no_confirmation_or_install(self):
        ui=UI()
        with patch.object(g,'prerequisites',return_value=True),patch.object(g.subprocess,'run') as run:
            g.ensure_private_dependencies(self.state,{'python':'/usr/bin/python3'},ui)
        run.assert_not_called();self.assertEqual(ui.prompts,[])
    def test_old_health_contract_is_not_ready(self):
        settings={'http_port':18765,'admin_port':18766,'config_id':'fixture'}
        health={'mode':'global','listener_ready':True,'instance_id':'run','config_id':'fixture'}
        with patch.object(g,'loopback_get',return_value=json.dumps(health).encode()):
            with self.assertRaisesRegex(ValueError,'tool_contract_upgrade_required'):
                g.Ports().ready(settings,'run','fixture-bearer-123456789')

if __name__=='__main__':unittest.main()
