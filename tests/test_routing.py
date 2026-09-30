import concurrent.futures
import copy
import json
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import broker_bootstrap as broker
import probe
from portable import Deployment, FileQueue, QueueError, protocol
from routing import Registry, initialize
from routing_worker import Worker
from test_tool_probe import request as tool_request, intent
from file_queue import request_for_text


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'routing'
        initialize(self.root, 'owner', seconds=300, max_sessions=3)
        self.reg = Registry(self.root)
        self.queues = []

    def tearDown(self):
        for q in self.queues: q.close()
        self.tmp.cleanup()

    def assert_error(self, code, call, *args, **kwargs):
        with self.assertRaises(QueueError) as raised: call(*args, **kwargs)
        self.assertEqual(code, raised.exception.code)

    def session(self, key='a', scope='text_only', confirm=True):
        route = self.reg.create_session('owner', key, scope, seconds=180)
        d = Deployment(self.root / 'sessions' / key)
        probe.observe(d, 'desktop'); probe.offer(d); probe.observe(d, 'broker'); probe.answer(d); probe.verify(d)
        q = FileQueue(d.queue_root, create=True, owner='owner', session='session_' + d.m['run_id'], max_jobs=3)
        self.queues.append(q)
        protocol.atomic_json(d.queue_root / 'ready.json', dict(version=1, instance=uuid.uuid4().hex, deadline=time.time()+180))
        if confirm:
            self.reg.dispatch_started('owner', route['admission_id'], 'synthetic-test-adapter')
            cred = self.reg.confirm('owner', route['admission_id'], 'synthetic/' + key + '/1')
            return d, q, Worker(self.root, cred)
        return d, q, route

    def enqueue(self, q, req):
        return q.enqueue(req, owner='owner', session=q.meta['session'], timeout=60)

    def finish(self, q, worker, text):
        ticket = worker.claim(0, 30); worker.read(ticket)
        worker.complete(ticket, dict(kind='message', text=text))
        q.delivery(ticket['broker_ticket']['job']['id'], owner='owner', session=q.meta['session'], value='delivered')
        return ticket

    def expire(self, key):
        with self.reg.locked() as state:
            state['sessions'][key]['lease_until'] = time.time() - 1
            self.reg.save(state)

    def replacement(self, key='a'):
        route = self.reg.failover('owner', key)
        self.reg.dispatch_started('owner', route['admission_id'], 'synthetic-test-adapter')
        cred = self.reg.confirm('owner', route['admission_id'], 'synthetic/' + key + '/' + str(route['generation']))
        return Worker(self.root, cred)

    def test_multiple_sessions_direct_sticky_turns(self):
        da, qa, wa = self.session('a'); db, qb, wb = self.session('b')
        identity_a, identity_b = copy.deepcopy(wa.credential), copy.deepcopy(wb.credential)
        for turn in range(2):
            self.enqueue(qa, request_for_text('A' + str(turn))); self.enqueue(qb, request_for_text('B' + str(turn)))
            with patch.object(Registry, 'create_session', side_effect=AssertionError('router called')), \
                 patch.object(Registry, 'confirm', side_effect=AssertionError('admission called')), \
                 patch.object(Registry, 'failover', side_effect=AssertionError('failover called')):
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    a = pool.submit(self.finish, qa, wa, 'A-result-' + str(turn))
                    b = pool.submit(self.finish, qb, wb, 'B-result-' + str(turn))
                    ta, tb = a.result(), b.result()
            self.assertNotEqual(ta['broker_ticket']['job']['id'], tb['broker_ticket']['job']['id'])
        self.assertEqual(identity_a, wa.credential); self.assertEqual(identity_b, wb.credential)
        self.assertNotEqual(wa.credential['worker_instance'], wb.credential['worker_instance'])
        snapshot = json.dumps(self.reg.snapshot())
        self.assertNotIn('A-result', snapshot); self.assertNotIn('B-result', snapshot)
        self.assertEqual(2, len(self.reg.snapshot()['admissions']))

    def test_scope_identity_and_reopen(self):
        d, q, worker = self.session()
        self.assertEqual(worker.credential, Registry(self.root).credential(self.reg.snapshot()['sessions']['a']))
        bad = dict(worker.credential, run_id='0'*32)
        self.assert_error('stale_worker_generation', Worker(self.root, bad).heartbeat)
        self.assert_error('owner_mismatch', self.reg.create_session, 'other', 'b')
        self.assert_error('invalid_label', self.reg.create_session, 'owner', '../escape')
        self.assert_error('session_options_conflict', self.reg.create_session, 'owner', 'a', 'tool_probe')

    def test_create_idempotent_and_concurrent(self):
        with concurrent.futures.ThreadPoolExecutor() as pool:
            results = list(pool.map(lambda _: Registry(self.root).create_session('owner', 'same'), range(4)))
        self.assertEqual(1, len({r['admission_id'] for r in results}))
        self.assertEqual(1, len(self.reg.snapshot()['admissions']))

    def test_admission_needs_actual_parent_confirmation(self):
        d, q, route = self.session(confirm=False)
        self.assert_error('admission_not_dispatched', self.reg.confirm, 'owner', route['admission_id'], 'synthetic/a')
        self.assert_error('worker_not_active', Worker(self.root, self.reg.credential(route)).claim, 0)
        self.reg.dispatch_started('owner', route['admission_id'], 'synthetic')
        self.assert_error('admission_already_dispatched', self.reg.dispatch_started, 'owner', route['admission_id'], 'synthetic')
        self.reg.failed_admission('owner', route['admission_id'], 'unknown', 'synthetic/unknown')
        self.assert_error('admission_not_safely_failed', self.reg.retry_admission, 'owner', 'a')
        cred = self.reg.confirm('owner', route['admission_id'], 'synthetic/found')
        self.assertEqual(cred, self.reg.confirm('owner', route['admission_id'], 'synthetic/found'))

    def test_bounded_rejected_admissions(self):
        route = self.reg.create_session('owner', 'a')
        for gen in range(1, 4):
            self.assertEqual(gen, route['generation'])
            self.reg.failed_admission('owner', route['admission_id'], 'not_started', 'synthetic/rejected')
            if gen < 3: route = self.reg.retry_admission('owner', 'a')
        self.assert_error('admission_budget_exhausted', self.reg.retry_admission, 'owner', 'a')
        self.assertEqual(3, len(self.reg.snapshot()['admissions']))

    def test_safe_idle_failover_fences_every_old_operation(self):
        d, q, old = self.session()
        self.enqueue(q, request_for_text('one')); old_ticket = self.finish(q, old, 'done')
        self.assert_error('worker_still_live', self.reg.failover, 'owner', 'a')
        old.close(); new = self.replacement()
        self.assertEqual(2, new.credential['generation'])
        for fn, args in [(old.heartbeat, ()), (old.claim, (0,)), (old.read, (old_ticket,)),
                         (old.renew, (old_ticket,)), (old.complete, (old_ticket, dict(kind='message', text='done'))),
                         (old.close, ())]:
            self.assert_error('stale_worker_generation', fn, *args)
        self.enqueue(q, request_for_text('two')); self.finish(q, new, 'replacement')

    def test_expiry_never_resurrects(self):
        d, q, old = self.session(); self.expire('a')
        self.assert_error('worker_lease_expired', old.heartbeat)
        self.assert_error('worker_lease_expired', old.claim, 0)
        new = self.replacement()
        self.assertEqual(2, new.credential['generation'])

    def test_started_unknown_blocks_even_after_cancel(self):
        d, q, worker = self.session()
        self.enqueue(q, request_for_text('unknown')); ticket = worker.claim(0); worker.read(ticket)
        q.cancel(ticket['broker_ticket']['job']['id'], owner='owner', session=q.meta['session'])
        self.expire('a')
        self.assert_error('ambiguous_inference_requires_review', self.reg.failover, 'owner', 'a')
        self.assertEqual(1, self.reg.snapshot()['sessions']['a']['generation'])

    def test_pre_read_claim_can_recover_only_after_lease(self):
        d, q, old = self.session()
        self.enqueue(q, request_for_text('unread')); ticket = old.claim(0, .05); self.expire('a')
        self.assert_error('prior_job_lease_active', self.reg.failover, 'owner', 'a')
        time.sleep(.06); new = self.replacement()
        new_ticket = new.claim(0); self.assertEqual(ticket['broker_ticket']['job']['id'], new_ticket['broker_ticket']['job']['id'])
        new.read(new_ticket); new.complete(new_ticket, dict(kind='message', text='safe'))

    def test_tool_receipt_direct_then_no_replay_on_failover(self):
        d, q, worker = self.session(scope='tool_probe')
        self.enqueue(q, tool_request()); first = worker.claim(0); worker.read(first); worker.complete(first, intent())
        jid = first['broker_ticket']['job']['id']; q.delivery(jid, owner='owner', session=q.meta['session'], value='delivered')
        self.expire('a')
        self.assert_error('unresolved_tool_execution', self.reg.failover, 'owner', 'a')
        call = protocol.validate_result(intent(), tool_request(), jid)
        req = tool_request('receipt')
        req['input'] += [call, dict(type='function_call_output', call_id=call['call_id'],
                                   output='Wall time: 0.020 seconds\nProcess exited with code 0\nOutput:\nTOOL_NONCE=0123456789abcdef01234567\n')]
        self.enqueue(q, req)
        new = self.replacement(); receipt = new.claim(0); actual = new.read(receipt)
        self.assertEqual(call['call_id'], actual['request']['input'][-1]['call_id'])
        self.assert_error('tool_probe_single_invocation_limit', new.complete, receipt, intent())
        new.complete(receipt, dict(kind='message', text='TOOL_OK:76543210fedcba9876543210'))

    def test_legacy_broker_adapter_cannot_bypass_fence(self):
        d, q, worker = self.session()
        a = self.reg.snapshot()['sessions']['a']['assignment']
        self.assert_error('routed_session_requires_fenced_adapter', d.assign, 'owner', 'broker', 'rogue', native_attested=True)
        self.assert_error('routed_session_requires_fenced_adapter', broker.claim, d, a, 0)
        with self.reg.locked() as state:
            guarded = self.reg.deployment(state, state['sessions']['a'])
        self.assert_error('routed_session_requires_fenced_adapter', broker.claim, guarded, a, 0)
        self.assert_error('registry_lock_scope_required', self.reg.deployment, state, state['sessions']['a'])


if __name__ == '__main__': unittest.main()
