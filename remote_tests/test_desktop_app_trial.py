"""Trial-profile proof/config fixtures; no real Mac, Google, or native inference.

The established strict profile fixture uses the real local queue/gateway/worker
with synthetic admissions. Apple signatures and the TOML parser are explicitly
stubbed here. This file does not count as real parser or desktop acceptance.
"""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from remote_tests import test_global_pilot as strict_fixture
from remote_tests.test_codex_desktop_discovery import make_app
from remote_transport import codex_desktop as app
from remote_transport import global_desktop as desktop
from remote_transport import global_config as config, global_pilot as pilot
from remote_transport.global_gateway import private_dir, private_write
from remote_transport.model import ProtocolError, hash_bytes, canonical


class DesktopAppTrialTests(unittest.TestCase):
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

    def setUp(self):
        strict_fixture.GlobalPilotTests.setUp(self)
        self.strict_versions=self.versions
        self.bundle=make_app(self.root/'Applications')
        self.signature_ok=True;self.commands=[]
        original=app.application_evidence
        stub=patch.object(app,'application_evidence',side_effect=lambda path,**kwargs:original(path,run=self.apple,platform='darwin'))
        stub.start();self.addCleanup(stub.stop)
        self.app_evidence=app.application_evidence(self.bundle)
        with patch.object(pilot.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=config.CODEX_VERSION+'\n')):
            self.versions=pilot.observe_desktop_app(self.store,self.cli,self.app_evidence)

    def apple(self,command,**kwargs):
        self.commands.append(command)
        self.assertEqual(command[0],'/usr/bin/codesign')
        return SimpleNamespace(returncode=0 if self.signature_ok else 1,stdout='')

    def require(self,proof):
        return pilot.require_pilot(self.store.root,proof['proof_id'],config.CODEX_VERSION,None,desktop_app=self.app_evidence)

    def apply_args(self,proof):
        home=private_dir(self.root/'trial-home',create=True);path=home/'config.toml'
        private_write(path,b'# before trial\n')
        return home,path,{'cli_version':config.CODEX_VERSION,'desktop_version':None,'desktop_app':self.app_evidence,
            'expected_before_hash':hash_bytes(path.read_bytes()),'expected_after_hash':hash_bytes(b'# trial fixture\n'),
            'confirm':True,'pilot_proof_id':proof['proof_id']}

    def apply_trial(self):
        plan,_=self.complete();proof=self.verify(plan);home,path,args=self.apply_args(proof)
        with patch.object(config,'render_patch',return_value=b'# trial fixture\n'):
            applied=config.apply(self.store.root,home,**args)
        _,transaction,_,_=config.load_transaction(self.store.root,applied['transaction_id'])
        return plan,proof,home,path,applied,transaction

    def test_trial_separates_app_display_version_from_cli_engine(self):
        self.assertEqual(self.versions['profile'],app.TRIAL)
        self.assertEqual(set(self.versions['binaries']),{'cli'})
        self.assertEqual(self.versions['desktop_app']['app_version'],'26.930.1')
        self.assertIsNone(self.versions['desktop_app']['engine_version'])
        plan,_=self.complete();proof=self.verify(plan);info=self.require(proof)
        self.assertTrue(info['pilot_ready']);self.assertFalse(info['production_ready'])
        self.assertFalse(info['desktop_compatibility_verified']);self.assertEqual(info['client_evidence_profile'],app.TRIAL)
        self.assertEqual(info['preflight_route_id'],plan['route_id'])

    def test_trial_cannot_masquerade_as_exact_two_binary_proof(self):
        plan,_=self.complete();proof=self.verify(plan)
        self.error('trial_app_evidence_required',pilot.require_pilot,self.store.root,proof['proof_id'],config.CODEX_VERSION,config.CODEX_VERSION)
        self.error('trial_app_evidence_required',pilot.require_pilot,self.store.root,proof['proof_id'],config.CODEX_VERSION,None)
        self.error('cannot_assert_engine',config.versions,config.CODEX_VERSION,config.CODEX_VERSION,self.app_evidence)

    def test_strict_proof_cannot_be_downgraded_into_trial(self):
        self.versions=self.strict_versions;plan,_=self.complete();proof=self.verify(plan)
        self.error('both_verified_versions',self.require,proof)
        self.error('both_verified_versions',pilot.require_pilot,self.store.root,proof['proof_id'],config.CODEX_VERSION,None)
        self.assertTrue(pilot.require_pilot(self.store.root,proof['proof_id'],config.CODEX_VERSION,config.CODEX_VERSION)['pilot_ready'])

    def test_changed_signed_metadata_fails_before_home_mutation(self):
        plan,_=self.complete();proof=self.verify(plan);home,path,args=self.apply_args(proof)
        signature=self.bundle/'Contents/_CodeSignature/CodeResources';signature.write_bytes(b'new signed resource set')
        with patch.object(config,'render_patch') as render:
            self.error('evidence_changed',config.apply,self.store.root,home,**args)
        render.assert_not_called();self.assertFalse((home/'.dots2codex-global.lock').exists())
        self.assertEqual(path.read_bytes(),b'# before trial\n')

    def test_missing_app_cannot_apply_or_fall_back_to_cli(self):
        plan,_=self.complete();proof=self.verify(plan);home,path,args=self.apply_args(proof)
        self.bundle.rename(self.bundle.with_name('Moved.app'))
        with self.assertRaises((ProtocolError,FileNotFoundError)):config.apply(self.store.root,home,**args)
        self.assertFalse((home/'.dots2codex-global.lock').exists())

    def test_invalid_signature_after_preflight_fails_before_apply(self):
        plan,_=self.complete();proof=self.verify(plan);home,path,args=self.apply_args(proof);self.signature_ok=False
        self.error('signature_not_verified',config.apply,self.store.root,home,**args)
        self.assertFalse((home/'.dots2codex-global.lock').exists())

    def test_signature_change_during_consent_commit_boundary_is_rechecked(self):
        plan,_=self.complete();proof=self.verify(plan);home,path,args=self.apply_args(proof)
        def changed():self.signature_ok=False
        with patch.object(config,'render_patch',return_value=b'# trial fixture\n'):
            self.error('signature_not_verified',config.apply,self.store.root,home,before_commit=changed,**args)
        self.assertEqual(path.read_bytes(),b'# before trial\n')

    def test_changed_terminal_cli_still_blocks_trial(self):
        self.cli.write_bytes(b'new terminal binary');self.error('binary_evidence_changed',self.prepare)
    def test_forged_profile_flag_without_sealed_evidence_fails(self):
        changed=copy.deepcopy(self.versions);changed['profile']='strict-client-binaries/1'
        self.error('signature',pilot.prepare_preflight,self.store,version_evidence=changed)
    def test_offline_controller_does_not_enable_trial(self):
        with self.store.transaction() as db:db.execute("UPDATE controller SET mode='offline_fixture'")
        self.error('live_native_controller_required',self.prepare)
    def test_trial_refresh_cannot_switch_profiles(self):
        plan,_=self.complete()
        self.error('refresh_evidence_changed',pilot.prepare_preflight,self.store,
                   version_evidence=self.strict_versions,previous_plan_id=plan['plan_id'])
    def test_explicit_trial_consent_required(self):
        plan,_=self.complete();proof=self.verify(plan);home,path,args=self.apply_args(proof);args['confirm']=False
        self.error('confirmation_required',config.apply,self.store.root,home,**args)
        self.assertFalse((home/'.dots2codex-global.lock').exists())
    def test_trial_apply_records_identity_not_engine_and_exact_restore_preserves_auth(self):
        plan,proof,home,path,applied,transaction=self.apply_trial()
        private_write(home/'auth.json',b'synthetic secret left untouched')
        self.assertEqual(transaction['desktop_app_evidence'],self.app_evidence)
        self.assertIsNone(transaction['desktop_version_evidence'])
        self.assertEqual(transaction['client_evidence_profile'],app.TRIAL)
        self.assertFalse(transaction['desktop_compatibility_verified'])
        self.assertEqual(desktop.client_route_observation(self.root,transaction)['completed_client_routes'],0)
        result=config.restore(self.store.root,applied['transaction_id'],confirm=True)
        self.assertEqual(result['phase'],'restored');self.assertEqual(path.read_bytes(),b'# before trial\n')
        self.assertEqual((home/'auth.json').read_bytes(),b'synthetic secret left untouched')

    def test_global_continue_normal_trial_diff_cancel_then_apply_without_picker(self):
        plan,_=self.complete();proof=self.verify(plan);home,path,args=self.apply_args(proof)
        cli=desktop.binary_evidence(self.cli,run=lambda *args,**kwargs:SimpleNamespace(returncode=0,stdout=config.CODEX_VERSION+'\n'))
        messages=[]
        ui=SimpleNamespace(confirm=lambda message:messages.append(message) or False,
                           file=lambda *args:(_ for _ in ()).throw(AssertionError('no picker')))
        launcher=desktop.DesktopGlobal(root=self.root,state=self.root/'launcher-state',config=self.root/'router.json',ui=ui)
        active={'runtime':str(self.root),'spec':{'generation':self.store.activation()['id'],
                'codex_home':str(home),'cli':cli,'desktop_app':self.app_evidence}}
        preview={'config_path':str(path),'diff':'model_provider: owned change only',
                 'before_hash':args['expected_before_hash'],'after_hash':args['expected_after_hash']}
        with patch.object(launcher,'_wait_stage',return_value={'stage':'PREFLIGHT_VERIFIED','pilot_proof_id':proof['proof_id']}),patch.object(config,'preview',return_value=preview),patch.object(config,'render_patch',return_value=b'# trial fixture\n'),patch.object(launcher.ports,'owned',return_value=True):
            with self.assertRaises(desktop.Cancelled):launcher._continue(active)
            self.assertEqual(path.read_bytes(),b'# before trial\n')
            self.assertIn(str(self.bundle),messages[0]);self.assertIn('26.930.1',messages[0])
            self.assertIn('compatibility has not been verified',messages[0])
            self.assertIn('Restore Global config',messages[0]);self.assertIn(str(path),messages[0])
            ui.confirm=lambda message:messages.append(message) or True
            result=launcher._continue(active)
        self.assertEqual(result['phase'],'committed');self.assertEqual(result['client_evidence_profile'],app.TRIAL)
        self.assertFalse(result['production_ready']);self.assertFalse(result['desktop_compatibility_verified'])
        self.assertEqual(path.read_bytes(),b'# trial fixture\n')

    def add_client_route(self,transaction,*,session='client-new',created=None,state='text_complete'):
        # Receipt-count fixture only, explicitly not a native protocol proof.
        when=transaction['committed_at']+.001 if created is None else created
        original=self.store.route(transaction['preflight_route_id']);row=dict(original)
        row.update(id='e'*32,identity=json.dumps({'session-id':session,'thread-id':'f'*32}),created=when,
                   native_task=None,pin=None,endpoint=None)
        with self.store.transaction() as db:
            db.execute('INSERT INTO routes ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?)',(row['id'],'a'*64,state,when,b'fixture response','resp_fixture'))

    def test_postapply_completed_client_route_is_never_desktop_attestation(self):
        *_,transaction=self.apply_trial();self.add_client_route(transaction)
        result=desktop.client_route_observation(self.root,transaction)
        self.assertEqual(result['completed_client_routes'],1);self.assertTrue(result['client_route_observed'])
        self.assertFalse(result['desktop_new_thread_verified']);self.assertFalse(result['desktop_compatibility_verified'])
    def test_changed_config_cannot_report_successful_trial(self):
        _,_,_,path,_,transaction=self.apply_trial();self.add_client_route(transaction);path.write_bytes(b'# user edit\n')
        result=desktop.client_route_observation(self.root,transaction)
        self.assertFalse(result['client_route_observed']);self.assertEqual(result['client_observation_state'],'config_changed_revalidate')
    def test_old_thread_completion_does_not_count_as_new_thread(self):
        *_,transaction=self.apply_trial();self.add_client_route(transaction,created=transaction['committed_at']-10)
        self.assertFalse(desktop.client_route_observation(self.root,transaction)['client_route_observed'])
    def test_preflight_completion_does_not_count_as_client(self):
        *_,transaction=self.apply_trial();self.add_client_route(transaction,session='dots-pilot-other')
        self.assertFalse(desktop.client_route_observation(self.root,transaction)['client_route_observed'])
    def test_unknown_outcome_does_not_count_as_completed_client(self):
        *_,transaction=self.apply_trial();self.add_client_route(transaction,state='dispatch_intent')
        self.assertFalse(desktop.client_route_observation(self.root,transaction)['client_route_observed'])
    def test_restored_or_expired_activation_does_not_report_client_ready(self):
        *_,transaction=self.apply_trial();self.add_client_route(transaction)
        self.store.disable(transaction['generation'])
        self.assertFalse(desktop.client_route_observation(self.root,transaction)['client_route_observed'])
        transaction['phase']='restored'
        self.assertFalse(desktop.client_route_observation(self.root,transaction)['client_route_observed'])


if __name__=='__main__':unittest.main()
