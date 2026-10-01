"""Offline staged preflight regressions; no Google or native-platform calls."""
import concurrent.futures
import copy
import secrets
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from remote_tests import test_global_pilot as pilot_fixture
from remote_transport import global_pilot as pilot, global_desktop as desktop
from remote_transport.global_gateway import strict_json
from remote_transport.model import ProtocolError


class GlobalPrewarmTests(unittest.TestCase):
    setUp=pilot_fixture.GlobalPilotTests.setUp
    tearDown=pilot_fixture.GlobalPilotTests.tearDown
    error=pilot_fixture.GlobalPilotTests.error
    path=pilot_fixture.GlobalPilotTests.path
    execute=pilot_fixture.GlobalPilotTests.execute
    native=pilot_fixture.GlobalPilotTests.native
    child_admit=pilot_fixture.GlobalPilotTests.child_admit

    def reserve(self):
        return pilot.reserve_preflight_route(self.store,version_evidence=self.versions)

    def status(self,reservation):
        return pilot.preflight_route_status(self.store,reservation['reservation_id'],
            queue_state=self.bridge.read().state,join_code=self.code)

    def finalize(self,reservation,versions=None):
        return pilot.finalize_preflight_route(self.store,reservation['reservation_id'],
            version_evidence=versions or self.versions,queue_state=self.bridge.read().state,join_code=self.code)

    def counts(self):
        with self.store.transaction() as db:
            return tuple(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('routes','requests'))

    def ready(self,reservation):
        self.assertEqual(self.bridge.step()['state'],'demand_published')
        return self.child_admit(reservation['route_id'],self.native(reservation['route_id']))

    def tick_to(self,target):
        last=self.bridge.read().state['logical']['controller']['heartbeat_at']
        for at in [*range(last+25,target,25),target]:
            with patch('time.time',return_value=at):
                self.execute('heartbeat',now=at);self.bridge.sync_heartbeat()

    def fresh_versions(self):
        with patch.object(pilot.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=pilot.CODEX_VERSION+'\n')):
            return pilot.observe_versions(self.store,self.cli,self.desktop)

    def test_route_reservation_is_one_use_and_never_creates_http_or_nonce_plan(self):
        with patch.object(desktop,'post_preflight') as connection:
            reservation=self.reserve()
            self.assertEqual(self.counts(),(1,0))
            self.assertFalse(self.status(reservation)['ready'])
            self.assertEqual(list((self.store.root/'pilot').glob('plan-*.json')),[])
            self.error('already_reserved_no_replay',self.reserve)
            self.error('route_not_ready',self.finalize,reservation)
            self.assertEqual(self.counts(),(1,0));connection.assert_not_called()

    def test_seven_minute_setup_then_one_post_fresh_plan_and_proof(self):
        reservation=self.reserve();self.assertEqual(self.bridge.step()['state'],'demand_published')
        target=int(time.time())+420;self.tick_to(target)
        with patch('time.time',return_value=target):
            self.assertEqual(self.counts(),(1,0));self.assertFalse(self.status(reservation)['ready'])
            self.assertEqual(list((self.store.root/'pilot').glob('plan-*.json')),[])
            worker,pin=self.child_admit(reservation['route_id'],self.native(reservation['route_id']))
            versions=self.fresh_versions();plan=self.finalize(reservation,versions)
            self.assertEqual(self.counts(),(1,0));self.assertEqual(plan['expires'],target+pilot.PLAN_SECONDS)
            self.error('attempt_already_consumed',self.finalize,reservation,versions)
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future=pool.submit(desktop.post_preflight,self.store,plan)
                try:
                    permit=worker.poll(worker.start_next,attempts=8,initial_delay=.1,max_delay=.5)
                    self.assertIsNotNone(permit);worker.complete(permit,plan['expected_nonce'])
                    self.assertEqual(future.result(timeout=10),{'http_status':200})
                except BaseException:
                    self.bridge.close_local_facades()
                    raise
            until=time.monotonic()+2
            while time.monotonic()<until:
                with self.store.transaction() as db:
                    row=db.execute('SELECT state FROM requests WHERE route=?',(plan['route_id'],)).fetchone()
                if row['state']=='text_complete':break
                time.sleep(.01)
            proof=pilot.verify_preflight(self.store,plan['plan_id'],queue_state=self.bridge.read().state,join_code=self.code)
            self.assertEqual(proof['expires'],target+pilot.PROOF_SECONDS)
            self.assertEqual(self.counts(),(1,1))
            self.assertEqual(self.store.route(plan['route_id'])['native_task'],pin.body['identity']['native_task_id'])

    def test_changed_reobserved_client_evidence_cannot_finalize(self):
        reservation=self.reserve();self.ready(reservation)
        current=pilot._unseal(self.store,'versions',self.fresh_versions())
        # Another separately observed executable can be legitimate, but is not
        # the same client chosen when this bounded prewarm started.
        other=self.root/'other-cli';other.write_bytes(b'other valid binary');other.chmod(0o700)
        with patch.object(pilot.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=pilot.CODEX_VERSION+'\n')):
            changed=pilot.observe_versions(self.store,other,self.desktop)
        self.error('client_evidence_changed',self.finalize,reservation,changed)
        self.assertEqual(self.counts(),(1,0))

    def test_signed_child_bootstrap_expiry_is_not_extended_by_setup_budget(self):
        reservation=self.reserve();self.bridge.step()
        demand=self.bridge.read().state['logical']['demands'][reservation['route_id']]
        target=demand['child_bootstrap']['expires'];self.tick_to(target)
        with patch('time.time',return_value=target):
            self.error('setup_expired',self.status,reservation)
            self.error('setup_expired',self.finalize,reservation,self.fresh_versions())
            self.error('already_reserved_no_replay',self.reserve)
            self.assertEqual(self.counts(),(1,0))

    def test_unknown_route_and_dispatch_intent_cannot_be_reissued(self):
        reservation=self.reserve();self.ready(reservation)
        with self.store.transaction() as db:
            db.execute('INSERT INTO requests VALUES(?,?,?,?,NULL,NULL)',(reservation['route_id'],'0'*64,'dispatch_intent',time.time()))
        self.error('route_already_used',self.finalize,reservation)
        with self.store.transaction() as db:db.execute("UPDATE routes SET state='unknown' WHERE id=?",(reservation['route_id'],))
        self.error('route_unusable',self.status,reservation)
        self.assertEqual(self.counts(),(1,1))

    def test_finalize_crash_burns_plan_without_new_request_or_route(self):
        reservation=self.reserve();self.ready(reservation)
        with patch.object(pilot,'_prepare_preflight',side_effect=OSError('synthetic lost output')):
            with self.assertRaises(OSError):self.finalize(reservation)
        self.error('attempt_already_consumed',self.finalize,reservation)
        self.error('already_reserved_no_replay',self.reserve)
        self.assertEqual(self.counts(),(1,0))

    def test_explicit_stop_and_cancel_do_not_post_or_mint_replacement(self):
        reservation=self.reserve();self.ready(reservation);plan=self.finalize(reservation)
        control={'lock':threading.Lock(),'cancelled':True,'socket':None}
        with patch.object(desktop.http.client,'HTTPConnection') as connection:
            self.error('cancelled_before_dispatch',desktop.post_preflight,self.store,plan,control)
            connection.return_value.request.assert_not_called()
        self.bridge.stop()
        self.error('attempt_already_consumed|live_native_controller_required',self.finalize,reservation)
        self.assertEqual(self.counts(),(1,0))


if __name__=='__main__':unittest.main()
