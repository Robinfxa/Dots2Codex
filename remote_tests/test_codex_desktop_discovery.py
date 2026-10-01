"""Offline synthetic app fixtures. Apple signatures are stubbed, never claimed real."""
import copy
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from remote_transport import codex_desktop as app
from remote_transport import global_desktop as desktop
from remote_transport.mac_launcher import default_config
from remote_transport.model import ProtocolError, canonical, hash_bytes
from remote_transport.selection import select, load_catalog


def make_app(root, name='Codex.app', *, version='26.930.1', executable='Codex', bundle_id=app.APP_ID):
    bundle = root / name
    (bundle / 'Contents/MacOS').mkdir(parents=True)
    (bundle / 'Contents/_CodeSignature').mkdir()
    (bundle / 'Contents/_CodeSignature/CodeResources').write_bytes(b'synthetic signed resource map')
    info = {'CFBundleIdentifier':bundle_id,'CFBundleExecutable':executable}
    if version is not None: info['CFBundleShortVersionString'] = version
    (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    binary = bundle / 'Contents/MacOS' / executable
    binary.write_bytes(b'SYNTHETIC MAIN EXECUTABLE; DO NOT RUN'); binary.chmod(0o700)
    return bundle


class UI:
    def __init__(self): self.messages=[]; self.choices=[]; self.confirmations=[]
    def notify(self, message): self.messages.append(message)
    def choose(self, message, choices): self.choices.append((message,choices)); return choices[0]
    def confirm(self, message): self.confirmations.append(message); return False
    def file(self, *args): raise AssertionError('No file picker is allowed')
    def folder(self, *args): raise AssertionError('No folder picker is allowed')


class AppDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name); self.calls=[]; self.registered=[]; self.signature_ok=True
        self.ui=UI()
    def commands(self, command, **kwargs):
        self.calls.append(command)
        if command[0]=='/usr/bin/codesign': return SimpleNamespace(returncode=0 if self.signature_ok else 1,stdout='')
        if command[0]=='/usr/bin/mdfind': return SimpleNamespace(returncode=0,stdout='\0'.join(map(str,self.registered)))
        if command[0]=='/usr/bin/osascript': return SimpleNamespace(returncode=0,stdout='')
        self.fail('Unexpected command: '+str(command))
    def discover(self, **kwargs):
        return app.discover_application(home=self.home,run=self.commands,platform='darwin',ui=self.ui,**kwargs)
    def evidence(self, bundle): return app.application_evidence(bundle,run=self.commands,platform='darwin')

    def test_common_user_app_without_internal_engine_is_detected(self):
        bundle=make_app(self.home/'Applications'); result=self.discover()
        self.assertEqual(result['path'],str(bundle)); self.assertEqual(result['app_version'],'26.930.1')
        self.assertIsNone(result['engine_version']); self.assertFalse(result['compatibility_verified'])
        self.assertEqual(self.ui.choices,[])
        self.assertFalse((bundle/'Contents/Resources/codex').exists())
        self.assertTrue(all(command[0].startswith('/usr/bin/') for command in self.calls))
        self.assertFalse(any('--version' in command for command in self.calls))

    def test_registered_moved_and_renamed_app_uses_actual_metadata(self):
        bundle=make_app(self.home/'Moved tools','My Desktop.app',executable='ChatGPT');self.registered=[bundle]
        self.assertEqual(self.discover()['path'],str(bundle))
    def test_duplicate_metadata_results_do_not_ask(self):
        bundle=make_app(self.home/'Applications');self.registered=[bundle,bundle]
        self.discover();self.assertEqual(self.ui.choices,[])
    def test_multiple_apps_show_only_app_names_locations_and_versions(self):
        first=make_app(self.home/'Applications');second=make_app(self.home/'Other',version='27.1')
        self.registered=[second];result=self.discover()
        self.assertEqual(result['path'],str(second));self.assertEqual(len(self.ui.choices),1)
        self.assertIn(str(first),self.ui.choices[0][1][1]);self.assertIn('app 27.1',self.ui.choices[0][1][0])
    def test_custom_explicit_app_avoids_search(self):
        bundle=make_app(self.home/'private tools');self.discover(explicit=bundle)
        self.assertEqual({command[0] for command in self.calls},{'/usr/bin/codesign'})
    def test_nondarwin_fails_without_search(self):
        with self.assertRaisesRegex(ProtocolError,'requires_macos'):
            app.discover_application(home=self.home,run=self.commands,platform='linux')
        self.assertEqual(self.calls,[])
    def test_missing_app_fails_without_file_picker(self):
        with self.assertRaisesRegex(ProtocolError,'not_found'):self.discover()
        self.assertIn('--desktop-app',self.ui.messages[0]);self.assertNotIn('Select the installed',self.ui.messages[0])
    def test_unknown_version_is_not_replaced_by_cli_version(self):
        bundle=make_app(self.home,version=None)
        with self.assertRaisesRegex(ProtocolError,'version_unavailable'):self.discover(explicit=bundle)
        self.assertEqual(self.calls,[])
    def test_spoofed_bundle_identifier_rejected_before_signature_or_code(self):
        bundle=make_app(self.home,bundle_id='attacker.codex')
        with self.assertRaisesRegex(ProtocolError,'identity_mismatch'):self.discover(explicit=bundle)
        self.assertEqual(self.calls,[])
    def test_spoofed_signer_rejected(self):
        bundle=make_app(self.home);self.signature_ok=False
        with self.assertRaisesRegex(ProtocolError,'signature_not_verified'):self.discover(explicit=bundle)
        self.assertIn(app.TEAM_ID,self.calls[0][-2]);self.assertIn('anchor apple',self.calls[0][-2])
    def test_app_symlink_is_not_followed(self):
        bundle=make_app(self.home/'original');link=self.home/'Linked.app';link.symlink_to(bundle)
        with self.assertRaisesRegex(ProtocolError,'symlink'):self.discover(explicit=link)
    def test_metadata_and_executable_symlinks_are_rejected(self):
        bundle=make_app(self.home);info=bundle/'Contents/Info.plist';other=self.home/'other.plist'
        other.write_bytes(info.read_bytes());info.unlink();info.symlink_to(other)
        with self.assertRaisesRegex(ProtocolError,'symlink'):self.evidence(bundle)
    def test_executable_cannot_escape_bundle(self):
        bundle=make_app(self.home);info=bundle/'Contents/Info.plist';value=plistlib.loads(info.read_bytes())
        value['CFBundleExecutable']='../../arbitrary';info.write_bytes(plistlib.dumps(value))
        with self.assertRaisesRegex(ProtocolError,'invalid_executable'):self.evidence(bundle)
    def test_signature_fingerprint_change_invalidates_evidence(self):
        bundle=make_app(self.home);evidence=self.evidence(bundle)
        (bundle/'Contents/_CodeSignature/CodeResources').write_bytes(b'changed signed resource map')
        with self.assertRaisesRegex(ProtocolError,'evidence_changed'):
            app.check_application(evidence,run=self.commands,platform='darwin')
    def test_changed_app_version_invalidates_evidence(self):
        bundle=make_app(self.home);evidence=self.evidence(bundle);path=bundle/'Contents/Info.plist'
        value=plistlib.loads(path.read_bytes());value['CFBundleShortVersionString']='27.1';path.write_bytes(plistlib.dumps(value))
        with self.assertRaisesRegex(ProtocolError,'evidence_changed'):
            app.check_application(evidence,run=self.commands,platform='darwin')
    def test_app_replaced_during_signature_check_fails(self):
        bundle=make_app(self.home)
        def race(command,**kwargs):
            (bundle/'Contents/MacOS/Codex').write_bytes(b'replaced')
            return SimpleNamespace(returncode=0,stdout='')
        with self.assertRaisesRegex(ProtocolError,'changed_during_read'):
            app.application_evidence(bundle,run=race,platform='darwin')
    def test_timeout_cannot_claim_verified_signature(self):
        bundle=make_app(self.home)
        with patch.object(app.subprocess,'run',side_effect=subprocess.TimeoutExpired('codesign',30)):
            with self.assertRaisesRegex(ProtocolError,'signature_check_unavailable'):
                app.application_evidence(bundle,platform='darwin')
    def test_global_normal_flow_reaches_consent_without_engine_or_picker(self):
        bundle=make_app(self.home/'Applications');home=self.home/'.codex';home.mkdir(mode=0o700)
        binary=self.home/'terminal-codex';binary.write_bytes(b'fixture');binary.chmod(0o700)
        cfg=default_config('folder',self.home/'credential',self.home,binary)
        cfg['model_selection']=select(load_catalog(),'gpt-6.1-sol','high');raw=canonical(cfg)
        config=self.home/'router.json';desktop.private_write(config,raw)
        launcher=desktop.DesktopGlobal(root=self.home,state=self.home/'launcher',config=config,ui=self.ui)
        args=SimpleNamespace(expected_config_sha256=hash_bytes(raw),codex_home=str(home),desktop_codex=None,
                             desktop_app=None,model=None,effort=None,catalog=None)
        with patch.object(desktop.config_tx,'parser'),patch.object(launcher.ports,'binary',return_value={'version':desktop.config_tx.CODEX_VERSION}),patch.object(launcher.ports,'desktop_app',side_effect=lambda explicit,ui:self.discover(explicit=explicit)):
            with self.assertRaises(desktop.Cancelled):launcher.start(args)
        self.assertIn('CODEX_HOME: '+str(home),self.ui.confirmations[0])
        self.assertFalse(launcher.state.exists());self.assertEqual(list(home.iterdir()),[])


if __name__=='__main__':unittest.main()
