"""Known local configuration paths only; no Finder, real home or cloud effects."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from remote_transport import global_desktop as desktop
from remote_transport.global_gateway import private_dir, private_write
from remote_transport.mac_launcher import default_config
from remote_transport.model import ProtocolError, canonical, hash_bytes
from remote_transport.selection import select, load_catalog


class UI:
    def __init__(self): self.choices=[]; self.confirmations=[]; self.pick=0
    def choose(self,message,choices): self.choices.append((message,choices));return choices[self.pick]
    def folder(self,*args): raise AssertionError('normal Global startup must not open a folder chooser')
    def confirm(self,message): self.confirmations.append(message);return False


class HomeResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.home=private_dir(self.root/'user',create=True);self.ui=UI()
    def tearDown(self):self.tmp.cleanup()
    def directory(self,name):return private_dir(self.home/name,create=True)
    def resolve(self,**kwargs):
        return desktop.resolve_codex_home(home=self.home,environ={},ui=self.ui,**kwargs)

    def test_no_env_uses_existing_default_without_chooser(self):
        target=self.directory('.codex');self.assertEqual(self.resolve(),target);self.assertEqual(self.ui.choices,[])
    def test_empty_default_directory_is_valid_and_not_written(self):
        target=self.directory('.codex');self.assertEqual(list(target.iterdir()),[])
        self.assertEqual(self.resolve(),target);self.assertEqual(list(target.iterdir()),[])
    def test_valid_environment_without_default_is_automatic(self):
        target=self.directory('custom')
        self.assertEqual(desktop.resolve_codex_home(home=self.home,environ={'CODEX_HOME':str(target)},ui=self.ui),target)
        self.assertEqual(self.ui.choices,[])
    def test_empty_environment_uses_default(self):
        target=self.directory('.codex')
        self.assertEqual(desktop.resolve_codex_home(home=self.home,environ={'CODEX_HOME':''},ui=self.ui),target)
    def test_explicit_path_wins_conflicting_environment_and_saved(self):
        explicit=self.directory('explicit');saved=self.directory('saved');env=self.directory('environment');self.directory('.codex')
        self.assertEqual(desktop.resolve_codex_home(explicit=explicit,saved=saved,home=self.home,
            environ={'CODEX_HOME':str(env)},ui=self.ui),explicit);self.assertEqual(self.ui.choices,[])
    def test_saved_valid_selection_reused_without_environment(self):
        target=self.directory('saved');self.directory('.codex')
        self.assertEqual(self.resolve(saved=target),target);self.assertEqual(self.ui.choices,[])
    def test_saved_matches_environment_without_repeat_choice(self):
        target=self.directory('saved');self.directory('.codex')
        self.assertEqual(desktop.resolve_codex_home(saved=target,home=self.home,environ={'CODEX_HOME':str(target)},ui=self.ui),target)
        self.assertEqual(self.ui.choices,[])
    def test_stale_saved_path_falls_back_to_default(self):
        target=self.directory('.codex')
        self.assertEqual(self.resolve(saved=self.home/'removed'),target);self.assertEqual(self.ui.choices,[])
    def test_stale_saved_path_falls_back_to_valid_environment(self):
        target=self.directory('environment')
        self.assertEqual(desktop.resolve_codex_home(saved=self.home/'removed',home=self.home,
            environ={'CODEX_HOME':str(target)},ui=self.ui),target);self.assertEqual(self.ui.choices,[])
    def test_distinct_environment_and_default_require_path_choice(self):
        env=self.directory('environment');default=self.directory('.codex');self.ui.pick=1
        self.assertEqual(desktop.resolve_codex_home(home=self.home,environ={'CODEX_HOME':str(env)},ui=self.ui),default)
        self.assertEqual(len(self.ui.choices),1)
        self.assertEqual(self.ui.choices[0][1],[str(env)+' (CODEX_HOME)',str(default)+' (standard desktop location)'])
    def test_changed_environment_conflicts_with_saved_setting(self):
        saved=self.directory('saved');env=self.directory('environment');self.ui.pick=1
        self.assertEqual(desktop.resolve_codex_home(saved=saved,home=self.home,environ={'CODEX_HOME':str(env)},ui=self.ui),env)
        self.assertEqual(len(self.ui.choices),1)
    def test_equivalent_paths_do_not_create_false_conflicts(self):
        target=self.directory('.codex');self.directory('child');alias=self.home/'child'/'..'/'.codex'
        self.assertEqual(desktop.resolve_codex_home(home=self.home,environ={'CODEX_HOME':str(alias)},ui=self.ui),target)
        self.assertEqual(desktop.resolve_codex_home(saved=alias,home=self.home,environ={'CODEX_HOME':str(target)},ui=self.ui),target)
        self.assertEqual(self.ui.choices,[])
    def test_missing_or_relative_environment_never_silently_uses_default(self):
        self.directory('.codex')
        for value,error in [(str(self.home/'missing'),'environment_missing'),('relative-home','environment_must_be_absolute')]:
            with self.assertRaisesRegex(ProtocolError,error):desktop.resolve_codex_home(home=self.home,environ={'CODEX_HOME':value},ui=self.ui)
        self.assertEqual(self.ui.choices,[])
    def test_no_existing_home_does_not_create_directory(self):
        with self.assertRaisesRegex(ProtocolError,'missing_start_codex_once'):self.resolve()
        self.assertFalse((self.home/'.codex').exists());self.assertEqual(self.ui.choices,[])
    def test_symlink_saved_env_explicit_and_default_fail_closed(self):
        target=self.directory('target');link=self.home/'link';link.symlink_to(target)
        for kwargs in ({'saved':link},{'explicit':link}):
            with self.assertRaisesRegex(ProtocolError,'symlink'):self.resolve(**kwargs)
        with self.assertRaisesRegex(ProtocolError,'symlink'):desktop.resolve_codex_home(home=self.home,environ={'CODEX_HOME':str(link)},ui=self.ui)
        (self.home/'.codex').symlink_to(target)
        with self.assertRaisesRegex(ProtocolError,'symlink'):self.resolve()
    def test_unsafe_saved_path_does_not_fall_back(self):
        target=self.directory('saved');target.chmod(0o777);self.directory('.codex')
        with self.assertRaisesRegex(ProtocolError,'unsafe_codex_home'):self.resolve(saved=target)
    def test_global_start_happy_path_reaches_confirmation_without_folder_chooser(self):
        target=self.directory('.codex');binary=self.home/'codex';binary.write_text('fixture');binary.chmod(0o700)
        cfg=default_config('folder',self.home/'credential',self.home,binary)
        cfg['model_selection']=select(load_catalog(),'gpt-6.1-sol','high');raw=canonical(cfg)
        config=self.home/'router.json';private_write(config,raw)
        app=desktop.DesktopGlobal(root=self.root,state=self.home/'launcher',config=config,ui=self.ui)
        args=SimpleNamespace(expected_config_sha256=hash_bytes(raw),codex_home=None,desktop_codex=str(binary),
            model=None,effort=None,catalog=None)
        with patch.object(desktop.config_tx,'parser'),patch.object(Path,'home',return_value=self.home),patch.dict(os.environ,{},clear=True),patch.object(app.ports,'binary',return_value={'version':desktop.config_tx.CODEX_VERSION}):
            with self.assertRaises(desktop.Cancelled):app.start(args)
        self.assertEqual(self.ui.choices,[]);self.assertIn('CODEX_HOME: '+str(target),self.ui.confirmations[0])
        self.assertFalse(app.state.exists());self.assertEqual(list(target.iterdir()),[])


if __name__=='__main__':unittest.main()
