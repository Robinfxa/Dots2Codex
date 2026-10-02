"""Offline v3 opt-in/config tests; all writes stay inside TemporaryDirectory."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from dots_lite import config_transaction as tx
from dots_lite.client_catalog import load_catalog, select
from dots_lite.launcher import (Launcher, CONTRACT, legacy_inspection, join_text,
                                stop_owned, resolve_codex_home, safe_error)
from dots_lite.private_io import private_dir, save, private_write
from dots_lite.protocol import ProtocolError, DEFAULT_LIMITS, PROTOCOL, grant_hash
from dots_lite.authorization import CONTRACT as AUTHORIZATION_CONTRACT, authorization_scope


class ConfigTransactions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = private_dir(self.root / 'home', create=True)
        self.state = private_dir(self.root / 'state', create=True)
        self.path = self.home / 'config.toml'
        self.info = {'protocol': PROTOCOL, 'generation': 'a'*32,
                     'selection': select(load_catalog(), 'gpt-6-astra', 'xhigh'),
                     'catalog_path': str(self.state / 'models.json'),
                     'base_url': 'http://127.0.0.1:43187/activations/' + 'a'*32 + '/v1'}
        self.assertEqual(tx.parser().__version__, '0.13.3')

    def tearDown(self): self.temp.cleanup()

    def write(self, raw):
        self.path.write_bytes(raw); self.path.chmod(0o600)

    def apply(self, **kwargs):
        plan = tx.preview(self.state, self.home, self.info)
        return tx.apply(self.state, self.home, self.info,
                        expected_before_hash=plan['before_hash'], expected_after_hash=plan['after_hash'],
                        confirm=True, check_ready=lambda: True, **kwargs)

    def test_missing_config_exact_restore(self):
        result = self.apply()
        self.assertTrue(self.path.exists())
        self.assertTrue(result['first_use_trial'])
        self.assertFalse(result['real_roundtrip_verified'])
        tx.restore(self.state, result['transaction_id'], confirm=True)
        self.assertFalse(self.path.exists())

    def test_crlf_comments_preserved(self):
        raw = b'# original\r\nmodel = "old" # keep\r\n[projects."/tmp/project"]\r\ntrust_level = "trusted"\r\n'
        self.write(raw)
        result = self.apply()
        patched = self.path.read_bytes()
        self.assertIn(b'# original\r\n', patched)
        self.assertNotIn(b'\n', patched.replace(b'\r\n', b''))
        tx.restore(self.state, result['transaction_id'], confirm=True)
        self.assertEqual(self.path.read_bytes(), raw)

    def test_unrelated_concurrent_edit_preserved(self):
        self.write(b'# keep\nmodel = "old"\n')
        result = self.apply()
        self.write(self.path.read_bytes()+b'\n[extra]\nflag = true # user edit\n')
        restored = tx.restore(self.state, result['transaction_id'], confirm=True)
        self.assertEqual(restored['mode'], 'three_way_owned_values')
        self.assertIn(b'flag = true # user edit', self.path.read_bytes())
        self.assertIn(b'model = "old"', self.path.read_bytes())

    def test_owned_value_conflict_stops(self):
        self.write(b'model="old"\n')
        result = self.apply()
        self.write(self.path.read_bytes().replace(b'gpt-6-astra', b'changed-model'))
        current = self.path.read_bytes()
        with self.assertRaisesRegex(ProtocolError, 'restore_owned_value_conflict'):
            tx.restore(self.state, result['transaction_id'], confirm=True)
        self.assertEqual(self.path.read_bytes(), current)

    def test_owned_comment_conflict_stops(self):
        result = self.apply()
        self.write(self.path.read_bytes().replace(b'model = "gpt-6-astra"', b'model = "gpt-6-astra" # new'))
        with self.assertRaisesRegex(ProtocolError, 'restore_owned_syntax_conflict'):
            tx.restore(self.state, result['transaction_id'], confirm=True)

    def test_concurrent_before_commit_preserves_editor(self):
        self.write(b'# before\n')
        with self.assertRaisesRegex(ProtocolError, 'config_concurrent_edit'):
            self.apply(before_commit=lambda: self.write(b'# user edit\n'))
        self.assertEqual(self.path.read_bytes(), b'# user edit\n')

    def test_crash_after_replace_reconciles(self):
        def crash(): raise RuntimeError('crash')
        with self.assertRaisesRegex(RuntimeError, 'crash'): self.apply(after_replace=crash)
        journal = next((self.state/'config-transactions').glob('*.json'))
        tid = journal.stem
        self.assertEqual(tx.reconcile(self.state, tid)['phase'], 'committed')
        tx.restore(self.state, tid, confirm=True)
        self.assertFalse(self.path.exists())

    def test_wrong_exact_after_hash_rejected(self):
        plan = tx.preview(self.state, self.home, self.info)
        with self.assertRaisesRegex(ProtocolError, 'preview_outdated'):
            tx.apply(self.state, self.home, self.info, expected_before_hash=plan['before_hash'],
                     expected_after_hash='0'*64, confirm=True, check_ready=lambda: True)
        self.assertFalse(self.path.exists())

    def test_no_preflight_gate_but_explicit_consent_and_live_check(self):
        plan = tx.preview(self.state, self.home, self.info)
        self.assertFalse(plan['real_roundtrip_verified'])
        kwargs = dict(expected_before_hash=plan['before_hash'],expected_after_hash=plan['after_hash'])
        with self.assertRaisesRegex(ProtocolError, 'explicit_first_use'):
            tx.apply(self.state, self.home, self.info, **kwargs)
        with self.assertRaisesRegex(ProtocolError, 'local_listener_recheck_required'):
            tx.apply(self.state, self.home, self.info, confirm=True, **kwargs)

    def test_home_replacement_rejected(self):
        result = self.apply()
        old = self.root/'old-home'
        self.home.rename(old); self.home.mkdir(mode=0o700)
        self.write((old/'config.toml').read_bytes())
        with self.assertRaisesRegex(ProtocolError, 'config_transaction_target_changed'):
            tx.restore(self.state, result['transaction_id'], confirm=True)

    def test_unmanaged_auth_provider_rejected(self):
        self.write(b'[model_providers.dots2codex_lightweight_v3]\nhttp_headers={Authorization="secret"}\n')
        with self.assertRaisesRegex(ProtocolError, 'existing_owned_provider_has_unmanaged_fields'):
            tx.preview(self.state, self.home, self.info)

    def test_symlink_home_rejected(self):
        alias = self.root/'alias'; alias.symlink_to(self.home)
        with self.assertRaisesRegex(ProtocolError, 'symlink_path_rejected'):
            tx.preview(self.state, alias, self.info)


class MigrationAndLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.legacy=private_dir(self.root/'legacy',create=True)
        self.old_active=self.root/'old-active.json'
        self.home=private_dir(self.root/'home',create=True)

    def tearDown(self): self.temp.cleanup()

    def test_clean_legacy_empty_is_safe(self):
        self.assertTrue(legacy_inspection(self.legacy,self.old_active,self.home)['safe_to_activate'])

    def test_old_unknown_or_alive_never_hot_migrates(self):
        save(self.old_active, {'facade_pid': None, 'stage': 'READY'})
        self.assertFalse(legacy_inspection(self.legacy,self.old_active)['safe_to_activate'])
        save(self.old_active, {'facade_pid': os.getpid(), 'closed': True, 'process_stopped': True})
        self.assertFalse(legacy_inspection(self.legacy,self.old_active)['safe_to_activate'])

    def test_old_unrestored_transaction_blocks(self):
        directory=private_dir(self.legacy/'global/runs/old/gateway/config-transactions',create=True)
        save(directory/'x.json', {'phase':'prepared','config_path':str(self.home/'config.toml')})
        self.assertIn('legacy_config_requires_restore_or_reconciliation',
                      legacy_inspection(self.legacy,self.old_active)['blockers'])

    def test_old_provider_still_selected_blocks(self):
        private_write(self.home/'config.toml', b'model_provider="dots2codex_global"\n')
        self.assertIn('legacy_provider_still_selected', legacy_inspection(self.legacy,self.old_active,self.home)['blockers'])

    def test_global_settings_do_not_require_workdir_or_rewrite_legacy(self):
        old=private_dir(self.root/'old',create=True)/'router.json'
        selected=select(load_catalog(),'gpt-6-astra','xhigh')
        config={'folder_id':'folder_123','authorized_user_file':str(self.root/'cred.json'),
                'workdir':'/missing/project/path','model_selection':selected, 'codex_home':str(self.home)}
        save(old,config); raw=old.read_bytes()
        launcher=Launcher(root=self.root,state=self.root/'v3',legacy_state=self.legacy,
            router_config=old,router_active=self.old_active)
        args=type('Args',(),dict(model=None,effort=None,credentials=None,folder_id=None,codex_home=None))()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings=launcher.settings(args)
        self.assertEqual(settings['selection'],selected)
        self.assertNotIn('workdir',settings)
        self.assertEqual(old.read_bytes(),raw)
        self.assertFalse((self.root/'v3/settings.json').exists())

    def test_unrecorded_v3_provider_cannot_be_stacked(self):
        old=private_dir(self.root/'old',create=True)/'router.json'
        selected=select(load_catalog(),'gpt-6-astra','xhigh')
        save(old, {'folder_id':'folder','authorized_user_file':str(self.root/'cred.json'),
                   'model_selection':selected,'codex_home':str(self.home)})
        private_write(self.home/'config.toml', b'model_provider="dots2codex_lightweight_v3"\n')
        launcher=Launcher(root=self.root,state=self.root/'v3',legacy_state=self.legacy,
                          router_config=old,router_active=self.old_active)
        args=type('Args',(),dict(model=None,effort=None,credentials=None,folder_id=None,codex_home=str(self.home)))()
        with self.assertRaisesRegex(ProtocolError,'selected_v3_provider_requires_recorded_restore'):
            launcher.settings(args)

    def test_native_max_not_coerced(self):
        with self.assertRaisesRegex(ProtocolError,'max_effort_removed'):
            select(load_catalog(),'gpt-6-astra','max')

    def test_never_signal_unowned_pid(self):
        with mock.patch('dots_lite.launcher.alive',return_value=True), mock.patch('dots_lite.launcher.owned',return_value=False), mock.patch('os.kill') as kill:
            with self.assertRaisesRegex(ProtocolError,'unowned_process'):
                stop_owned({'pid':123,'process_identity':'wrong'})
            kill.assert_not_called()

    def test_private_join_exact_marker_no_stdout(self):
        grant={'protocol':PROTOCOL,'activation_id':'a'*32,'folder_id':'folder','inbox_id':'inbox',
               'created_at':1,'expires_at':2,'allowed_pairs':[{'model':'gpt-6-astra','reasoning_effort':'xhigh'}],
               'limits':dict(DEFAULT_LIMITS),'package_sha256':'b'*64}
        output=io.StringIO()
        with contextlib.redirect_stdout(output): value=join_text(grant,'c'*64)
        self.assertEqual(output.getvalue(),'')
        marker,raw=value.split(' ',1)
        self.assertEqual(marker,'DOTS2CODEX_GLOBAL_JOIN_V3')
        self.assertEqual(json.loads(raw),{'activation_id':'a'*32,'inbox_id':'inbox','grant_sha256':grant_hash(grant),'join_code':'c'*64,
            'transport_authorization':authorization_scope(grant)})

    def test_v3_import_does_not_import_legacy_graph(self):
        result=subprocess.run([sys.executable,'-c',
            'import sys;import dots_lite.launcher;assert not any(k=="remote_transport" or k.startswith("remote_transport.") for k in sys.modules)'],
            cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_safe_errors_hide_credential_and_prompt(self):
        self.assertEqual(safe_error(RuntimeError('secret code: abc URL https://example.com')), 'RuntimeError')

    def test_home_defaults_no_workdir_prompt(self):
        base=self.root/'user';private_dir(base,create=True);private_dir(base/'.codex',create=True)
        self.assertEqual(resolve_codex_home(environ={},home=base),base/'.codex')


class SupervisorIntegration(unittest.TestCase):
    def setUp(self):
        from dots_lite.launcher import credential_check
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.state = private_dir(self.root / 'state', create=True)
        self.run_id = 'd'*32
        self.runtime = private_dir(self.state / 'runs' / self.run_id, create=True)
        self.credential = self.root / 'authorized-user.json'
        save(self.credential, {'type': 'authorized_user', 'client_id': 'fake', 'client_secret': 'fake',
            'refresh_token': 'fake', 'scopes': ['https://www.googleapis.com/auth/drive.file',
                                             'https://www.googleapis.com/auth/drive.readonly']})
        self.settings = {'selection': select(load_catalog(), 'gpt-6-astra', 'xhigh'),
            'authorized_user_file': str(self.credential), 'credential_evidence': credential_check(self.credential),
            'folder_id': 'folder', 'seconds': 14400, 'limits': dict(DEFAULT_LIMITS), 'wait_seconds': 900, 'port': 0}
        save(self.runtime / 'spec.json', {'contract': CONTRACT, 'run_id': self.run_id,
            'runtime': str(self.runtime), 'created_at': int(time.time()), 'package_sha256': 'b'*64,
            'transport_authorization_contract': AUTHORIZATION_CONTRACT, 'settings': self.settings})
        private_write(self.runtime / 'join-key', b'c'*64)
        from lite_tests.gateway_fixtures import FakeGoogle
        class Google(FakeGoogle):
            def get_metadata(self, file_id, **kwargs):
                if file_id == 'folder':
                    return {'id': file_id, 'mimeType': 'application/vnd.google-apps.folder', 'trashed': False}
                return super().get_metadata(file_id, **kwargs)
        self.google = Google()

    def tearDown(self): self.temp.cleanup()

    def wait_status(self, stage):
        until = time.monotonic() + 3
        while time.monotonic() < until:
            path = self.runtime / 'status.json'
            if path.exists():
                value = json.loads(path.read_bytes())
                if value.get('stage') == stage: return value
                if value.get('stage') == 'FAILED': self.fail(value)
            time.sleep(.02)
        self.fail('supervisor status timeout: ' + stage)

    def test_actual_supervisor_gateway_initialization_and_stop(self):
        from dots_lite.launcher import supervise, health
        with mock.patch('dots_lite.launcher.package_identity', return_value='b'*64), \
             mock.patch('dots_lite.launcher.signal.signal'), mock.patch.dict(os.environ):
            thread = threading.Thread(target=supervise, args=(self.runtime,),
                kwargs={'docs': self.google, 'drive': self.google}, daemon=True)
            thread.start()
            try:
                status = self.wait_status('LOCAL_READY')
                self.assertTrue(status['listener_ready'])
                self.assertFalse(status['first_round_trip_verified'])
                grant = json.loads((self.runtime / 'grant.json').read_bytes())
                self.assertEqual(grant['allowed_pairs'], [{'model': 'gpt-6-astra', 'reasoning_effort': 'xhigh'}])
                info = json.loads((self.runtime / 'config-info.json').read_bytes())
                self.assertEqual(health(info['base_url'])['activation_id'], self.run_id)
                self.assertEqual(len([c for c in self.google.calls if c[0]=='create_document']), 1)
                # No native child, synthetic request, or probe was manufactured.
                self.assertFalse([c for c in self.google.calls if c[0]=='upload'])
            finally:
                save(self.runtime / 'stop.json', {'activation_id': self.run_id})
                thread.join(5)
            self.assertFalse(thread.is_alive())
            status = self.wait_status('STOPPED')
            self.assertTrue(status['inbox_stop_published'])
            self.assertFalse(status['native_children_stopped'])
            self.assertFalse(status['native_notification_sent'])
            self.assertIn('"stop":true', self.google.texts[grant['inbox_id']])

    def test_expired_grant_cannot_authorize_new_config(self):
        grant={'protocol':PROTOCOL,'activation_id':self.run_id,'folder_id':'folder','inbox_id':'inbox',
               'created_at':1,'expires_at':2,'allowed_pairs':[{'model':'gpt-6-astra','reasoning_effort':'xhigh'}],
               'limits':dict(DEFAULT_LIMITS),'package_sha256':'b'*64}
        save(self.runtime/'grant.json',grant)
        ports=mock.Mock();ports.now.return_value=3
        launcher=Launcher(root=self.root,state=self.state,ports=ports)
        record={'runtime':str(self.runtime),'run_id':self.run_id,'pid':123,'process_identity':'identity'}
        with mock.patch('dots_lite.launcher.owned',return_value=True),mock.patch('dots_lite.launcher.alive',return_value=True):
            with self.assertRaisesRegex(ProtocolError,'grant_expired_or_clock_unproven'):
                launcher._ready(record)
        ports.health.assert_not_called()

    def test_unknown_inbox_create_is_not_repeated(self):
        from dots_lite.launcher import supervise
        save(self.runtime / 'inbox-creation.json', {'phase': 'creation_intent'})
        with mock.patch('dots_lite.launcher.package_identity', return_value='b'*64), \
             mock.patch('dots_lite.launcher.signal.signal'), mock.patch.dict(os.environ):
            supervise(self.runtime, docs=self.google, drive=self.google)
        status = json.loads((self.runtime/'status.json').read_bytes())
        self.assertEqual(status['error'], 'inbox_creation_outcome_unknown_no_automatic_retry')
        self.assertFalse([c for c in self.google.calls if c[0]=='create_document'])

    def test_resume_preserves_identity_and_resets_stale_readiness(self):
        private_dir(self.runtime/'gateway/mac',create=True)
        save(self.runtime/'grant.json', {'existing':'grant'})
        save(self.runtime/'bound-port.json', {'port':43187})
        save(self.runtime/'supervisor.json', {'run_id':self.run_id,'pid':111,'process_identity':'old'})
        save(self.runtime/'status.json', {'stage':'LOCAL_READY','listener_ready':True})
        save(self.state/'current.json', {'contract':CONTRACT,'run_id':self.run_id,'runtime':str(self.runtime),
             'pid':111,'process_identity':'old'})
        before={p.name:p.read_bytes() for p in [self.runtime/'grant.json',self.runtime/'join-key',self.runtime/'bound-port.json',self.runtime/'spec.json']}
        ui=mock.Mock();ui.confirm.return_value=True
        ports=mock.Mock();ports.package.return_value='b'*64
        def spawn(root,runtime):
            state=json.loads((runtime/'status.json').read_bytes())
            self.assertFalse(state['listener_ready']);self.assertEqual(state['stage'],'STARTING')
            return {'pid':222,'process_identity':'new'}
        ports.spawn.side_effect=spawn
        launcher=Launcher(root=self.root,state=self.state,ui=ui,ports=ports)
        with mock.patch('dots_lite.launcher.alive',return_value=False), mock.patch('dots_lite.launcher.owned',return_value=False):
            result=launcher.resume()
        self.assertTrue(result['resume_requested']);self.assertFalse(result['fresh_join_created'])
        self.assertFalse(result['listener_ready'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in [self.runtime/'grant.json',self.runtime/'join-key',self.runtime/'bound-port.json',self.runtime/'spec.json']})
        # Ambiguous process-launch outcome cannot issue a second spawn.
        ports.spawn.reset_mock()
        with mock.patch('dots_lite.launcher.alive',return_value=False), mock.patch('dots_lite.launcher.owned',return_value=False):
            with self.assertRaisesRegex(ProtocolError,'process_launch_outcome_unknown'):
                launcher.resume()
        ports.spawn.assert_not_called()

    def test_port_collision_preserves_evidence_without_listening(self):
        import socket
        from dots_lite.launcher import supervise
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1',0));occupied.listen()
            spec=json.loads((self.runtime/'spec.json').read_bytes())
            spec['settings']['port']=occupied.getsockname()[1];save(self.runtime/'spec.json',spec)
            with mock.patch('dots_lite.launcher.package_identity',return_value='b'*64), \
                 mock.patch('dots_lite.launcher.signal.signal'),mock.patch.dict(os.environ):
                supervise(self.runtime,docs=self.google,drive=self.google)
        status=json.loads((self.runtime/'status.json').read_bytes())
        self.assertEqual(status['stage'],'FAILED');self.assertFalse(status['listener_ready'])
        self.assertTrue((self.runtime/'grant.json').exists())
        self.assertFalse((self.runtime/'config-info.json').exists())

    def test_copy_join_requires_explicit_clipboard_consent(self):
        from dots_lite.launcher import join_text
        grant = {'protocol': PROTOCOL, 'activation_id': self.run_id, 'folder_id': 'folder', 'inbox_id': 'inbox',
            'created_at': int(time.time()), 'expires_at': int(time.time())+100,
            'allowed_pairs': [{'model': 'gpt-6-astra', 'reasoning_effort': 'xhigh'}],
            'limits': dict(DEFAULT_LIMITS), 'package_sha256': 'b'*64}
        save(self.runtime/'grant.json', grant)
        save(self.state/'current.json', {'contract':CONTRACT,'run_id':self.run_id,
            'runtime':str(self.runtime),'pid':None,'process_identity':None})
        ui=mock.Mock();ui.confirm.return_value=False
        ports=mock.Mock();ports.package.return_value='b'*64;ports.now.return_value=time.time()
        launcher=Launcher(root=self.root,state=self.state,ui=ui,ports=ports)
        from dots_lite.ui import Cancelled
        with mock.patch.object(launcher,'_ready',return_value={}):
            with self.assertRaises(Cancelled): launcher.copy_join()
        ports.copy.assert_not_called()
        ui.confirm.return_value=True
        with mock.patch.object(launcher,'_ready',return_value={}):
            out=launcher.copy_join()
        self.assertFalse(out['join_sent'])
        ports.copy.assert_called_once_with(join_text(grant,'c'*64))


if __name__=='__main__': unittest.main()
