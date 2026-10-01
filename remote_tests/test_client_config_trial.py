"""Config-first gate/lifecycle fixtures; no installed app, real native or Google.

Binary observations and parser binding are explicitly stubbed. Config rendering
stubs here test transactions, not tomlkit. The nine real parser integration tests
remain dependency-based skips in test_global_config and are not relabeled passes.
"""
import copy
import os
from pathlib import Path
from types import SimpleNamespace
import tomllib
import unittest
from unittest.mock import patch

from remote_tests import test_global_pilot as strict_fixture
from remote_tests import test_desktop_app_trial as app_fixture
from remote_transport import codex_desktop as app
from remote_transport import global_desktop as desktop
from remote_transport import global_config as config, global_pilot as pilot
from remote_transport.global_gateway import private_dir, private_write
from remote_transport.mac_launcher import default_config
from remote_transport.model import ProtocolError, canonical, hash_bytes
from remote_transport.selection import load_catalog, select


class ClientConfigTrialTests(unittest.TestCase):
    error=strict_fixture.GlobalPilotTests.error
    path=strict_fixture.GlobalPilotTests.path
    execute=strict_fixture.GlobalPilotTests.execute
    demand=strict_fixture.GlobalPilotTests.demand
    native=strict_fixture.GlobalPilotTests.native
    child_admit=strict_fixture.GlobalPilotTests.child_admit
    turn=strict_fixture.GlobalPilotTests.turn
    prepare=strict_fixture.GlobalPilotTests.prepare
    complete=strict_fixture.GlobalPilotTests.complete
    verify=strict_fixture.GlobalPilotTests.verify
    tearDown=strict_fixture.GlobalPilotTests.tearDown
    add_client_route=app_fixture.DesktopAppTrialTests.add_client_route

    def setUp(self):
        strict_fixture.GlobalPilotTests.setUp(self)
        self.strict_versions=self.versions
        self.home=private_dir(self.root/'target codex home',create=True)
        self.config_path=self.home/'config.toml'
        private_write(self.config_path,b'# before config trial\n')
        self.versions=self.observe()

    def observe(self,home=None):
        with patch.object(pilot.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=config.CODEX_VERSION+'\n')):
            return pilot.observe_config_client(self.store,self.cli,home or self.home)

    def require(self,proof,**overrides):
        args={'cli_version':config.CODEX_VERSION,'desktop_version':None,
              'client_profile':pilot.CONFIG_TRIAL,'codex_home':self.home,**overrides}
        return pilot.require_pilot(self.store.root,proof['proof_id'],**args)

    def apply_args(self,proof):
        return {'cli_version':config.CODEX_VERSION,'desktop_version':None,'client_profile':pilot.CONFIG_TRIAL,
                'expected_before_hash':hash_bytes(self.config_path.read_bytes()),
                'expected_after_hash':hash_bytes(b'# after config trial\n'),
                'confirm':True,'pilot_proof_id':proof['proof_id']}

    def apply_trial(self):
        plan,_=self.complete();proof=self.verify(plan)
        with patch.object(config,'render_patch',return_value=b'# after config trial\n'):
            applied=config.apply(self.store.root,self.home,**self.apply_args(proof))
        _,transaction,_,_=config.load_transaction(self.store.root,applied['transaction_id'])
        return plan,proof,applied,transaction

    def signed_mutation(self,**fields):
        value={k:v for k,v in self.versions.items() if k not in ('contract','kind','mac')}
        value.update(fields)
        return pilot._save(self.store,'versions',value)

    def test_explicit_config_profile_binds_target_and_exact_catalog_adapter_without_app(self):
        self.assertEqual(self.versions['profile'],pilot.CONFIG_TRIAL)
        self.assertEqual(set(self.versions['binaries']),{'cli'})
        self.assertNotIn('desktop_app',self.versions)
        self.assertEqual(self.versions['config_target'],pilot.config_target(self.home))
        self.assertEqual(self.versions['catalog_adapter']['cli_version'],config.CODEX_VERSION)
        with patch.object(app,'check_application',side_effect=AssertionError('app gate forbidden')):
            plan,_=self.complete();proof=self.verify(plan);info=self.require(proof)
        self.assertTrue(info['pilot_ready']);self.assertFalse(info['production_ready'])
        self.assertFalse(info['client_compatibility_verified']);self.assertFalse(info['desktop_compatibility_verified'])

    def test_unknown_or_nonstring_profiles_are_rejected_even_when_signed(self):
        for profile in ('unknown/1',None,False,1,[],{}):
            with self.subTest(profile=profile):
                value=self.signed_mutation(profile=profile)
                self.error('unknown_evidence_profile',pilot.prepare_preflight,self.store,version_evidence=value)

    def test_missing_profile_cannot_downgrade_config_evidence_to_strict(self):
        value={k:v for k,v in self.versions.items() if k not in ('contract','kind','mac','profile')}
        evidence=pilot._save(self.store,'versions',value)
        self.error('evidence_profile_mismatch',pilot.prepare_preflight,self.store,version_evidence=evidence)

    def test_profile_tampering_without_seal_rejected(self):
        value=copy.deepcopy(self.versions);value['profile']='strict-client-binaries/1'
        self.error('signature',pilot.prepare_preflight,self.store,version_evidence=value)

    def test_mixed_app_or_desktop_evidence_rejected_even_when_signed(self):
        for field in ('desktop_app','desktop'):
            with self.subTest(field=field):
                value=self.signed_mutation(**{field:None})
                self.error('evidence_profile_mismatch',pilot.prepare_preflight,self.store,version_evidence=value)
        mixed=copy.deepcopy(self.versions['binaries']);mixed['desktop']=mixed['cli']
        value=self.signed_mutation(binaries=mixed)
        self.error('evidence_profile_mismatch',pilot.prepare_preflight,self.store,version_evidence=value)

    def test_config_proof_requires_explicit_matching_profile_and_target(self):
        plan,_=self.complete();proof=self.verify(plan)
        for changes in ({'client_profile':None},{'desktop_version':config.CODEX_VERSION},{'desktop_app':{}},
                        {'client_profile':'strict-client-binaries/1'}):
            with self.subTest(changes=changes):
                self.error('profile',self.require,proof,**changes)
        self.error('config_target_mismatch',self.require,proof,codex_home=None)

    def test_old_strict_proof_cannot_authorize_config_profile(self):
        self.versions=self.strict_versions;plan,_=self.complete();proof=self.verify(plan)
        self.error('requested_profile_mismatch',self.require,proof)
        self.assertTrue(pilot.require_pilot(self.store.root,proof['proof_id'],config.CODEX_VERSION,config.CODEX_VERSION)['pilot_ready'])

    def test_trial_without_proof_cannot_use_production_gate(self):
        self.error('requires_pilot_proof',config.apply,self.store.root,self.home,cli_version=config.CODEX_VERSION,
                   client_profile=pilot.CONFIG_TRIAL,expected_before_hash=hash_bytes(self.config_path.read_bytes()),confirm=True)
        self.assertFalse((self.home/'.dots2codex-global.lock').exists())

    def test_wrong_cli_version_and_replaced_binary_remain_blocked(self):
        with patch.object(pilot.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='codex-cli 999\n')):
            self.error('binary_version_mismatch',pilot.observe_config_client,self.store,self.cli,self.home)
        self.cli.write_bytes(b'changed binary');self.error('binary_evidence_changed',self.prepare)

    def test_catalog_adapter_cannot_be_substituted(self):
        evidence=self.signed_mutation(catalog_adapter={'cli_version':'codex-cli 999'})
        self.error('catalog_adapter_changed',pilot.prepare_preflight,self.store,version_evidence=evidence)

    def test_home_replacement_is_detected_before_preflight(self):
        self.home.rename(self.home.with_name('original-home'))
        private_dir(self.home,create=True)
        self.error('config_target_changed',self.prepare)

    def test_proof_cannot_be_reused_for_another_config_home(self):
        plan,_=self.complete();proof=self.verify(plan);other=private_dir(self.root/'other-home',create=True)
        self.error('config_target_mismatch',config.apply,self.store.root,other,**self.apply_args(proof))
        self.assertFalse((other/'.dots2codex-global.lock').exists())
        self.assertFalse((other/'config.toml').exists())

    def test_refresh_cannot_switch_profile_or_config_home(self):
        plan,_=self.complete();other=private_dir(self.root/'other-home',create=True)
        for versions in (self.strict_versions,self.observe(other)):
            self.error('refresh_evidence_changed',pilot.prepare_preflight,self.store,
                       version_evidence=versions,previous_plan_id=plan['plan_id'])

    def test_expired_proof_does_not_mutate_home(self):
        plan,_=self.complete();proof=self.verify(plan)
        with patch.object(pilot.time,'time',return_value=proof['expires']):
            self.error('proof_expired',config.apply,self.store.root,self.home,**self.apply_args(proof))
        self.assertFalse((self.home/'.dots2codex-global.lock').exists())

    def test_lost_controller_and_unverified_backend_are_rejected(self):
        with self.store.transaction() as db:db.execute("UPDATE controller SET mode='offline_fixture'")
        self.error('live_native_controller_required',self.prepare)

    def test_controller_lost_after_preflight_blocks_apply(self):
        plan,_=self.complete();proof=self.verify(plan)
        with self.store.transaction() as db:db.execute('UPDATE controller SET heartbeat=0,expires=0')
        self.error('live_native_controller_required',config.apply,self.store.root,self.home,**self.apply_args(proof))
        self.assertFalse((self.home/'.dots2codex-global.lock').exists())

    def test_backend_identity_absent_cannot_mint_proof(self):
        plan,_=self.complete()
        with self.store.transaction() as db:db.execute('UPDATE requests SET backend_response_id=NULL')
        self.error('backend_response_identity_required',self.verify,plan)

    def test_replaced_catalog_after_preflight_blocks_apply(self):
        plan,_=self.complete();proof=self.verify(plan)
        private_write(self.store.activation()['catalog'],b'{}')
        self.error('catalog_changed',config.apply,self.store.root,self.home,**self.apply_args(proof))
        self.assertFalse((self.home/'.dots2codex-global.lock').exists())

    def test_config_edit_after_preview_is_preserved(self):
        plan,_=self.complete();proof=self.verify(plan);args=self.apply_args(proof)
        self.config_path.write_bytes(b'# external edit\n')
        self.error('preview_outdated',config.apply,self.store.root,self.home,**args)
        self.assertEqual(self.config_path.read_bytes(),b'# external edit\n')

    def test_final_boundary_rechecks_controller_before_write(self):
        plan,_=self.complete();proof=self.verify(plan)
        def lost():
            with self.store.transaction() as db:db.execute('UPDATE controller SET heartbeat=0,expires=0')
        with patch.object(config,'render_patch',return_value=b'# after config trial\n'):
            self.error('live_native_controller_required',config.apply,self.store.root,self.home,
                       before_commit=lost,**self.apply_args(proof))
        self.assertEqual(self.config_path.read_bytes(),b'# before config trial\n')

    def test_cancelled_diff_and_absent_app_do_not_change_config(self):
        plan,_=self.complete();proof=self.verify(plan);messages=[]
        cli=desktop.binary_evidence(self.cli,run=lambda *a,**kw:SimpleNamespace(returncode=0,stdout=config.CODEX_VERSION+'\n'))
        ui=SimpleNamespace(confirm=lambda text:messages.append(text) or False)
        launcher=desktop.DesktopGlobal(root=self.root,state=self.root/'launcher',config=self.root/'router.json',ui=ui)
        active={'runtime':str(self.root),'spec':{'generation':self.store.activation()['id'],'codex_home':str(self.home),
                'cli':cli,'client_evidence_profile':pilot.CONFIG_TRIAL}}
        args=self.apply_args(proof);preview={'config_path':str(self.config_path),'diff':'owned safe diff',
            'before_hash':args['expected_before_hash'],'after_hash':args['expected_after_hash']}
        with patch.object(launcher,'_wait_stage',return_value={'stage':'PREFLIGHT_VERIFIED','pilot_proof_id':proof['proof_id']}), \
             patch.object(config,'preview',return_value=preview), \
             patch.object(app,'check_application',side_effect=AssertionError('no app gate')):
            with self.assertRaises(desktop.Cancelled):launcher._continue(active)
        self.assertIn(str(self.config_path),messages[0]);self.assertIn('0.159.2',messages[0])
        self.assertIn('Restore Global config',messages[0]);self.assertIn('compatibility is not established',messages[0])
        self.assertEqual(self.config_path.read_bytes(),b'# before config trial\n')
        self.assertFalse((self.home/'.dots2codex-global.lock').exists())

    def test_continue_applies_confirmed_diff_with_no_app_metadata(self):
        plan,_=self.complete();proof=self.verify(plan);messages=[]
        cli=desktop.binary_evidence(self.cli,run=lambda *a,**kw:SimpleNamespace(returncode=0,stdout=config.CODEX_VERSION+'\n'))
        launcher=desktop.DesktopGlobal(root=self.root,state=self.root/'launcher',config=self.root/'router.json',
            ui=SimpleNamespace(confirm=lambda text:messages.append(text) or True))
        active={'runtime':str(self.root),'spec':{'generation':self.store.activation()['id'],'codex_home':str(self.home),
                'cli':cli,'client_evidence_profile':pilot.CONFIG_TRIAL}}
        args=self.apply_args(proof);preview={'config_path':str(self.config_path),'diff':'owned safe diff',
            'before_hash':args['expected_before_hash'],'after_hash':args['expected_after_hash']}
        with patch.object(launcher,'_wait_stage',return_value={'stage':'PREFLIGHT_VERIFIED','pilot_proof_id':proof['proof_id']}), \
             patch.object(config,'preview',return_value=preview),patch.object(launcher.ports,'owned',return_value=True), \
             patch.object(config,'render_patch',return_value=b'# after config trial\n'), \
             patch.object(app,'check_application',side_effect=AssertionError('app gate forbidden')):
            result=launcher._continue(active)
        self.assertEqual(result['phase'],'committed');self.assertEqual(result['client_evidence_profile'],pilot.CONFIG_TRIAL)
        self.assertFalse(result['production_ready']);self.assertFalse(result['client_compatibility_verified'])
        self.assertTrue((self.root/'config-applied.json').exists());self.assertEqual(len(messages),1)

    def test_apply_and_exact_restore_leave_auth_untouched_and_unread(self):
        auth=self.home/'auth.json';private_write(auth,b'fixture secret')
        original=os.open
        def guarded(path,*args,**kwargs):
            if Path(path).name=='auth.json':raise AssertionError('auth file access forbidden')
            return original(path,*args,**kwargs)
        with patch.object(os,'open',side_effect=guarded):
            _,_,applied,transaction=self.apply_trial()
            self.assertEqual(transaction['client_evidence_profile'],pilot.CONFIG_TRIAL)
            self.assertEqual(transaction['config_target_evidence'],self.versions['config_target'])
            self.assertIsNone(transaction['desktop_app_evidence']);self.assertIsNone(transaction['desktop_version_evidence'])
            self.assertFalse(transaction['client_compatibility_verified'])
            result=config.restore(self.store.root,applied['transaction_id'],confirm=True)
        self.assertEqual(result['phase'],'restored');self.assertEqual(auth.read_bytes(),b'fixture secret')
        self.assertEqual(self.config_path.read_bytes(),b'# before config trial\n')

    def replace_home(self):
        prior=self.config_path.read_bytes();self.home.rename(self.home.with_name('moved-home'))
        private_dir(self.home,create=True);private_write(self.config_path,prior)
        return prior

    def test_restore_and_reconcile_reject_replaced_home_before_lock(self):
        _,_,applied,_=self.apply_trial();prior=self.replace_home()
        for operation in (config.restore,config.reconcile):
            kwargs={'confirm':True} if operation==config.restore else {}
            self.error('config_transaction_target_changed',operation,self.store.root,applied['transaction_id'],**kwargs)
            self.assertEqual(self.config_path.read_bytes(),prior)
            self.assertFalse((self.home/'.dots2codex-global.lock').exists())

    def test_restore_final_boundary_rechecks_home_identity(self):
        _,_,applied,_=self.apply_trial()
        self.error('config_transaction_target_changed',config.restore,self.store.root,applied['transaction_id'],
                   confirm=True,before_commit=self.replace_home)
        self.assertEqual(self.config_path.read_bytes(),b'# after config trial\n')
        self.assertEqual((self.home.with_name('moved-home')/'config.toml').read_bytes(),b'# after config trial\n')

    def test_prepared_transaction_cannot_be_bypassed_with_home_alias(self):
        plan,_=self.complete();proof=self.verify(plan);args=self.apply_args(proof)
        def crash():raise RuntimeError('synthetic before commit interruption')
        with patch.object(config,'render_patch',return_value=b'# after config trial\n'):
            with self.assertRaisesRegex(RuntimeError,'interruption'):
                config.apply(self.store.root,self.home,before_commit=crash,**args)
            alias=self.home/'..'/self.home.name
            self.assertEqual(config.known_home(alias),self.home)
            self.error('existing_config_transaction_requires_reconciliation',config.apply,self.store.root,alias,**args)
        directory=self.store.root/'config-transactions'
        self.assertEqual(len(list(directory.glob('*.json'))),1)
        self.assertEqual(self.config_path.read_bytes(),b'# before config trial\n')

    def test_legacy_alias_journal_restores_same_safe_target(self):
        _,_,applied,_=self.apply_trial();directory=self.store.root/'config-transactions'
        journal=directory/(applied['transaction_id']+'.json');value=desktop.read(journal)
        alias=self.home/'..'/self.home.name
        value.update(codex_home=str(alias),config_path=str(alias/'config.toml'))
        desktop.save(journal,value)
        result=config.restore(self.store.root,applied['transaction_id'],confirm=True)
        self.assertEqual(result['phase'],'restored');self.assertEqual(self.config_path.read_bytes(),b'# before config trial\n')
        saved=desktop.read(journal);self.assertEqual(saved['codex_home'],str(self.home))

    def test_restore_owned_conflict_preserves_user_edit(self):
        _,_,applied,_=self.apply_trial();self.config_path.write_bytes(b'model = "external"\n')
        # Plain parse stub is sufficient to exercise the value-conflict guard.
        with patch.object(config,'decoded',side_effect=lambda raw:({},tomllib.loads(raw.decode()))):
            self.error('restore_owned_value_conflict',config.restore,self.store.root,applied['transaction_id'],confirm=True)
        self.assertEqual(self.config_path.read_bytes(),b'model = "external"\n')

    def test_preview_redacts_unrelated_secrets(self):
        private_write(self.config_path,b'model = "old-secret-value"\napi_key = "unrelated-secret"\n')
        with patch.object(config,'render_patch',return_value=b'# preview fixture\n'), \
             patch.object(config,'decoded',side_effect=lambda raw:({},tomllib.loads(raw.decode()))):
            value=config.preview(self.store.root,self.home,cli_version=config.CODEX_VERSION,client_profile=pilot.CONFIG_TRIAL)
        self.assertNotIn('old-secret-value',str(value));self.assertNotIn('unrelated-secret',str(value))
        self.assertIn('<existing value hidden>',value['diff']);self.assertFalse(value['client_compatibility_verified'])

    def test_missing_or_unsafe_home_and_config_leave_status_non_positive(self):
        *_,transaction=self.apply_trial();self.add_client_route(transaction)
        self.home.rename(self.home.with_name('moved-home'))
        value=desktop.client_route_observation(self.root,transaction)
        self.assertFalse(value['client_route_observed'])
        self.assertEqual(value['client_observation_state'],'config_target_changed_revalidate')
        self.home.with_name('moved-home').rename(self.home);self.home.chmod(0o777)
        self.assertFalse(desktop.client_route_observation(self.root,transaction)['client_route_observed'])
        self.home.chmod(0o700);self.config_path.chmod(0o666)
        value=desktop.client_route_observation(self.root,transaction)
        self.assertFalse(value['client_route_observed'])
        self.assertEqual(value['client_observation_state'],'config_unavailable_revalidate')

    def test_postcommit_traffic_is_not_client_identity_or_readiness(self):
        *_,transaction=self.apply_trial()
        self.assertFalse(desktop.client_route_observation(self.root,transaction)['client_route_observed'])
        self.add_client_route(transaction)
        value=desktop.client_route_observation(self.root,transaction)
        self.assertTrue(value['client_route_observed']);self.assertFalse(value['client_compatibility_verified'])
        self.assertFalse(value['desktop_compatibility_verified']);self.assertFalse(value['desktop_new_thread_verified'])
        prior=self.config_path.read_bytes();self.home.rename(self.home.with_name('moved-home'))
        private_dir(self.home,create=True);private_write(self.config_path,prior)
        value=desktop.client_route_observation(self.root,transaction)
        self.assertFalse(value['client_route_observed'])
        self.assertEqual(value['client_observation_state'],'config_target_changed_revalidate')


