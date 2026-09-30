#!/usr/bin/env python3
"""Independent synthetic routing review. Never invokes native inference or Codex."""
import copy
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
PACKAGE = BASE
sys.path.insert(0, str(PACKAGE))
import routing
from routing import Registry
from routing_worker import Worker
from portable import Deployment, FileQueue, QueueError, protocol
import broker_bootstrap as broker
import probe


def request(text='shared prompt'):
    return {'model':'native-subagent-bridge', 'stream':True,
            'input':[{'role':'user','content':[{'type':'input_text','text':text}]}]}

class RoutingIndependent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sticky-routing-independent-')
        self.root = Path(self.tmp.name) / 'registry'
        routing.initialize(self.root, 'owner', seconds=90, max_sessions=8)
        self.reg = Registry(self.root)
        self.queues = []

    def tearDown(self):
        for q in self.queues: q.close()
        self.tmp.cleanup()

    def session(self, key='a', native=None, scope='text_only', lease=30):
        route = self.reg.create_session('owner', key, scope=scope, seconds=60)
        self.reg.dispatch_started('owner', route['admission_id'], 'synthetic-adapter')
        cred = self.reg.confirm('owner', route['admission_id'], native or 'synthetic-task-' + key, lease=lease)
        return Worker(self.root, cred), route

    def queue(self, key='a'):
        d = Deployment(self.root/'sessions'/key)
        probe.observe(d,'desktop'); probe.offer(d); probe.observe(d,'broker'); probe.answer(d); probe.verify(d)
        q = FileQueue(d.queue_root, create=True, owner='owner', session='session_'+d.m['run_id'], max_jobs=3)
        self.queues.append(q)
        protocol.atomic_json(d.queue_root/'ready.json', {'version':1, 'instance':uuid.uuid4().hex, 'deadline':time.time()+45})
        return d, q

    def enqueue(self, q, text='shared prompt'):
        return q.enqueue(request(text), owner='owner', session=q.meta['session'], timeout=30)

    def expire(self, key='a', expire_job=False):
        with self.reg.locked() as state:
            route = state['sessions'][key]
            route['lease_until'] = time.time()-1
            self.reg.save(state)
        d = Deployment(self.root/'sessions'/key)
        with d.locked() as state:
            state['roles']['broker']['expires'] = time.time()-1
            d.save(state)
        if expire_job:
            q = d.queue()
            try:
                with q.locked():
                    for job in q.states():
                        if job['state']=='running':
                            job['lease_until']=time.time()-1
                            q.save(job)
            finally:q.close()

    def replacement(self, key='a'):
        route = self.reg.failover('owner',key)
        self.reg.dispatch_started('owner',route['admission_id'],'synthetic-adapter')
        cred=self.reg.confirm('owner',route['admission_id'],'synthetic-replacement-'+key)
        return Worker(self.root,cred),route

    def test_01_identical_payloads_remain_session_isolated(self):
        wa,ra=self.session('a'); wb,rb=self.session('b')
        da,qa=self.queue('a'); db,qb=self.queue('b')
        ja=self.enqueue(qa); jb=self.enqueue(qb)
        barrier=threading.Barrier(2); outputs={}; errors=[]
        def run(k,w):
            try:
                barrier.wait();t=w.claim(0);inputs=w.read(t);outputs[k]=(t,inputs);w.complete(t,{'kind':'message','text':'only-'+k})
            except Exception as exc:errors.append(exc)
        threads=[threading.Thread(target=run,args=(k,w)) for k,w in [('a',wa),('b',wb)]]
        for t in threads:t.start()
        for t in threads:t.join(5);self.assertFalse(t.is_alive())
        self.assertFalse(errors,errors)
        self.assertEqual(outputs['a'][0]['broker_ticket']['job']['id'],ja['id'])
        self.assertEqual(outputs['b'][0]['broker_ticket']['job']['id'],jb['id'])
        self.assertEqual(qa.get(ja['id'],owner='owner',session=qa.meta['session'])['result']['text'],'only-a')
        self.assertEqual(qb.get(jb['id'],owner='owner',session=qb.meta['session'])['result']['text'],'only-b')
        self.assertNotEqual(wa.credential['worker_instance'],wb.credential['worker_instance'])
        self.assertNotEqual(ra['run_id'],rb['run_id'])
        self.assertEqual(len(self.reg.snapshot()['admissions']),2)

    def test_02_cross_session_ticket_rejected_on_every_operation(self):
        wa,_=self.session('a');wb,_=self.session('b');_,qa=self.queue('a');self.queue('b');self.enqueue(qa);t=wa.claim(0)
        for op in [lambda:wb.read(t),lambda:wb.renew(t),lambda:wb.complete(t,{'kind':'message','text':'x'}),lambda:wb.status(t)]:
            with self.assertRaises(QueueError):op()
        self.assertEqual(wa.read(t)['request'],request())

    def test_03_credential_correlation_all_fields_fenced(self):
        w,_=self.session();self.queue()
        for k in routing.CORRELATION:
            c=copy.deepcopy(w.credential);c[k]=2 if k in ('generation','broker_epoch') else 'wrong'
            with self.assertRaises(QueueError,msg=k):Worker(self.root,c).heartbeat()

    def test_04_expired_worker_cannot_renew_or_complete(self):
        w,_=self.session();_,q=self.queue();self.enqueue(q);t=w.claim(0);w.read(t);self.expire()
        for op in [lambda:w.heartbeat(),lambda:w.renew(t),lambda:w.complete(t,{'kind':'message','text':'late'}),lambda:w.read(t),lambda:w.claim(0)]:
            with self.assertRaises(QueueError):op()
        with self.assertRaises(QueueError) as caught:self.reg.failover('owner','a')
        self.assertEqual(caught.exception.code,'ambiguous_inference_requires_review')

    def test_05_safe_pre_read_failover_fences_old_generation(self):
        w,_=self.session();_,q=self.queue();j=self.enqueue(q);t=w.claim(0);self.expire(expire_job=True)
        replacement,route=self.replacement();self.assertEqual(route['generation'],2)
        for op in [lambda:w.heartbeat(),lambda:w.read(t),lambda:w.renew(t),lambda:w.close()]:
            with self.assertRaises(QueueError):op()
        nt=replacement.claim(0);self.assertEqual(nt['broker_ticket']['job']['id'],j['id']);replacement.read(nt)
        replacement.complete(nt,{'kind':'message','text':'replacement'})

    def test_06_dispatch_unknown_never_auto_retries(self):
        r=self.reg.create_session('owner','a');self.reg.dispatch_started('owner',r['admission_id'],'synthetic-adapter')
        self.reg.failed_admission('owner',r['admission_id'],'unknown','synthetic-uncertain')
        for op in [lambda:self.reg.retry_admission('owner','a'),lambda:self.reg.dispatch_started('owner',r['admission_id'],'synthetic-adapter'),lambda:self.reg.failover('owner','a')]:
            with self.assertRaises(QueueError):op()
        self.assertEqual(len(self.reg.snapshot()['admissions']),1)

    def test_07_found_native_task_resolves_uncertainty_without_respawn(self):
        r=self.reg.create_session('owner','a');self.reg.dispatch_started('owner',r['admission_id'],'synthetic-adapter')
        self.reg.failed_admission('owner',r['admission_id'],'unknown','synthetic-uncertain')
        c=self.reg.confirm('owner',r['admission_id'],'synthetic-found-task')
        self.assertEqual(c['generation'],1);self.assertEqual(len(self.reg.snapshot()['admissions']),1)

    def test_08_native_task_context_cannot_cross_session_or_generation(self):
        w,_=self.session(native='synthetic-one-context');r=self.reg.create_session('owner','b');self.reg.dispatch_started('owner',r['admission_id'],'synthetic-adapter')
        with self.assertRaises(QueueError):self.reg.confirm('owner',r['admission_id'],'synthetic-one-context')
        w.close();r=self.reg.failover('owner','a');self.reg.dispatch_started('owner',r['admission_id'],'synthetic-adapter')
        with self.assertRaises(QueueError):self.reg.confirm('owner',r['admission_id'],'synthetic-one-context')

    def test_09_binding_commit_crash_adopts_exact_assignment(self):
        r=self.reg.create_session('owner','a');self.reg.dispatch_started('owner',r['admission_id'],'synthetic-adapter');real=self.reg.save
        def fail_active(state):
            if state['sessions']['a']['phase']=='active':raise OSError('synthetic before active commit')
            return real(state)
        with patch.object(self.reg,'save',side_effect=fail_active):
            with self.assertRaises(OSError):self.reg.confirm('owner',r['admission_id'],'synthetic-task')
        d=Deployment(self.root/'sessions/a')
        with d.locked() as state:before=copy.deepcopy(state['roles']['broker'])
        c=Registry(self.root).confirm('owner',r['admission_id'],'synthetic-task')
        with d.locked() as state:after=state['roles']['broker']
        self.assertEqual(before,after);self.assertEqual(c['assignment_id'],before['assignment_id'])
        with self.assertRaises(QueueError):self.reg.confirm('owner',r['admission_id'],'synthetic-other-task')

    def test_10_legacy_entrypoint_rejected_for_routed_session(self):
        w,_=self.session();d,q=self.queue();self.enqueue(q)
        with d.locked() as state:a=copy.deepcopy(state['roles']['broker'])
        for op in [lambda:d.assign('owner','broker','rogue',native_attested=True),lambda:broker.claim(d,a,0),lambda:d.heartbeat(a)]:
            with self.assertRaises(QueueError) as caught:op()
            self.assertEqual(caught.exception.code,'routed_session_requires_fenced_adapter')

    def test_11_guard_revoked_when_registry_lock_exits(self):
        w,_=self.session();self.queue()
        with self.reg.locked() as state:
            route=state['sessions']['a'];guarded=self.reg.deployment(state,route);a=copy.deepcopy(route['assignment'])
        with self.assertRaises(QueueError):broker.claim(guarded,a,0)

    def test_12_guard_does_not_cross_threads(self):
        w,_=self.session();self.queue();results=[]
        with self.reg.locked() as state:
            route=state['sessions']['a'];guarded=self.reg.deployment(state,route);a=copy.deepcopy(route['assignment'])
            def other():
                try:broker.claim(guarded,a,0);results.append('accepted')
                except QueueError as exc:results.append(exc.code)
            t=threading.Thread(target=other);t.start();t.join(3);self.assertFalse(t.is_alive())
        self.assertEqual(results,['routed_session_requires_fenced_adapter'])

    def test_13_begin_marker_crash_never_reveals_request_twice(self):
        w,_=self.session();d,q=self.queue();self.enqueue(q);t=w.claim(0);real=Deployment.save
        def fail_after_start(instance,state):
            real(instance,state)
            if any(x['status']=='started' for x in state['dispatch'].values()):raise OSError('synthetic after durable begin')
        with patch.object(Deployment,'save',fail_after_start):
            with self.assertRaises(OSError):w.read(t)
        with self.assertRaises(QueueError):w.read(t)
        self.expire(expire_job=True)
        with self.assertRaises(QueueError):self.reg.failover('owner','a')

    def test_14_completion_commit_crash_exact_retry_only(self):
        w,_=self.session();d,q=self.queue();self.enqueue(q);t=w.claim(0);w.read(t);real=Deployment.save
        def fail_completed(instance,state):
            if any(x['status']=='completed' for x in state['dispatch'].values()):raise OSError('synthetic after queue completion')
            return real(instance,state)
        with patch.object(Deployment,'save',fail_completed):
            with self.assertRaises(OSError):w.complete(t,{'kind':'message','text':'exact'})
        self.assertEqual(w.status(t)['state'],'completed')
        self.assertTrue(w.complete(t,{'kind':'message','text':'exact'})['idempotent'])
        with self.assertRaises(QueueError):w.complete(t,{'kind':'message','text':'changed'})

    def test_15_ambiguous_delivery_never_redelivered_on_failover(self):
        w,_=self.session();_,q=self.queue();j=self.enqueue(q);t=w.claim(0);w.read(t);w.complete(t,{'kind':'message','text':'exact'});self.expire()
        with self.assertRaises(QueueError) as caught:self.reg.failover('owner','a')
        self.assertEqual(caught.exception.code,'ambiguous_delivery_requires_review')
        q.delivery(j['id'],owner='owner',session=q.meta['session'],value='ambiguous')
        with self.assertRaises(QueueError):self.reg.failover('owner','a')

    def test_16_concurrent_confirm_is_idempotent(self):
        r=self.reg.create_session('owner','a');self.reg.dispatch_started('owner',r['admission_id'],'synthetic-adapter');barrier=threading.Barrier(4);values=[];errors=[]
        def run():
            try:barrier.wait();values.append(Registry(self.root).confirm('owner',r['admission_id'],'synthetic-shared-result'))
            except Exception as exc:errors.append(exc)
        ts=[threading.Thread(target=run) for _ in range(4)]
        for t in ts:t.start()
        for t in ts:t.join(5);self.assertFalse(t.is_alive())
        self.assertFalse(errors,errors);self.assertEqual(len(values),4);self.assertTrue(all(x==values[0] for x in values))
        self.assertEqual(self.reg.snapshot()['sessions']['a']['broker_epoch'],1)

    def test_17_safe_failure_retry_never_reuses_generation(self):
        r=self.reg.create_session('owner','a');self.reg.failed_admission('owner',r['admission_id'],'not_started','synthetic-pre-dispatch')
        nr=self.reg.retry_admission('owner','a');self.assertEqual(nr['generation'],2);self.assertEqual(nr['broker_epoch'],1)
        with self.assertRaises(QueueError):self.reg.dispatch_started('owner',r['admission_id'],'synthetic-adapter')
        self.reg.dispatch_started('owner',nr['admission_id'],'synthetic-adapter');c=self.reg.confirm('owner',nr['admission_id'],'synthetic-second');self.assertEqual(c['generation'],2)

    def test_18_wrong_owner_cannot_allocate_bind_or_failover(self):
        r=self.reg.create_session('owner','a')
        for op in [lambda:self.reg.create_session('other','b'),lambda:self.reg.dispatch_started('other',r['admission_id'],'synthetic-adapter'),lambda:self.reg.confirm('other',r['admission_id'],'synthetic-task'),lambda:self.reg.failover('other','a')]:
            with self.assertRaises(QueueError):op()

    def tool_request(self, text='tool probe'):
        req=request(text)
        req['tools']=[{'type':'function','name':'exec_command','parameters':{'type':'object','properties':{'cmd':{'type':'string'},'login':{'type':'boolean'},'sandbox_permissions':{'type':'string','enum':['use_default','require_escalated']},'yield_time_ms':{'type':'number'},'max_output_tokens':{'type':'number'}},'required':['cmd'],'additionalProperties':False}}]
        return req

    def tool_first(self):
        import tool_probe
        w,_=self.session(scope='tool_probe');d,q=self.queue();req=self.tool_request()
        q.enqueue(req,owner='owner',session=q.meta['session'],timeout=30)
        t=w.claim(0);w.read(t)
        intent={'kind':'function_call','name':'exec_command','arguments':dict(tool_probe.ARGUMENTS)}
        w.complete(t,intent)
        q.delivery(t['broker_ticket']['job']['id'],owner='owner',session=q.meta['session'],value='delivered')
        call=protocol.validate_result(intent,req,t['broker_ticket']['job']['id'])
        return w,d,q,t,call

    def tool_followup(self,call,call_id=None):
        req=self.tool_request('followup')
        req['input'] += [call,{'type':'function_call_output','call_id':call_id or call['call_id'],'output':'Wall time: 0.020 seconds\nProcess exited with code 0\nOutput:\nTOOL_NONCE=0123456789abcdef01234567\n'}]
        return req

    def test_19_unresolved_issued_tool_blocks_failover(self):
        w,d,q,t,call=self.tool_first();self.expire()
        with self.assertRaises(QueueError) as caught:self.reg.failover('owner','a')
        self.assertEqual(caught.exception.code,'unresolved_tool_execution')
        q.enqueue(self.tool_followup(call,'wrong-call'),owner='owner',session=q.meta['session'],timeout=30)
        with self.assertRaises(QueueError) as caught:self.reg.failover('owner','a')
        self.assertEqual(caught.exception.code,'unresolved_tool_execution')

    def test_20_correlated_tool_receipt_continues_without_reexecuting_tool(self):
        w,d,q,t,call=self.tool_first();follow=self.tool_followup(call)
        job=q.enqueue(follow,owner='owner',session=q.meta['session'],timeout=30);self.expire()
        replacement,r=self.replacement();new=replacement.claim(0);shown=replacement.read(new)
        self.assertEqual(new['broker_ticket']['job']['id'],job['id']);self.assertEqual(shown['request'],follow)
        with self.assertRaises(QueueError):replacement.complete(new,{'kind':'function_call','name':'exec_command','arguments':__import__('tool_probe').ARGUMENTS})
        result=replacement.complete(new,{'kind':'message','text':'TOOL_OK:76543210fedcba9876543210'})
        self.assertEqual(result['state'],'completed');self.assertEqual(result['generation'],2)
        with q.locked():jobs=q.states()
        self.assertEqual(sum(j.get('result',{}).get('kind')=='function_call' for j in jobs),1)

    def test_21_expired_started_receipt_still_blocks_failover(self):
        w,d,q,t,call=self.tool_first();q.enqueue(self.tool_followup(call),owner='owner',session=q.meta['session'],timeout=30)
        t2=w.claim(0);w.read(t2);self.expire(expire_job=True)
        with self.assertRaises(QueueError) as caught:self.reg.failover('owner','a')
        self.assertEqual(caught.exception.code,'ambiguous_inference_requires_review')

    def test_22_failover_cannot_interleave_completion_lock_scope(self):
        w,_=self.session();d,q=self.queue();self.enqueue(q);t=w.claim(0);w.read(t)
        with self.reg.locked() as state:state['sessions']['a']['lease_until']=time.time()+.08;self.reg.save(state)
        entered=threading.Event();release=threading.Event();raced=threading.Event();outcomes=[];real=broker.complete
        def held(*args,**kwargs):entered.set();self.assertTrue(release.wait(2));return real(*args,**kwargs)
        def complete():
            try:outcomes.append(('complete',w.complete(t,{'kind':'message','text':'held'})))
            except Exception as exc:outcomes.append(('complete-error',repr(exc)))
        def failover():
            try:outcomes.append(('failover',Registry(self.root).failover('owner','a')))
            except QueueError as exc:outcomes.append(('failover-error',exc.code))
            finally:raced.set()
        with patch.object(broker,'complete',side_effect=held):
            a=threading.Thread(target=complete);a.start();self.assertTrue(entered.wait(2));time.sleep(.1)
            b=threading.Thread(target=failover);b.start();self.assertFalse(raced.wait(.05));release.set();a.join(3);b.join(3)
        self.assertFalse(a.is_alive());self.assertFalse(b.is_alive())
        self.assertEqual({kind for kind,value in outcomes},{'complete','failover-error'})
        self.assertIn(('failover-error','ambiguous_delivery_requires_review'),outcomes)
        self.assertEqual(self.reg.snapshot()['sessions']['a']['generation'],1)

    def test_23_close_crash_reconciles_exact_closed_assignment(self):
        w,_=self.session();real=w.registry.save
        def fail_closed(state):
            if state['sessions']['a']['phase']=='closed':raise OSError('synthetic after broker close')
            return real(state)
        with patch.object(w.registry,'save',side_effect=fail_closed):
            with self.assertRaises(OSError):w.close()
        w.close()
        self.assertEqual(self.reg.snapshot()['sessions']['a']['phase'],'closed')
        with self.assertRaises(QueueError):w.heartbeat()

    def test_24_explicit_inference_resolution_retires_original_job(self):
        w,_=self.session();_,q=self.queue();j=self.enqueue(q);t=w.claim(0);w.read(t)
        with self.assertRaises(QueueError):self.reg.resolve_inference('owner','a',j['id'],'confirmed_stopped','synthetic-task-status')
        self.expire()
        result=self.reg.resolve_inference('owner','a',j['id'],'confirmed_stopped','synthetic-task-status')
        self.assertFalse(result['original_job_replayed'])
        self.assertEqual(q.get(j['id'],owner='owner',session=q.meta['session'])['state'],'cancelled')
        replacement,_=self.replacement();self.assertIsNone(replacement.claim(0))
        with self.assertRaises(QueueError):w.complete(t,{'kind':'message','text':'late'})

    def test_25_resolution_crash_cannot_replay_original_job(self):
        w,_=self.session();_,q=self.queue();j=self.enqueue(q);t=w.claim(0);w.read(t);self.expire();real=self.reg.save
        def fail_closed(state):
            if state['sessions']['a']['phase']=='closed':raise OSError('synthetic after resolution commit')
            return real(state)
        with patch.object(self.reg,'save',side_effect=fail_closed):
            with self.assertRaises(OSError):self.reg.resolve_inference('owner','a',j['id'],'confirmed_stopped','synthetic-task-status')
        result=self.reg.resolve_inference('owner','a',j['id'],'confirmed_stopped','synthetic-task-status')
        self.assertFalse(result['original_job_replayed'])
        replacement,_=self.replacement();self.assertIsNone(replacement.claim(0))
        self.assertEqual(q.get(j['id'],owner='owner',session=q.meta['session'])['state'],'cancelled')

    def test_26_two_loopback_facades_pin_distinct_canonical_sessions(self):
        import concurrent.futures
        import http.client
        from service_adapter import ReadyService
        wa,_=self.session('a');wb,_=self.session('b');da,qa=self.queue('a');db,qb=self.queue('b')
        services=[]
        try:
            for d in (da,db):
                (d.queue_root/'ready.json').unlink()
                services.append(ReadyService(d.queue_root,lifetime=20,request_deadline=10).start())
            identity={'a':{'session-id':'synthetic-codex-a','thread-id':str(uuid.uuid4())},'b':{'session-id':'synthetic-codex-b','thread-id':str(uuid.uuid4())}}
            def post(index,headers,text='same-wire-prompt'):
                conn=http.client.HTTPConnection('127.0.0.1',services[index].facade.server.server_port,timeout=4)
                try:
                    conn.request('POST','/v1/responses',body=json.dumps(request(text)),headers={'Content-Type':'application/json',**headers})
                    response=conn.getresponse();return response.status,response.read().decode()
                finally:conn.close()
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                a=pool.submit(post,0,identity['a']);b=pool.submit(post,1,identity['b'])
                for key,w in [('a',wa),('b',wb)]:
                    t=w.claim(2);self.assertIsNotNone(t);w.read(t);w.complete(t,{'kind':'message','text':'HTTP-ONLY-'+key})
                ar,br=a.result(),b.result()
            self.assertEqual((ar[0],br[0]),(200,200));self.assertIn('HTTP-ONLY-a',ar[1]);self.assertNotIn('HTTP-ONLY-b',ar[1]);self.assertIn('HTTP-ONLY-b',br[1]);self.assertNotIn('HTTP-ONLY-a',br[1])
            bad_status,bad=post(0,identity['b'],'second')
            self.assertEqual(bad_status,409);self.assertIn('session_scope_mismatch',bad)
            for key,d in [('a',da),('b',db)]:
                saved=json.loads((d.queue_root/'client-session-map.json').read_text())
                self.assertEqual(saved['client'],identity[key])
            self.assertEqual(len(self.reg.snapshot()['admissions']),2)
        finally:
            for service in services:service.close()

    def test_27_live_verifier_rejects_empty_registry(self):
        import verify_routing_live as verifier
        with patch.object(verifier,'verify_freeze',return_value={'aggregate_sha256':'synthetic'}):
            with self.assertRaises(QueueError) as caught:verifier.verify(self.root)
            self.assertEqual(caught.exception.code,'live_expected_sessions_mismatch')

    def test_28_live_snapshot_does_not_refresh_expired_jobs(self):
        from verify_routing_live import SnapshotQueue
        self.session();d,q=self.queue()
        j=q.enqueue(request('expires'),owner='owner',session=q.meta['session'],timeout=.05)
        path=q.path(j['id']);before=path.read_bytes();time.sleep(.06)
        snapshot=SnapshotQueue(d.queue_root)
        try:
            with snapshot.locked():rows=snapshot.states()
        finally:snapshot.close()
        self.assertEqual(rows[0]['state'],'pending');self.assertEqual(path.read_bytes(),before)

    def test_29_live_verifier_rejects_native_identity_reuse(self):
        import verify_routing_live as verifier
        self.session('a');self.session('b')
        with self.reg.locked() as state:
            admissions=list(state['admissions'].values());admissions[1]['native_task_id']=admissions[0]['native_task_id'];self.reg.save(state)
        with patch.object(verifier,'verify_freeze',return_value={'aggregate_sha256':'synthetic'}):
            with self.assertRaises(QueueError) as caught:verifier.verify(self.root)
            self.assertEqual(caught.exception.code,'live_native_context_not_unique')

if __name__=='__main__':unittest.main(verbosity=2)
