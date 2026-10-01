"""Synthetic native-shaped queue/Store ports only; no native tool or Google.

The parser and binary command observations are explicitly stubbed in these gate
unit tests, and each test snapshots its package-source baseline so concurrent
development edits cannot change the fixture mid-turn. Source-change rejection
is exercised explicitly. Real tomlkit tests remain honest dependency-based skips
in test_global_config; these tests do not claim installed-client acceptance.
"""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from remote_transport import global_config as config, global_pilot as pilot
from remote_transport.global_gateway import private_dir, private_write
from remote_transport.global_fixture import post
from remote_transport.model import ProtocolError, canonical, hash_bytes
from remote_tests import test_global_control as control_fixture


class GlobalPilotTests(unittest.TestCase):
    # Reuse the actual signed queue + actual CASWorker/facade fixture ports.
    error=control_fixture.GlobalControlTests.error
    path=control_fixture.GlobalControlTests.path
    execute=control_fixture.GlobalControlTests.execute
    demand=control_fixture.GlobalControlTests.demand
    native=control_fixture.GlobalControlTests.native
    child_admit=control_fixture.GlobalControlTests.child_admit
    turn=control_fixture.GlobalControlTests.turn

    def setUp(self):
        control_fixture.GlobalControlTests.setUp(self)
        self.cli=self.root/'fixture-cli';self.desktop=self.root/'fixture-desktop'
        for path in (self.cli,self.desktop):path.write_bytes(b'synthetic binary');path.chmod(0o700)
        self.parser_stub=patch.object(pilot,'_parser_binding',return_value={'version':'0.13.3','source':'explicit unit-test stub'})
        self.parser_stub.start();self.addCleanup(self.parser_stub.stop)
        self.source_stub=patch.object(pilot,'_sources',return_value=pilot._sources())
        self.source_stub.start();self.addCleanup(self.source_stub.stop)
        with patch.object(pilot.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=config.CODEX_VERSION+'\n')):
            self.versions=pilot.observe_versions(self.store,self.cli,self.desktop)

    def tearDown(self):control_fixture.GlobalControlTests.tearDown(self)

    def prepare(self):return pilot.prepare_preflight(self.store,version_evidence=self.versions)

    def complete(self,result=None):
        plan=self.prepare();rid,body,client=self.demand(plan['body'],plan['identity'])
        worker,pin=self.child_admit(rid,self.native(rid))
        self.worker=worker
        self.turn(rid,body,client,worker,plan['expected_nonce'] if result is None else result)
        # HTTP completion can precede the durable finish by a few instructions.
        until=time.monotonic()+2
        while time.monotonic()<until:
            with self.store.transaction() as db:
                state=db.execute('SELECT state FROM requests WHERE route=? AND digest=?',(rid,plan['request_digest'])).fetchone()
            if state and state['state']=='text_complete':break
            time.sleep(.01)
        return plan,pin

    def verify(self,plan):
        return pilot.verify_preflight(self.store,plan['plan_id'],queue_state=self.bridge.read().state,join_code=self.code)

    def require(self,proof):
        return pilot.require_pilot(self.store.root,proof['proof_id'],config.CODEX_VERSION,config.CODEX_VERSION)

    def test_alternate_signed_queue_root_cannot_seal_preflight(self):
        plan,_=self.complete();state=self.bridge.read().state
        q=control_fixture.q
        clone=q.initial(activation_id=state['activation_id'],queue_id='f'*32,
            folder_id=state['folder_id'],document_id=state['document_id'],tab_id=state['tab_id'],
            join_code=self.code,created=state['created'],expires=state['expires'],
            runtime_source_hashes=state['runtime_source_hashes'],**state['limits'])
        for event in state['events']:
            args=copy.deepcopy(event['arguments'])
            if event['kind']=='begin':args['dispatch_id']=q.dispatch_id(clone,args['route_id'])
            clone=q.transition(clone,self.code,event['kind'],event['actor'],args,
                operation_id=event['operation_id'],now=event['at'])
        self.error('signed_queue_root_mismatch',pilot.verify_preflight,self.store,plan['plan_id'],
            queue_state=clone,join_code=self.code)

    def test_signed_completed_request_is_bound_and_never_production_ready(self):
        plan,pin=self.complete();proof=self.verify(plan);info=self.require(proof)
        self.assertNotEqual(plan['expected_nonce'],plan['request_nonce'])
        self.assertEqual(proof['route_id'],plan['route_id']);self.assertEqual(proof['native_task_id'],pin.body['identity']['native_task_id'])
        self.assertTrue(info['pilot_ready']);self.assertFalse(info['production_ready']);self.assertFalse(info['ready_for_config'])
        self.assertFalse(self.store.status()['production_ready'])
        self.assertEqual((self.store.root/'pilot'/('proof-'+proof['proof_id']+'.json')).stat().st_mode&0o777,0o600)

    def test_prepare_does_not_admit_or_dispatch(self):
        plan=self.prepare()
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM routes').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0],0)
        self.assertEqual(plan['body']['tools'],[])

    def test_offline_mode_cannot_prepare(self):
        with self.store.transaction() as db:db.execute("UPDATE controller SET mode='offline_fixture'")
        self.error('pilot_live_native_controller_required',self.prepare)

    def test_unbound_gateway_cannot_prepare(self):
        self.gateway.close()
        with self.assertRaises((OSError,ProtocolError)):self.prepare()

    def test_caller_version_assertions_cannot_authorize(self):
        self.error('signature',pilot.prepare_preflight,self.store,version_evidence={'cli':config.CODEX_VERSION,'desktop':config.CODEX_VERSION})
        changed=copy.deepcopy(self.versions);changed['binaries']['cli']['version']='codex-cli 999'
        self.error('signature',pilot.prepare_preflight,self.store,version_evidence=changed)

    def test_version_probe_rejects_unsupported_observed_binary(self):
        with patch.object(pilot.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='codex-cli 999\n')):
            self.error('version_mismatch',pilot.observe_versions,self.store,self.cli,self.desktop)

    def test_binary_change_rejects_evidence(self):
        self.cli.write_bytes(b'a different binary')
        self.error('binary_evidence_changed',self.prepare)

    def test_binary_symlink_is_bound_to_its_original_executable(self):
        link=self.root/'fixture-homebrew-codex';link.symlink_to(self.cli)
        with patch.object(pilot.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=config.CODEX_VERSION+'\n')):
            versions=pilot.observe_versions(self.store,link,self.desktop)
        pilot.prepare_preflight(self.store,version_evidence=versions)
        link.unlink();link.symlink_to(self.desktop)
        self.error('binary_evidence_changed',pilot.prepare_preflight,self.store,version_evidence=versions)

    def test_package_and_parser_evidence_changes_rejected(self):
        sources=copy.deepcopy(pilot._sources());sources['files']['remote_transport/global_pilot.py']='0'*64
        with patch.object(pilot,'_sources',return_value=sources):self.error('package_source_changed',self.prepare)
        with patch.object(pilot,'_parser_binding',return_value={'version':'0.13.2'}):self.error('parser_evidence_changed',self.prepare)

    def test_version_evidence_expiry(self):
        with patch.object(pilot.time,'time',return_value=self.versions['observed_at']+pilot.VERSION_SECONDS):
            self.error('version_evidence_expired',self.prepare)

    def test_ready_alone_without_completed_request_is_insufficient(self):
        plan=self.prepare();rid,_,_=self.demand(plan['body'],plan['identity']);self.child_admit(rid,self.native(rid))
        self.error('successful_preflight_required',self.verify,plan)

    def test_wrong_nonce_rejected(self):
        plan,_=self.complete('earlier unrelated response')
        self.error('nonce_response_mismatch',self.verify,plan)

    def test_stale_response_cannot_satisfy_new_plan(self):
        plan,_=self.complete()
        private=pilot._load(self.store,'plan',plan['plan_id'])
        with self.store.transaction() as db:
            db.execute('UPDATE requests SET created=? WHERE route=?',(private['created']-1,plan['route_id']))
        self.error('stale_preflight_response',self.verify,plan)

    def test_unknown_delivery_or_absent_backend_identity_rejected(self):
        plan,_=self.complete()
        with self.store.transaction() as db:
            db.execute("UPDATE requests SET state='dispatch_intent' WHERE route=?",(plan['route_id'],))
        self.error('successful_preflight_required',self.verify,plan)
        with self.store.transaction() as db:
            db.execute("UPDATE requests SET state='text_complete',backend_response_id=NULL WHERE route=?",(plan['route_id'],))
        self.error('backend_response_identity_required',self.verify,plan)

    def test_wrong_signed_queue_or_native_receipt_rejected(self):
        plan,_=self.complete();state=self.bridge.read().state
        wrong=copy.deepcopy(state);wrong['logical']['demands'][plan['route_id']]['ready']['runtime_hash']='b'*64
        self.error('projection',pilot.verify_preflight,self.store,plan['plan_id'],queue_state=wrong,join_code=self.code)
        self.error('join_code',pilot.verify_preflight,self.store,plan['plan_id'],queue_state=state,join_code='0'*64)

    def test_retired_epoch_or_different_generation_invalidates_proof(self):
        plan,_=self.complete();proof=self.verify(plan)
        with self.store.transaction() as db:db.execute("UPDATE controller SET epoch=?",('c'*32,))
        self.error('activation_or_controller_changed|live_native_controller_required',self.require,proof)

    def test_new_activation_invalidates_proof(self):
        plan,_=self.complete();proof=self.verify(plan)
        self.store.activate(self.store.status()['selection'])
        self.error('native_activation_mismatch',self.require,proof)

    def test_expired_pin_or_changed_ready_route_invalidates_proof(self):
        plan,_=self.complete();proof=self.verify(plan)
        with self.store.transaction() as db:db.execute('UPDATE routes SET expires=? WHERE id=?',(time.time()-1,plan['route_id']))
        self.error('route_expired',self.require,proof)

    def test_changed_response_invalidates_proof(self):
        plan,_=self.complete();proof=self.verify(plan)
        with self.store.transaction() as db:
            row=db.execute('SELECT response FROM requests WHERE route=?',(plan['route_id'],)).fetchone()
            db.execute('UPDATE requests SET response=? WHERE route=?',(bytes(row['response'])+b'\n',plan['route_id']))
        self.error('invalid_response_stream|completed_route_evidence_changed',self.require,proof)

    def test_proof_tampering_and_wrong_client_version_rejected(self):
        plan,_=self.complete();proof=self.verify(plan)
        self.error('both_verified_versions',pilot.require_pilot,self.store.root,proof['proof_id'],config.CODEX_VERSION,None)
        path=self.store.root/'pilot'/('proof-'+proof['proof_id']+'.json');value=json.loads(path.read_bytes());value['expires']+=10000
        private_write(path,canonical(value,max_bytes=pilot.MAX_EVIDENCE));self.error('signature',self.require,proof)

    def test_proof_expiry_and_refresh_do_not_dispatch(self):
        plan,_=self.complete();proof=self.verify(plan)
        expired=copy.deepcopy(pilot._load(self.store,'proof',proof['proof_id']));expired['expires']=time.time()-1
        pilot._save(self.store,'proof',expired)
        self.error('proof_expired',self.require,proof)
        refreshed=self.verify(plan);self.require(refreshed)
        with self.store.transaction() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0],1)

    def test_explicit_nonce_refresh_reuses_route_without_new_native_admission(self):
        previous,pin=self.complete();self.verify(previous)
        original=self.store.route(previous['route_id'])
        refreshed=pilot.prepare_preflight(self.store,version_evidence=self.versions,previous_plan_id=previous['plan_id'])
        self.assertEqual(refreshed['identity'],previous['identity']);self.assertEqual(refreshed['route_id'],previous['route_id'])
        self.assertNotEqual(refreshed['expected_nonce'],previous['expected_nonce'])
        self.assertNotEqual(refreshed['request_digest'],previous['request_digest'])
        self.assertEqual(refreshed['body']['input'][0],previous['body']['input'][0])
        self.assertEqual(refreshed['body']['input'][1]['role'],'assistant')
        self.turn(refreshed['route_id'],refreshed['body'],refreshed['identity'],self.worker,refreshed['expected_nonce'])
        proof=self.verify(refreshed);self.require(proof)
        current=self.store.route(previous['route_id'])
        self.assertEqual((current['pin'],current['native_task'],current['version']),
                         (original['pin'],original['native_task'],original['version']))
        self.error('latest_completed_plan_required',pilot.prepare_preflight,self.store,
                   version_evidence=self.versions,previous_plan_id=previous['plan_id'])
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM routes').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0],2)

    def test_refresh_blocks_unknown_prior_outcome_and_exhausted_request_budget(self):
        previous,_=self.complete();rid=previous['route_id']
        with self.store.transaction() as db:
            db.execute('INSERT INTO requests VALUES(?,?,?,?,NULL,NULL)',(rid,'0'*64,'dispatch_intent',time.time()))
        self.error('refresh_unknown_prior_outcome',pilot.prepare_preflight,self.store,
                   version_evidence=self.versions,previous_plan_id=previous['plan_id'])
        with self.store.transaction() as db:
            db.execute('DELETE FROM requests WHERE digest=?',('0'*64,))
            db.execute('UPDATE routes SET used=128 WHERE id=?',(rid,))
        self.error('refresh_request_budget_exhausted',pilot.prepare_preflight,self.store,
                   version_evidence=self.versions,previous_plan_id=previous['plan_id'])

    def test_expired_plan_requires_explicit_refresh_with_current_heartbeat(self):
        previous,_=self.complete();now=int(time.time());future=now+pilot.PLAN_SECONDS+1
        # A real supervisor must continue heartbeats; a dead controller cannot
        # be revived by a single stale heartbeat merely to refresh its proof.
        for at in [*range(now+20,future,20),future]:
            with patch.object(pilot.time,'time',return_value=at):
                self.execute('heartbeat',now=at);self.bridge.sync_heartbeat()
        with patch.object(pilot.time,'time',return_value=future):
            self.error('preflight_expired',self.verify,previous)
            refreshed=pilot.prepare_preflight(self.store,version_evidence=self.versions,previous_plan_id=previous['plan_id'])
            self.assertEqual(refreshed['route_id'],previous['route_id'])
            self.assertGreater(refreshed['expires'],future)
        with self.store.transaction() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0],1)

    def test_apply_needs_exact_preview_and_rechecks_gate_before_replacement(self):
        plan,_=self.complete();proof=self.verify(plan);home=private_dir(self.root/'temporary-home',create=True)
        path=home/'config.toml';private_write(path,b'# before\n');args={'cli_version':config.CODEX_VERSION,
            'desktop_version':config.CODEX_VERSION,'expected_before_hash':hash_bytes(path.read_bytes()),'confirm':True,
            'pilot_proof_id':proof['proof_id']}
        self.error('exact_preview_confirmation',config.apply,self.store.root,home,**args)
        self.assertFalse((home/'.dots2codex-global.lock').exists())
        # This test isolates commit gating, explicitly not parser integration.
        with patch.object(config,'render_patch',return_value=b'# proposed fixture only\n'):
            self.error('preview_outdated',config.apply,self.store.root,home,expected_after_hash='0'*64,**args)
            def retire():
                with self.store.transaction() as db:db.execute('UPDATE controller SET epoch=?',('d'*32,))
            self.error('activation_or_controller_changed|live_native_controller_required',config.apply,self.store.root,home,
                expected_after_hash=hash_bytes(b'# proposed fixture only\n'),before_commit=retire,**args)
        self.assertEqual(path.read_bytes(),b'# before\n')


if __name__=='__main__':unittest.main()