class ConfigFirstStartupTests(unittest.TestCase):
    def test_normal_start_reaches_consent_without_any_app_discovery(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);home=private_dir(root/'home',create=True);binary=root/'cli'
            binary.write_bytes(b'fixture CLI');binary.chmod(0o700)
            cfg=default_config('fixture-folder',root/'credential',root,binary)
            cfg['model_selection']=select(load_catalog(),'gpt-6.1-sol','high')
            raw=canonical(cfg);path=root/'router.json';private_write(path,raw);messages=[]
            ui=SimpleNamespace(confirm=lambda text:messages.append(text) or False)
            launcher=desktop.DesktopGlobal(root=root,state=root/'state',config=path,ui=ui)
            args=SimpleNamespace(expected_config_sha256=hash_bytes(raw),codex_home=str(home),
                model=None,effort=None,catalog=None)
            with patch.object(config,'parser'),patch.object(launcher.ports,'binary',return_value={'version':config.CODEX_VERSION}), \
                 patch.object(launcher.ports,'desktop_app',side_effect=AssertionError('app discovery forbidden')) as discover:
                for name in (None,'ChatGPT.app','Arbitrary renamed client.app'):
                    if name:
                        bundle=root/name;bundle.mkdir();(bundle/'Info.plist').write_text('unknown bundle metadata')
                    with self.assertRaises(desktop.Cancelled):launcher.start(args)
                discover.assert_not_called()
            self.assertEqual(len(messages),3);self.assertFalse((root/'state').exists())
            self.assertIn('Only local consumers',messages[0]);self.assertFalse((home/'config.toml').exists())
            # Even an empty explicit path is an advanced-mode request, never
            # permission to downgrade silently to config-first.
            for field in ('desktop_app','desktop_codex'):
                setattr(args,field,'')
                with patch.object(config,'parser'), \
                     patch.object(launcher.ports,'binary',side_effect=lambda path: ({'version':config.CODEX_VERSION} if path else (_ for _ in ()).throw(ProtocolError('empty_explicit_binary')))), \
                     patch.object(launcher.ports,'desktop_app',side_effect=ProtocolError('empty_explicit_app')):
                    with self.assertRaisesRegex(ProtocolError,'empty_explicit'):launcher.start(args)
                delattr(args,field)
            self.assertEqual(len(messages),3)

    def test_explicit_old_modes_are_not_silently_downgraded(self):
        cli={'version':config.CODEX_VERSION}
        for spec in ({'cli':cli},{'cli':cli,'client_evidence_profile':False},
                     {'cli':cli,'client_evidence_profile':pilot.CONFIG_TRIAL,'desktop_app':None},
                     {'cli':cli,'client_evidence_profile':pilot.CONFIG_TRIAL,'desktop':None}):
            with self.assertRaises(ProtocolError):desktop.client_arguments(spec)
        self.assertNotIn('client_profile',desktop.client_arguments({'cli':cli,'desktop':cli}))
        self.assertIn('desktop_app',desktop.client_arguments({'cli':cli,'desktop_app':{'legacy':'unchanged'}}))
        self.assertEqual(desktop.client_arguments({'cli':cli,'client_evidence_profile':pilot.CONFIG_TRIAL})['client_profile'],pilot.CONFIG_TRIAL)


if __name__=='__main__':unittest.main()
