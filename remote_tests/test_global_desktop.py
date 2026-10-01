"""Desktop lifecycle fixtures only; no Google, native, auth, or user config."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from remote_transport import global_desktop as desktop
from remote_transport import global_config as config
from remote_transport import global_pilot as pilot
from remote_transport.mac_launcher import default_config
from remote_transport.global_gateway import Store, private_dir
from remote_transport.global_fixture import unused_fixture_port
from remote_transport.model import ProtocolError, hash_bytes, canonical
from remote_transport.selection import select, load_catalog


class UI:
    def __init__(self, confirms=True): self.confirms=confirms; self.messages=[]
    def confirm(self, message): self.messages.append(message); return self.confirms
    def notify(self, message): self.messages.append(message)
    def choose(self, message, choices): return 'Status'


class DeadPorts:
    def owned(self, active): return False
    def alive(self, active): return False
    def now(self): return 123456.0


class DesktopGlobalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.state=private_dir(self.root/'desktop', create=True)
        self.home=private_dir(self.root/'codex-home', create=True)
        self.ui=UI(); self.app=desktop.DesktopGlobal(root=self.root,state=self.state,
            config=self.root/'config.json',ui=self.ui,ports=DeadPorts())
    def tearDown(self): self.tmp.cleanup()
    def active(self, status=None):
        rid='a'*32; runtime=private_dir(self.state/'runs'/rid, create=True)
        store,generation=Store.initialize(runtime/'gateway',select(load_catalog(),'gpt-6.1-sol','high'),port=unused_fixture_port())
        spec={'contract':desktop.CONTRACT,'runtime':str(runtime),'run_id':rid,'generation':generation,'codex_home':str(self.home)}
        desktop.save(runtime/'spec.json',spec)
        desktop.save(self.app.current,{'contract':desktop.CONTRACT,'runtime':str(runtime),'run_id':rid,
            'facade_pid':None,'facade_identity':None})
        if status:desktop.save(runtime/'status.json',{'stage':status})
        return runtime,store,generation
    def transaction(self,runtime,generation,phase='committed'):
        d=private_dir(runtime/'gateway'/'config-transactions',create=True); tid='b'*32
        before=b'# original\nmodel = "old"\n';after=b'config written before marker crash\n'
        desktop.private_write(self.home/'config.toml',after)
        desktop.private_write(d/(tid+'.before'),before);desktop.private_write(d/(tid+'.after'),after)
        value={'contract':config.CONTRACT,'id':tid,'phase':phase,'generation':generation,
            'config_path':str(self.home/'config.toml'),'codex_home':str(self.home),
            'backup':str(d/(tid+'.before')),'postimage':str(d/(tid+'.after')),
            'before_hash':hash_bytes(before),'after_hash':hash_bytes(after),'before_exists':True}
        desktop.save(d/(tid+'.json'),value);return before,value

    def test_recovery_only_open_supervised_activation_can_continue(self):
        runtime,store,_=self.active('WAITING_CONTROLLER');active=self.app.active()
        active['spec']['expires']=self.app.ports.now()+1800
        base={'stage':'WAITING_CONTROLLER','supervisor_alive':True,'queue_closed':False,'config_changed':False}
        with patch.object(self.app,'status',return_value=base):
            value=self.app._recovery(active,base)
            self.assertTrue(value['resume_possible']);self.assertFalse(value['new_activation_required'])
            for patch_status in ({'queue_closed':True},{'stage':'FAILED'},{'stage':'STOPPED'},
                                 {'supervisor_alive':False}):
                current={**base,**patch_status}
                with patch.object(self.app,'status',return_value=current):
                    value=self.app._recovery(active,current)
                    self.assertFalse(value['resume_possible']);self.assertTrue(value['new_activation_required'])
                    self.assertIn('new activation',value['next']);self.assertNotIn('same activation',value['next'])
            active['spec']['expires']=self.app.ports.now()
            self.assertTrue(self.app._recovery(active,base)['new_activation_required'])
        self.assertEqual(len(list((self.state/'runs').iterdir())),1)

    def test_missing_parser_blocks_before_google_or_local_runtime(self):
        with patch.object(config,'parser',side_effect=ProtocolError('global_config_requires_optional_tomlkit_dependency')):
            with self.assertRaisesRegex(ProtocolError,'tomlkit'):self.app.start(SimpleNamespace())
        self.assertFalse((self.state/'runs').exists());self.assertFalse(self.app.current.exists())

    def test_stop_dead_supervisor_fences_and_recovers_launch_intent(self):
        runtime,store,generation=self.active()
        value=self.app.stop();self.assertEqual(value['stage'],'STOPPED')
        self.assertFalse(value['native_children_stopped']);self.assertFalse(store.activation()['enabled'])
        self.assertTrue((runtime/'stop.json').exists());self.assertFalse(value['queue_closed'])

    def test_apply_marker_crash_recovers_from_transaction_and_restores(self):
        runtime,store,generation=self.active('PREFLIGHT_VERIFIED')
        before,_=self.transaction(runtime,generation)
        self.assertFalse((runtime/'config-applied.json').exists())
        self.assertTrue(self.app.status()['config_changed'])
        result=self.app.restore();self.assertEqual(result['phase'],'restored')
        self.assertEqual((self.home/'config.toml').read_bytes(),before)
        self.assertFalse(self.app.status()['config_changed'])

    def test_prepared_after_replace_crash_reconciles_before_restore(self):
        runtime,store,generation=self.active('FAILED')
        before,_=self.transaction(runtime,generation,'prepared')
        self.assertTrue(self.app.status()['config_recovery_required'])
        self.assertEqual(self.app.restore()['phase'],'restored')
        self.assertEqual((self.home/'config.toml').read_bytes(),before)

    def test_restore_cancel_leaves_config_and_journal_untouched(self):
        runtime,store,generation=self.active('STOPPED');_,record=self.transaction(runtime,generation)
        prior=(self.home/'config.toml').read_bytes();self.ui.confirms=False
        with self.assertRaises(desktop.Cancelled):self.app.restore()
        self.assertEqual((self.home/'config.toml').read_bytes(),prior)
        self.assertEqual(desktop.read(Path(record['backup']).with_suffix('.json'))['phase'],'committed')

    def test_owned_supervisor_record_reconciles_popen_pointer_crash(self):
        runtime,_,_=self.active()
        desktop.save(runtime/'supervisor.json',{'run_id':'a'*32,'facade_pid':123,'facade_identity':'actual-process-fingerprint'})
        # The child self-record is insufficient without current OS evidence.
        self.assertIsNone(self.app.active()['facade_pid'])
        with patch.object(self.app.ports,'owned',return_value=True):
            self.assertEqual(self.app.active()['facade_pid'],123)
        desktop.save(runtime/'supervisor.json',{'run_id':'c'*32,'facade_pid':123,'facade_identity':'x'})
        with self.assertRaisesRegex(ProtocolError,'identity_mismatch'):self.app.active()

    def test_launcher_identity_race_reconciles_only_same_current_pid(self):
        runtime,_,_=self.active()
        record=desktop.read(self.app.current);record.update(facade_pid=123,facade_identity='pre-exec')
        desktop.save(self.app.current,record)
        owned={'run_id':'a'*32,'facade_pid':123,'facade_identity':'post-exec'}
        desktop.save(runtime/'supervisor.json',owned)
        with patch.object(self.app.ports,'owned',side_effect=lambda value:value==owned):
            self.assertEqual(self.app.active()['facade_identity'],'post-exec')
        with patch.object(self.app.ports,'owned',return_value=False):
            self.assertEqual(self.app.active()['facade_identity'],'pre-exec')
        owned['facade_pid']=456;desktop.save(runtime/'supervisor.json',owned)
        with patch.object(self.app.ports,'owned',return_value=True):
            self.assertEqual(self.app.active()['facade_pid'],123)
            self.assertEqual(self.app.active()['facade_identity'],'pre-exec')
        # Status reconciliation never rewrites the durable launch intent.
        self.assertEqual(desktop.read(self.app.current),record)

    def test_recovery_rejects_alive_but_unowned_supervisor(self):
        runtime,_,_=self.active('PAUSED_DOCS_READ');active=self.app.active()
        active['spec']['expires']=self.app.ports.now()+1800
        current={'stage':'PAUSED_DOCS_READ','supervisor_alive':True,'supervisor_owned':False}
        with patch.object(self.app,'status',return_value=current):
            self.assertTrue(self.app._recovery(active,current)['new_activation_required'])

    def test_paused_stage_returns_recoverable_status_without_config_prompt(self):
        runtime,_,_=self.active('PAUSED_DOCS_READ');active=self.app.active()
        active['spec']['expires']=self.app.ports.now()+1800
        current={'stage':'PAUSED_DOCS_READ','supervisor_alive':True,'supervisor_owned':True,
                 'activation_enabled':True,'queue_closed':False}
        with patch.object(self.app,'status',return_value=current), patch.object(config,'preview') as preview:
            result=self.app._continue(active)
        self.assertTrue(result['resume_possible']);self.assertFalse(result['new_activation_required'])
        self.assertIn('exact read',result['next']);preview.assert_not_called();self.assertFalse(self.ui.messages)

    def test_unresolved_preflight_never_offers_config_or_claims_it_can_resume(self):
        runtime,_,_=self.active('PREFLIGHT_UNRESOLVED');active=self.app.active()
        active['spec']['expires']=self.app.ports.now()+1800
        current={'stage':'PREFLIGHT_UNRESOLVED','supervisor_alive':True,'supervisor_owned':True,
                 'activation_enabled':True,'queue_closed':False}
        with patch.object(self.app,'status',return_value=current), patch.object(config,'preview') as preview:
            result=self.app._continue(active)
        self.assertFalse(result['resume_possible']);self.assertTrue(result['new_activation_required'])
        self.assertIn('will not be replayed',result['next']);preview.assert_not_called()
        self.assertFalse(self.ui.messages)

    def test_binary_symlink_target_and_byte_replacement_are_detected(self):
        target=self.root/'codex-real';target.write_text('#!/bin/sh\necho "codex-cli 0.159.2"\n');target.chmod(0o700)
        link=self.root/'codex';link.symlink_to(target)
        evidence=desktop.binary_evidence(link);desktop.check_binary(evidence)
        other=self.root/'other';other.write_bytes(target.read_bytes());other.chmod(0o700)
        link.unlink();link.symlink_to(other)
        with self.assertRaisesRegex(ProtocolError,'binary_changed'):desktop.check_binary(evidence)

    def test_status_does_not_claim_config_or_desktop_ready_from_socket(self):
        self.active('WAITING_CONTROLLER')
        value=self.app.status();self.assertFalse(value['config_changed']);self.assertFalse(value['controller_active'])
        self.assertFalse(value['production_ready']);self.assertFalse(value['native_children_stopped'])

    def test_stop_during_unpublished_start_prevents_launch(self):
        binary=self.root/'codex';binary.write_text('fixture');binary.chmod(0o700)
        cfg=default_config('folder',self.root/'credential.json',self.root,binary)
        cfg['model_selection']=select(load_catalog(),'gpt-6.1-sol','high')
        raw=canonical(cfg);desktop.private_write(self.app.config,raw)
        args=SimpleNamespace(expected_config_sha256=hash_bytes(raw),codex_home=str(self.home),
            desktop_codex=str(binary),model=None,effort=None,catalog=None)
        original=Store.initialize;stops=[]
        def initialize(*a,**kw):
            value=original(*a,**kw);stops.append(self.app.stop());return value
        with patch.object(config,'parser'), patch.object(self.app.ports,'binary',create=True,return_value={'version':config.CODEX_VERSION}), patch.object(Store,'initialize',side_effect=initialize), patch.object(self.app.ports,'spawn',create=True) as spawn:
            with self.assertRaisesRegex(ProtocolError,'global_start_cancelled_by_stop'):self.app.start(args)
        self.assertEqual(stops[0]['stage'],'NOT_STARTED');spawn.assert_not_called()

    def test_expired_proof_refresh_timeout_never_opens_diff(self):
        self.active('PREFLIGHT_VERIFIED');active=self.app.active()
        active['spec'].update(cli={'version':config.CODEX_VERSION},desktop={'version':config.CODEX_VERSION})
        stale={'stage':'PREFLIGHT_VERIFIED','pilot_proof_id':'c'*32}
        with patch.object(self.app,'_wait_stage',return_value=stale), patch.object(pilot,'require_pilot',side_effect=ProtocolError('pilot_proof_expired')), patch.object(config,'preview') as preview, patch.object(config,'apply') as apply:
            self.app._continue(active)
        preview.assert_not_called();apply.assert_not_called();self.assertEqual(len(self.ui.messages),1)

    def test_edit_during_diff_consent_is_preserved(self):
        runtime,_,_=self.active('PREFLIGHT_VERIFIED');active=self.app.active()
        active['spec'].update(cli={'version':config.CODEX_VERSION},desktop={'version':config.CODEX_VERSION})
        path=self.home/'config.toml';before=b'model = "old"\n';after=b'model = "proposed"\n';edit=b'model = "user edit"\n'
        desktop.private_write(path,before)
        preview={'config_path':str(path),'diff':'model changed','before_hash':hash_bytes(before),'after_hash':hash_bytes(after)}
        def confirm(message):desktop.private_write(path,edit);return True
        with patch.object(self.app,'_wait_stage',return_value={'stage':'PREFLIGHT_VERIFIED','pilot_proof_id':'c'*32}), patch.object(self.app.ports,'owned',return_value=True), patch.object(pilot,'require_pilot',return_value={}), patch.object(desktop,'check_binary'), patch.object(config,'preview',return_value=preview), patch.object(config,'render_patch',return_value=after), patch.object(self.ui,'confirm',side_effect=confirm):
            with self.assertRaisesRegex(ProtocolError,'preview_outdated'):self.app._continue(active)
        self.assertEqual(path.read_bytes(),edit);self.assertFalse((runtime/'gateway'/'config-transactions').exists())


if __name__=='__main__':unittest.main()
