import concurrent.futures
from dataclasses import replace
import tempfile
import sqlite3
import unittest
from pathlib import Path

from transport import Binding, Principal, Queue, QueueError, RouteAuthorization, ToolSurface, JsonLoopback


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'queue.sqlite'
        self.now = 1000
        self.q = Queue(self.path, clock=lambda: self.now)
        self.b = Binding('grant', 'route', 'session', 'thread', 'gpt-6.1-sol', 'xhigh')
        self.client = Principal('fixture-client')
        self.worker = Principal('/root/fixture-native')
        self.auth = RouteAuthorization(self.b, self.client.actor_id, self.worker.actor_id, 900, 1100, 'fixture-explicit-approval')
        self.q.install_route(self.auth)
        self.payload = {'history_append': [{'role': 'user', 'content': 'hello'}]}

    def tearDown(self):
        self.temp.cleanup()

    def enqueue(self, rid='request-1', seq=1, **kwargs):
        return self.q.enqueue_request(self.client, self.b, rid, seq, self.payload, **kwargs)

    def claim(self, rid='request-1'):
        return self.q.claim_request(self.worker, self.b, rid)

    def execute(self, rid='request-1'):
        self.claim(rid)
        return self.q.reserve_execution(self.worker, self.b, rid)

    def finish(self, rid='request-1', result=None):
        self.execute(rid)
        return self.q.submit_result(self.worker, self.b, rid, result or {'output': 'hello'})

    def assertError(self, code, method, *args, **kwargs):
        with self.assertRaises(QueueError) as e:
            method(*args, **kwargs)
        self.assertEqual(code, e.exception.code)

    def test_acquisition_retry_does_not_consume_execution_permit(self):
        self.enqueue()
        first = self.claim()
        retry = self.claim()
        self.assertEqual(first['payload'], retry['payload'])
        self.assertEqual(('first', 'replay'), (first['delivery'], retry['delivery']))
        self.assertFalse(first['execution_reserved'])
        self.assertTrue(self.execute()['execute'])
        self.assertFalse(self.execute()['execute'])

    def test_reconnect_after_lost_execution_reply_never_reexecutes(self):
        self.enqueue()
        self.assertTrue(self.execute()['execute'])
        q = Queue(self.path, clock=lambda: self.now)
        self.assertTrue(q.claim_request(self.worker, self.b, 'request-1')['execution_reserved'])
        self.assertFalse(q.reserve_execution(self.worker, self.b, 'request-1')['execute'])

    def test_claims_have_no_timeout_or_reassignment(self):
        self.enqueue()
        self.execute()
        self.now = 1099
        self.assertFalse(self.execute()['execute'])
        self.assertError('actor_not_authorized', self.q.claim_request,
                         Principal('/root/replacement'), self.b, 'request-1')

    def test_concurrent_permit_reservation_wins_once(self):
        self.enqueue()
        self.claim()
        # All contenders share one deliberately active fixture clock.
        def attempt(_):
            return Queue(self.path, clock=lambda: self.now).reserve_execution(self.worker, self.b, 'request-1')['execute']
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(24)))
        self.assertEqual(1, sum(results))

    def test_identical_enqueue_retry_and_mutated_retry(self):
        self.assertEqual(self.enqueue(), self.enqueue())
        self.assertError('request_id_conflict', self.q.enqueue_request, self.client, self.b,
                         'request-1', 1, {'different': True})

    def test_result_retry_is_immutable_and_survives_reconnect(self):
        self.enqueue()
        saved = self.finish()
        q = Queue(self.path)
        self.assertEqual(saved, q.submit_result(self.worker, self.b, 'request-1', {'output': 'hello'}))
        self.assertEqual({'output': 'hello'}, q.get_result(self.client, self.b, 'request-1')['result'])
        self.assertError('immutable_result_conflict', q.submit_result, self.worker, self.b,
                         'request-1', {'output': 'different'})

    def test_result_and_completion_are_one_atomic_commit(self):
        self.enqueue()
        self.execute()
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TRIGGER inject_failure BEFORE UPDATE OF result ON requests "
                       "BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.q.submit_result(self.worker, self.b, 'request-1', {'output': 'hello'})
        pending = self.q.get_result(self.client, self.b, 'request-1')
        self.assertEqual('claimed', pending['state'])
        self.assertIsNone(pending['result_id'])
        self.assertNotIn('result', pending)
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TRIGGER inject_failure')
        self.assertEqual('completed', self.finish()['state'])

    def test_revocation_after_acquisition_before_permit_is_terminal(self):
        self.enqueue()
        self.claim()
        self.q.revoke_route(self.client, self.b)
        self.assertEqual('cancelled', self.q.get_result(self.client, self.b, 'request-1')['state'])
        self.assertNotIn('payload', self.claim())

    def test_three_turn_sequence_with_matching_ack(self):
        ack = None
        executions = 0
        for seq in range(1, 4):
            rid = 'request-' + str(seq)
            self.enqueue(rid, seq, previous_result_id=ack)
            self.claim(rid)
            for _ in range(3):
                permit = self.q.reserve_execution(self.worker, self.b, rid)
                if permit['execute']:
                    executions += 1
            completed = self.q.submit_result(self.worker, self.b, rid, {'seq': seq})
            ack = completed['result_id']
            self.assertEqual({'seq': seq}, self.q.get_result(self.client, self.b, rid)['result'])
        self.assertEqual(3, executions)

    def test_no_next_turn_until_settled_and_acknowledged(self):
        self.enqueue()
        self.execute()
        self.assertError('previous_request_unsettled', self.enqueue, 'request-2', 2)
        done = self.finish()
        self.assertError('previous_result_ack_required', self.enqueue, 'request-2', 2)
        self.enqueue('request-2', 2, previous_result_id=done['result_id'])

    def test_binding_fields_all_pinned(self):
        self.enqueue()
        for field in ('grant_id', 'session_id', 'thread_id', 'model', 'reasoning_effort'):
            wrong = replace(self.b, **{field: 'low' if field == 'reasoning_effort' else 'wrong'})
            self.assertError('binding_not_authorized', self.q.claim_request, self.worker, wrong, 'request-1')
        self.assertError('actor_not_authorized', self.q.claim_request, self.client, self.b, 'request-1')

    def test_route_authorization_cannot_be_replaced(self):
        self.q.install_route(self.auth)
        self.assertError('route_authorization_immutable', self.q.install_route, replace(self.auth, worker_actor='/root/new'))

    def test_schema_is_exact_bound_and_requested(self):
        name = '["filesystem","read"]'
        schema = {'type': 'object', 'properties': {'path': {'type': 'string'}}}
        sha = self.q.put_schema(self.client, self.b, name, schema)
        self.enqueue(schema_refs={name: sha})
        self.claim()
        self.assertEqual(schema, self.q.read_schema(self.worker, self.b, 'request-1', name, sha)['schema'])
        self.assertError('schema_not_authorized_for_request', self.q.read_schema,
                         self.worker, self.b, 'request-1', name, '0' * 64)

    def test_cancel_before_claim_is_terminal(self):
        self.enqueue()
        self.q.cancel_request(self.client, self.b, 'request-1')
        self.assertEqual('cancelled', self.claim()['state'])
        self.assertNotIn('payload', self.claim())
        self.assertError('request_not_executable', self.q.reserve_execution, self.worker, self.b, 'request-1')

    def test_cancel_inflight_blocks_next_and_hides_late_result(self):
        self.enqueue()
        self.execute()
        self.q.cancel_request(self.client, self.b, 'request-1')
        self.assertTrue(self.claim()['cancel_requested'])
        self.assertNotIn('payload', self.claim())
        self.assertError('previous_request_unsettled', self.enqueue, 'request-2', 2)
        result = self.q.submit_result(self.worker, self.b, 'request-1', {'tool_call': 'late-output'})
        self.assertEqual('cancelled', result['state'])
        self.assertNotIn('result', self.q.get_result(self.client, self.b, 'request-1'))
        self.enqueue('request-2', 2, previous_result_id=result['result_id'])

    def test_cancel_between_claim_and_execution_blocks_permit(self):
        self.enqueue()
        self.claim()
        self.q.cancel_request(self.client, self.b, 'request-1')
        self.assertError('request_not_executable', self.q.reserve_execution, self.worker, self.b, 'request-1')
        cancelled = self.q.get_result(self.client, self.b, 'request-1')
        self.assertEqual('cancelled', cancelled['state'])
        self.enqueue('request-2', 2, previous_result_id=cancelled['result_id'])

    def test_revocation_stops_new_work_and_settles_old(self):
        self.enqueue()
        self.execute()
        self.q.revoke_route(self.client, self.b)
        self.assertNotIn('payload', self.claim())
        done = self.q.submit_result(self.worker, self.b, 'request-1', {'output': 'late'})
        self.assertEqual('cancelled', done['state'])
        self.assertError('route_revoked', self.enqueue, 'request-2', 2, previous_result_id=done['result_id'])

    def test_expiry_blocks_new_execution_but_allows_settlement(self):
        self.enqueue()
        self.execute()
        self.now = 1101
        self.assertError('authorization_expired', self.claim)
        self.assertEqual('completed', self.q.submit_result(self.worker, self.b, 'request-1', {'output': 'done'})['state'])

    def test_clock_rollback_blocks_new_work(self):
        self.enqueue()
        self.now = 998
        self.assertError('clock_rollback_new_work_paused', self.claim)

    def test_submit_without_execution_permit_rejected(self):
        self.enqueue()
        self.claim()
        self.assertError('execution_not_reserved', self.q.submit_result, self.worker, self.b, 'request-1', {})

    def test_surface_never_accepts_caller_asserted_authority(self):
        loop = JsonLoopback(ToolSurface(self.q, self.b), self.worker)
        self.assertError('authority_cannot_come_from_wire', loop.call, 'claim_request', request_id='x', principal='admin')
        self.assertError('unknown_method', loop.call, 'reserve_execution', request_id='x')

    def test_payload_limit_and_nonfinite_json(self):
        self.assertError('invalid_or_oversize_payload', self.q.enqueue_request, self.client, self.b,
                         'big', 1, {'text': 'x' * (1024 * 1024)})
        self.assertError('invalid_json', self.q.enqueue_request, self.client, self.b, 'nan', 1, {'x': float('nan')})


if __name__ == '__main__':
    unittest.main()
