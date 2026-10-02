"""Offline official ModelsResponse catalog generation and filesystem safety."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from remote_transport.codex_catalog import catalog_for_selection, write_catalog, validate_catalog
from remote_transport.selection import load_catalog, select
from remote_transport.global_gateway import global_catalog
from remote_transport.model import ProtocolError


class CodexCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.selection = select(load_catalog(),'gpt-6-astra','xhigh')
    def test_each_pair_has_only_bound_choice_and_plain_responses(self):
        for model,entry in load_catalog()['models'].items():
            for effort in entry['bridge_efforts']:
                with self.subTest(model=model,effort=effort):
                    catalog=catalog_for_selection(select(load_catalog(),model,effort))
                    self.assertEqual(len(catalog['models']),1)
                    item=catalog['models'][0]
                    self.assertEqual(item['slug'],model)
                    self.assertEqual(item['default_reasoning_level'],effort)
                    self.assertEqual([e['effort'] for e in item['supported_reasoning_levels']],[effort])
                    self.assertFalse(item['use_responses_lite']); self.assertFalse(item['supports_reasoning_effort_updates'])
                    self.assertEqual(item['input_modalities'],['text'])
                    self.assertNotIn('approval_policy',item);self.assertNotIn('sandbox_mode',item)
    def test_exported_global_choices_remove_max_and_preserve_explicit_defaults(self):
        catalog = load_catalog()
        for effort in ('low', 'medium', 'high', 'xhigh'):
            rows = global_catalog(select(catalog, 'gpt-6-astra', effort))['models']
            self.assertEqual(sum(len(row['supported_reasoning_levels']) for row in rows), 20)
            for row in rows:
                with self.subTest(default=effort, model=row['slug']):
                    self.assertEqual(row['default_reasoning_level'], effort)
                    self.assertEqual([entry['effort'] for entry in row['supported_reasoning_levels']],
                                     ['low', 'medium', 'high', 'xhigh'])
        self.assertTrue(all(entry['default_effort'] == 'xhigh' for entry in catalog['models'].values()))
    def test_pair_and_global_exports_do_not_accept_or_remap_max(self):
        invalid = {**self.selection, 'reasoning_effort': 'max'}
        for export in (catalog_for_selection, global_catalog):
            with self.assertRaisesRegex(ProtocolError, 'max_effort_removed_explicit_reselection_required'):
                export(invalid)
        self.assertEqual(invalid['reasoning_effort'], 'max')
    def test_original_instructions_preserved(self):
        from remote_transport.codex_catalog import INSTRUCTIONS_PATH
        self.assertEqual(catalog_for_selection(self.selection)['models'][0]['model_messages']['instructions_template'],
                         INSTRUCTIONS_PATH.read_text())
    def test_private_write_and_identical_resume(self):
        path=self.root/'catalog.json'
        result=write_catalog(path,self.selection)
        self.assertEqual(result,str(path.absolute()));self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertEqual(write_catalog(path,self.selection),result)
        self.assertEqual(validate_catalog(path,self.selection),result)
    def test_different_pair_never_overwrites(self):
        path=self.root/'catalog.json';write_catalog(path,self.selection);before=path.read_bytes()
        with self.assertRaises(ProtocolError):write_catalog(path,select(load_catalog(),'gpt-6-astra','low'))
        self.assertEqual(path.read_bytes(),before)
    def test_tamper_rejected(self):
        path=self.root/'catalog.json';write_catalog(path,self.selection)
        value=json.loads(path.read_bytes());value['models'][0]['use_responses_lite']=True
        path.write_text(json.dumps(value))
        with self.assertRaises(ProtocolError):validate_catalog(path,self.selection)
    def test_symlink_rejected_without_touching_target(self):
        target=self.root/'real';write_catalog(target,self.selection)
        link=self.root/'link';link.symlink_to(target);before=target.read_bytes()
        with self.assertRaises((ProtocolError,OSError)):write_catalog(link,self.selection)
        self.assertEqual(target.read_bytes(),before)
    def test_public_file_and_parent_rejected(self):
        path=self.root/'catalog.json';write_catalog(path,self.selection);path.chmod(0o644)
        with self.assertRaises(ProtocolError):validate_catalog(path,self.selection)
        directory=self.root/'public';directory.mkdir(mode=0o755);directory.chmod(0o755)
        with self.assertRaises(ProtocolError):write_catalog(directory/'catalog.json',self.selection)
