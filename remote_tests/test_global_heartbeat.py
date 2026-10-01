"""Clock-controlled live-controller latency regressions. All ports synthetic."""
import copy
import json
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch

from remote_tests.test_global_control import FakeGoogle
from remote_transport import global_control as q
from remote_transport.global_timing import TIMING, controller_active
from remote_transport.global_native import NativeLedger
from remote_transport.global_google import GoogleQueueBridge, mirror_verified_queue
from remote_transport.global_gateway import Store
from remote_transport.global_fixture import identity
from remote_transport.router_join import _source_hashes
from remote_transport.selection import select, load_catalog
from remote_transport.model import ProtocolError, canonical

class GlobalHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.clock=2000000000.;self.patcher=patch('time.time',side_effect=lambda:self.clock);self.patcher.start()
        self.addCleanup(self.patcher.stop);self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.package=Path(__file__).resolve().parents[1]
        self.store,self.gen=Store.initialize(self.root/'gateway',select(load_catalog(),'gpt-6.1-sol','high'))
        self.google=FakeGoogle();self.code=secrets.token_hex(32);did=self.google.create_document_once('folder','fixture')
        self.initial=q.initial(activation_id=self.gen,queue_id=secrets.token_hex(16),folder_id='folder',document_id=did,
            tab_id='t.0',join_code=self.code,created=int(self.clock),expires=int(self.clock)+1800,runtime_source_hashes=_source_hashes())
        self.bridge=GoogleQueueBridge(self.store,self.root/'bridge',self.google,self.google,self.initial,self.code)
        self.addCleanup(self.bridge.close);self.bridge.initialize_blank_queue()
        self.ledger=NativeLedger(self.root/'native',self.initial,self.code,'/root/fixture_controller');self.serial=0
    def path(self,label):
        self.serial+=1;return self.root/'native'/f'{self.serial}-{label}.json'
    def source(self):return self.bridge.read()
    def plan(self,kind,**kwargs):
        path=self.path(kind);out=self.ledger.plan_event(self.source(),kind,path,**kwargs);return path,out
    def commit(self,path,out,latency=0):
        self.ledger.check_plan(self.source(),path)
        self.clock+=latency
        response=self.google.batch_update_document(**out['tool_arguments'])
        result=self.ledger.verify_plan(path,response,self.google.get_document(self.initial['document_id']))
        return result
    def event(self,kind,latency=0,**kwargs):
        path,out=self.plan(kind,**kwargs);return self.commit(path,out,latency)
    def join(self,latency=26,seconds=1200):
        self.event('join',latency=latency,capacity=2,seconds=seconds)
        # Initial JOIN is authenticated but cannot yet drive client admission.
        self.bridge.sync_heartbeat();self.assertFalse(self.store.status()['controller_active'])
        if latency==0:self.clock+=1
        self.event('heartbeat');self.bridge.sync_heartbeat()
    def demand(self):
        row=self.store.admission(self.gen,identity(),select(load_catalog(),'gpt-6.1-sol','high'))
        self.bridge.prepare_child(row['id']);return row['id']
    def assertError(self,text,fn,*args,**kwargs):
        with self.assertRaisesRegex(ProtocolError,text):fn(*args,**kwargs)

    def test_exact_900_root_policy_and_no_old_protocol_migration(self):
        self.assertEqual(self.initial['controller_timing'],TIMING)
        self.assertEqual(TIMING['freshness_seconds'],900)
        for change in ({'freshness_seconds':180},{'freshness_seconds':899},{'freshness_seconds':901},
                       {'heartbeat_interval_seconds':10}):
            bad=copy.deepcopy(self.initial);bad['controller_timing'].update(change)
            self.assertError('timing',q.verify,bad,self.code)
        old=copy.deepcopy(self.initial);old['contract']='dots-global-admissions/1'
        self.assertError('root',q.verify,old,self.code)
        missing=copy.deepcopy(self.initial);del missing['controller_timing']
        self.assertError('state',q.verify,missing,self.code)

    def test_26_second_join_readback_then_immediate_first_heartbeat(self):
        start=self.clock;self.join()
        c=self.source().state['logical']['controller']
        self.assertEqual(c['joined_at'],start);self.assertEqual(c['heartbeat_at'],start+26)
        self.assertEqual(c['lease_expires'],start+1200)
        self.assertTrue(self.store.status()['controller_active'])

    def test_45_second_cas_and_90_second_native_result_keep_controller_alive(self):
        self.join();self.clock+=25;self.event('heartbeat',latency=45);self.bridge.sync_heartbeat()
        rid=self.demand();self.event('claim',route_id=rid);self.event('begin',route_id=rid)
        plan=self.path('spawn');out=self.ledger.plan_spawn(self.source(),rid,plan,self.package)
        self.ledger.check_spawn(self.source(),plan)
        self.clock+=90 # actual platform call/result may take this long; no synthetic heartbeat
        receipt=self.path('receipt');self.ledger.record_spawn(plan,out['arguments'],
            {'task_name':'/root/fixture/'+out['arguments']['task_name']},receipt)
        self.event('heartbeat');self.bridge.sync_heartbeat();self.event('admitted',route_id=rid)
        self.assertTrue(self.store.status()['controller_active'])
        self.assertEqual(self.source().state['logical']['demands'][rid]['state'],'admitted')

    def test_first_heartbeat_required_before_admission(self):
        self.event('join',capacity=2,seconds=1200)
        self.bridge.sync_heartbeat()
        self.assertEqual(self.bridge.step()['state'],'await_first_heartbeat')
        self.assertError('not_ready',self.store.admission,self.gen,identity(),select(load_catalog(),'gpt-6.1-sol','high'))
        self.assertError('first_heartbeat',q.transition,self.source().state,self.code,'claim','native',
            {'native_task_id':self.ledger.identity,'controller_epoch':self.source().state['logical']['controller']['controller_epoch'],
             'route_id':'a'*32,'claim_id':'b'*32})

    def test_strict_boundary_and_clock_rollback(self):
        self.join();c=self.source().state['logical']['controller'];at=c['heartbeat_at']
        self.assertTrue(controller_active(at,c['lease_expires'],TIMING,at+899.999))
        self.assertFalse(controller_active(at,c['lease_expires'],TIMING,at+900))
        self.assertFalse(controller_active(at,c['lease_expires'],TIMING,at-.01))
        self.clock+=10;self.ledger.inspect(self.source());self.clock-=1
        self.assertError('clock_rollback',self.ledger.inspect,self.source())
        self.clock+=2;self.assertError('clock_rollback',self.ledger.inspect,self.source())

    def test_stale_planned_tick_cannot_revive_either_ledger_or_gateway(self):
        self.join();self.clock+=899;path,out=self.plan('heartbeat')
        self.clock+=2
        response=self.google.batch_update_document(**out['tool_arguments']) # emulate late in-flight completion
        self.assertError('acceptance_window',self.ledger.verify_plan,path,response,self.google.get_document(self.initial['document_id']))
        self.assertError('not_active',self.bridge.sync_heartbeat)
        self.assertFalse(self.store.status()['controller_active'])
        self.assertError('not_active',self.ledger.inspect,self.source())
        restarted=NativeLedger(self.root/'native',self.source().state,self.code,self.ledger.identity)
        self.assertError('not_active',restarted.inspect,self.source())
        self.assertError('not_active',self.bridge.sync_heartbeat)

    def test_mac_rejects_late_prepared_tick_even_before_prior_liveness_expires(self):
        self.join();self.clock+=1;path,out=self.plan('heartbeat');self.clock+=120
        self.google.batch_update_document(**out['tool_arguments'])
        self.assertError('prepared_event_expired',self.bridge.sync_heartbeat)
        self.assertFalse(self.store.status()['controller_active'])

    def test_operation_budget_rejects_even_unexpired_heartbeat(self):
        self.join();self.clock+=1;path,out=self.plan('heartbeat');prepared=self.clock
        self.assertEqual(out['execute_before'],prepared+120)
        self.clock=prepared+119.999
        self.assertTrue(self.ledger.check_plan(self.source(),path)['dispatch_allowed'])
        self.clock=prepared+120
        self.assertTrue(self.store.status()['controller_active'])
        self.assertError('dispatch_window',self.ledger.check_plan,self.source(),path)
        self.assertError('unresolved',self.ledger.plan_event,self.source(),'heartbeat',self.path('retry'))

    def test_missing_write_reconciliation_cannot_be_reissued_as_new_tick(self):
        self.join();self.clock+=25;path,out=self.plan('heartbeat')
        self.assertError('not_observed',self.ledger.verify_plan,path,None,self.google.get_document(self.initial['document_id']))
        self.clock+=181;self.assertError('unresolved',self.ledger.plan_event,self.source(),'heartbeat',self.path('new'))

    def test_known_write_unknown_response_reconciles_without_rewrite(self):
        self.join();self.clock+=300;path,out=self.plan('heartbeat')
        self.google.batch_update_document(**out['tool_arguments']);before=len(self.google.calls)
        self.assertTrue(self.ledger.verify_plan(path,None,self.google.get_document(self.initial['document_id']))['verified'])
        self.assertEqual(len(self.google.calls),before)

    def test_expired_lease_and_closed_queue_cannot_renew(self):
        self.join(latency=26,seconds=60);self.clock=2000000060
        self.assertError('not_active',self.ledger.plan_event,self.source(),'heartbeat',self.path('late'))
        self.assertError('not_active',self.bridge.sync_heartbeat)
        self.bridge.event('close',{'confirm':True})
        self.assertError('closed|not_active',self.ledger.plan_event,self.source(),'heartbeat',self.path('closed'))
        self.assertError('closed',q.transition,self.source().state,self.code,'join','native',
            {'native_task_id':self.ledger.identity,'controller_epoch':secrets.token_hex(16),'lease_expires':2000001000,'capacity':2})

    def test_capacity_and_generation_remain_fenced(self):
        self.join();one=self.demand();two=self.store.admission(self.gen,identity(),select(load_catalog(),'gpt-6-astra','max'))
        self.bridge.prepare_child(two['id']);self.event('claim',route_id=one);self.event('claim',route_id=two['id'])
        three=self.store.admission(self.gen,identity(),select(load_catalog(),'gpt-6.1-sol','high'))
        self.bridge.prepare_child(three['id'])
        self.assertError('slots_exhausted',self.ledger.plan_event,self.source(),'claim',self.path('full'),route_id=three['id'])
        new=self.store.activate(select(load_catalog(),'gpt-6-astra','max'))
        self.assertError('new_native_join',self.store.admission,new,identity(),select(load_catalog(),'gpt-6-astra','max'))

    def test_unresolved_heartbeat_blocks_spawn_exposure_until_exact_reconciliation(self):
        self.join();rid=self.demand();self.event('claim',route_id=rid);self.event('begin',route_id=rid)
        self.clock+=1;path,out=self.plan('heartbeat')
        self.assertError('unresolved',self.ledger.plan_spawn,self.source(),rid,self.path('blocked'),self.package)
        self.google.batch_update_document(**out['tool_arguments'])
        self.ledger.verify_plan(path,None,self.google.get_document(self.initial['document_id']))
        spawn=self.path('spawn');self.ledger.plan_spawn(self.source(),rid,spawn,self.package)
        self.assertTrue(self.ledger.check_spawn(self.source(),spawn)['dispatch_allowed'])

    def test_unresolved_heartbeat_blocks_reserved_spawn_dispatch_without_replacement(self):
        self.join();rid=self.demand();self.event('claim',route_id=rid);self.event('begin',route_id=rid)
        spawn=self.path('spawn');self.ledger.plan_spawn(self.source(),rid,spawn,self.package)
        self.clock+=1;path,out=self.plan('heartbeat')
        self.assertError('unresolved',self.ledger.check_spawn,self.source(),spawn)
        self.google.batch_update_document(**out['tool_arguments'])
        self.ledger.verify_plan(path,None,self.google.get_document(self.initial['document_id']))
        self.assertTrue(self.ledger.check_spawn(self.source(),spawn)['dispatch_allowed'])
        self.assertError('already_reserved',self.ledger.plan_spawn,self.source(),rid,self.path('replacement'),self.package)

    def test_spawn_plan_expires_without_replacement_attempt(self):
        self.join();rid=self.demand();self.event('claim',route_id=rid);self.event('begin',route_id=rid)
        path=self.path('spawn');out=self.ledger.plan_spawn(self.source(),rid,path,self.package);prepared=self.clock
        self.assertEqual(out['execute_before'],prepared+10)
        self.clock=prepared+9.999
        self.assertTrue(self.ledger.check_spawn(self.source(),path)['dispatch_allowed'])
        self.clock=prepared+10;self.assertError('dispatch_window',self.ledger.check_spawn,self.source(),path)
        self.assertError('already_reserved',self.ledger.plan_spawn,self.source(),rid,self.path('again'),self.package)

    def test_unknown_native_spawn_stays_reserved_beyond_the_old_freshness_window(self):
        self.join();rid=self.demand();self.event('claim',route_id=rid);self.event('begin',route_id=rid)
        path=self.path('spawn');self.ledger.plan_spawn(self.source(),rid,path,self.package)
        self.clock+=300
        self.event('heartbeat');self.bridge.sync_heartbeat();self.event('unknown',route_id=rid)
        before=len(self.google.calls);source=self.source()
        self.assertTrue(self.store.status()['controller_active'])
        self.assertEqual(source.state['logical']['demands'][rid]['state'],'unknown')
        self.assertError('dispatch_window',self.ledger.check_spawn,source,path)
        self.assertError('already_reserved_no_replay',self.ledger.plan_spawn,source,rid,self.path('again'),self.package)
        restarted=NativeLedger(self.root/'native',source.state,self.code,self.ledger.identity)
        self.assertError('already_reserved_no_replay',restarted.plan_spawn,source,rid,self.path('restart'),self.package)
        self.assertEqual(len(self.google.calls),before)

    def test_future_signed_tick_clock_rollback_latches_before_freshness_verification(self):
        self.join();source=self.source();heartbeat=source.state['logical']['controller']['heartbeat_at']
        self.clock=heartbeat-1
        self.assertError('clock_rollback',self.ledger.inspect,source)
        self.assertError('clock_rollback',mirror_verified_queue,self.store,source.state,self.code)
        self.clock=heartbeat+1
        self.assertError('clock_rollback',self.ledger.inspect,source)
        self.assertError('clock_rollback',self.bridge.sync_heartbeat)

    def test_gateway_expiry_fence_survives_failed_transaction_and_clock_restore(self):
        self.join();heartbeat=self.source().state['logical']['controller']['heartbeat_at']
        self.clock=heartbeat+900
        self.assertError('not_ready',self.store.admission,self.gen,identity(),select(load_catalog(),'gpt-6.1-sol','high'))
        self.clock=heartbeat+899
        self.assertFalse(self.store.status()['controller_active'])
        self.assertError('not_active',self.bridge.sync_heartbeat)

    def test_gateway_observation_highwater_never_refreshes_heartbeat(self):
        self.join();heartbeat=self.source().state['logical']['controller']['heartbeat_at']
        self.clock=heartbeat+100;self.assertTrue(self.store.status()['controller_active'])
        self.clock=heartbeat+99;self.assertFalse(self.store.status()['controller_active'])
        self.clock=heartbeat+101;self.assertFalse(self.store.status()['controller_active'])
        self.assertError('clock_rollback',self.bridge.sync_heartbeat)

    def test_gateway_rollback_fence_is_durable_before_heartbeat_clock(self):
        self.join();heartbeat=self.source().state['logical']['controller']['heartbeat_at']
        self.clock=heartbeat+50;self.bridge.sync_heartbeat();self.clock=heartbeat+20
        self.assertFalse(self.store.status()['controller_active'])
        self.clock=heartbeat+51
        self.assertFalse(self.store.status()['controller_active'])
        self.assertError('clock_rollback',self.bridge.sync_heartbeat)

    def test_mac_local_heartbeat_cannot_forge_signed_native_liveness(self):
        self.join();credentials=self.bridge.sync_heartbeat()
        self.assertError('signed_google',self.store.heartbeat,credentials)

if __name__=='__main__':unittest.main()
