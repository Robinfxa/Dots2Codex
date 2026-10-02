"""Synthetic client/worker fixture. Real local HTTP, no Mac/native inference."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from facade.runtime import create_runtime


def config(directory):
    return {'db_path': str(Path(directory) / 'queue.sqlite3'),
            'approval_ref': 'explicit-offline-fixture', 'not_before': time.time() - 1,
            'expires_at': time.time() + 3600,
            'binding': {'grant_id': 'fixture-grant', 'route_id': 'fixture-route',
                        'session_id': 'fixture-session', 'thread_id': 'fixture-thread',
                        'model': 'gpt-6.1-sol', 'reasoning_effort': 'xhigh'},
            'client_actor': 'fixture-codex', 'worker_actor': 'fixture-native',
            'context_epoch': 'fixture-epoch', 'http_bearer': 'ephemeral-fixture-only-bearer',
            'max_wait_ms': 5000, 'http_wait_ms': 10000}


def request():
    return {'model': 'gpt-6.1-sol', 'reasoning': {'effort': 'xhigh'}, 'stream': True,
            'instructions': 'Synthetic repeated context. ' * 1000,
            'input': [{'role': 'user', 'content': 'Perform three synthetic callbacks.'}],
            'tools': [{'type': 'function', 'name': 'fixture_probe',
                       'description': 'Offline fixture only, never executes commands.',
                       'parameters': {'type': 'object', 'properties': {'round': {'type': 'integer'}},
                                      'required': ['round'], 'additionalProperties': False}}]}


def response(round_number, final=False):
    if final:
        item = {'id': 'msg_final', 'type': 'message', 'role': 'assistant',
                'content': [{'type': 'output_text', 'text': 'FIXTURE_COMPLETE'}]}
    else:
        item = {'id': f'fc_fixture{round_number}', 'type': 'function_call',
                'call_id': f'fixture-call-{round_number}', 'name': 'fixture_probe',
                'arguments': json.dumps({'round': round_number})}
    return {'id': f'resp_fixture{round_number}', 'object': 'response',
            'status': 'completed', 'model': 'gpt-6.1-sol', 'output': [item]}


def http_post(port, bearer, req, request_id=None):
    client = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
    headers = {'Authorization': 'Bearer ' + bearer, 'Content-Type': 'application/json'}
    if request_id:
        headers['X-Dots-Request-Id'] = request_id
    client.request('POST', '/v1/responses', json.dumps(req), headers)
    result = client.getresponse()
    status, raw = result.status, result.read()
    client.close()
    if status != 200:
        return status, json.loads(raw)
    for block in raw.decode().split('\n\n'):
        if block.startswith('event: response.completed\n'):
            return status, json.loads(block.split('data: ', 1)[1])['response']
    raise AssertionError('missing completed SSE event')


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = config(self.temp.name)
        self.runtime = create_runtime(self.cfg)
        self.runtime.start_http()
        self.port = self.runtime.http.server_address[1]

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def call(self, tool_name, **args):
        return self.runtime.call_tool(tool_name, args)

    def test_three_http_callbacks_one_actor_one_bootstrap(self):
        results, errors = [], []
        initial = request()
        def client():
            current = copy.deepcopy(initial)
            try:
                for i in range(4):
                    code, emitted = http_post(self.port, self.cfg['http_bearer'], current)
                    self.assertEqual(code, 200, emitted)
                    results.append(emitted)
                    if i < 3:
                        call = emitted['output'][0]
                        current['input'] += [call, {'type': 'function_call_output',
                            'call_id': call['call_id'], 'output': f'SYNTHETIC_FIXTURE_OUTPUT_{i + 1}'}]
            except BaseException as exc:
                errors.append(exc)
        client_thread = threading.Thread(target=client)
        client_thread.start()
        current = self.call('get_request', wait_ms=5000)
        self.assertEqual(current['context']['kind'], 'full')
        replay = self.call('get_request', wait_ms=0)
        self.assertEqual(replay['context_token'], current['context_token'])
        discovery = self.call('discover_tools', request_id=current['request_id'],
                              context_token=current['context_token'], query='fixture_probe')
        match = discovery['matches'][0]
        schema = self.call('lookup_schema', request_id=current['request_id'],
                           context_token=current['context_token'], name=match['key'],
                           sha256=match['schema_sha256'])
        for i in range(1, 4):
            current = self.call('submit_action_and_wait_result', request_id=current['request_id'],
                action_id=f'fixture-action-{i}', context_token=current['context_token'],
                schema_tokens=[schema['schema_token']] if i == 1 else [],
                response=response(i), wait_ms=5000)
            self.assertEqual(current['status'], 'ready')
            self.assertEqual(current['context']['kind'], 'delta')
            self.assertNotIn('history', current['context'])
            self.assertEqual(len(current['context']['append']), 2)
            self.assertEqual(current['context']['append'][-1]['output'], f'SYNTHETIC_FIXTURE_OUTPUT_{i}')
        self.call('finish_request', request_id=current['request_id'], action_id='fixture-final',
                  context_token=current['context_token'], response=response(4, final=True))
        client_thread.join(5)
        self.assertFalse(client_thread.is_alive())
        if errors:
            raise errors[0]
        self.assertEqual(len(results), 4)
        events = self.runtime.ledger
        self.assertEqual(sum(e['event'] == 'logical_native_actor_bound' for e in events), 1)
        context_events = [e for e in events if e['event'] == 'native_context_return_prepared']
        self.assertEqual([e['kind'] for e in context_events], ['full', 'delta', 'delta', 'delta'])
        self.assertTrue(all(e['payload_bytes'] < context_events[0]['payload_bytes'] / 5
                            for e in context_events[1:]))

    def test_wait_timeout_does_not_execute_or_retry(self):
        self.assertEqual(self.call('get_request', wait_ms=0)['status'], 'pending')
        self.assertEqual(self.runtime.requests, [])
        self.assertEqual(self.runtime.actions, {})

    def test_http_auth_and_host_binding(self):
        status, payload = http_post(self.port, 'wrong-fixture-token', request())
        self.assertEqual(status, 401)
        self.assertEqual(self.runtime.requests, [])

    def test_cancel_queued_prevents_native_delivery(self):
        record = self.runtime.ingest(request())
        result = self.call('cancel_request', request_id=record['id'])
        self.assertEqual(result['state'], 'cancelled')
        with self.assertRaisesRegex(ValueError, 'request_not_executable'):
            self.call('get_request', wait_ms=0)

    def test_conflicting_duplicate_and_unknown_callback_rejected(self):
        record = self.runtime.ingest(request(), 'fixture-id')
        altered = request()
        altered['instructions'] += 'changed'
        with self.assertRaisesRegex(ValueError, 'request_id_conflict'):
            self.runtime.ingest(altered, 'fixture-id')
        altered = request()
        altered['input'] += [{'type': 'function_call_output', 'call_id': 'unknown', 'output': 'fake'}]
        with self.assertRaisesRegex(ValueError, 'uncorrelated_tool_output'):
            self.runtime.ingest(altered)


    def prepared_action(self):
        self.runtime.ingest(request())
        current = self.call('get_request', wait_ms=0)
        found = self.call('discover_tools', request_id=current['request_id'],
                          context_token=current['context_token'], query='fixture_probe')['matches'][0]
        schema = self.call('lookup_schema', request_id=current['request_id'],
                           context_token=current['context_token'], name=found['key'],
                           sha256=found['schema_sha256'])
        return current, {'request_id': current['request_id'], 'action_id': 'fixture-action',
                         'context_token': current['context_token'],
                         'schema_tokens': [schema['schema_token']], 'response': response(1), 'wait_ms': 0}

    def test_duplicate_action_pending_then_http_one_use_and_callback_retry(self):
        current, action = self.prepared_action()
        self.assertEqual(self.call('submit_action_and_wait_result', **action)['status'], 'pending')
        self.assertEqual(self.call('submit_action_and_wait_result', **action)['status'], 'pending')
        self.assertEqual(sum(e['event'] == 'native_response_committed' for e in self.runtime.ledger), 1)
        status, result = http_post(self.port, self.cfg['http_bearer'], request())
        self.assertEqual(status, 200)
        status, result = http_post(self.port, self.cfg['http_bearer'], request())
        self.assertEqual(status, 409)
        self.assertEqual(result['error'], 'response_delivery_already_started')
        following = request()
        following['input'] += [response(1)['output'][0], {'type': 'function_call_output',
                               'call_id': 'fixture-call-1', 'output': 'SYNTHETIC_RESULT'}]
        self.runtime.ingest(following)
        observed = self.call('await_result', request_id=current['request_id'],
                             action_id=action['action_id'], wait_ms=0)
        replay = self.call('submit_action_and_wait_result', **action)
        self.assertEqual(observed['context_token'], replay['context_token'])
        self.assertEqual(observed['context'], replay['context'])
        conflict = copy.deepcopy(action)
        conflict['response']['id'] = 'resp_changed'
        with self.assertRaisesRegex(ValueError, 'action_id_conflict'):
            self.call('submit_action_and_wait_result', **conflict)
        with self.assertRaisesRegex(ValueError, 'unknown_action'):
            self.call('await_result', request_id=current['request_id'], action_id='unknown', wait_ms=0)

    def test_cancel_claimed_stops_publish_and_cancel_committed_is_explicit_too_late(self):
        current, action = self.prepared_action()
        self.call('cancel_request', request_id=current['request_id'])
        with self.assertRaisesRegex(ValueError, 'request_cancelled'):
            self.call('submit_action_and_wait_result', **action)
        self.assertIsNone(self.runtime.by_id[current['request_id']]['response'])
        self.assertEqual(sum(e['event'] == 'http_response_delivery_started' for e in self.runtime.ledger), 0)

    def test_cancel_after_result_commit_never_claims_rollback(self):
        current, action = self.prepared_action()
        self.call('submit_action_and_wait_result', **action)
        receipt = self.call('cancel_request', request_id=current['request_id'])
        self.assertEqual(receipt['status'], 'too_late_result_committed')
        self.assertEqual(receipt['effect'], 'unknown')

    def test_restart_refuses_reserved_inference_and_does_not_emit_again(self):
        current, action = self.prepared_action()
        replacement = create_runtime(self.cfg)
        try:
            replacement.ingest(request())
            with self.assertRaisesRegex(ValueError, 'execution_admission_unknown_no_replay'):
                replacement.call_tool('get_request', {'wait_ms': 0})
            self.assertFalse(replacement.actions)
        finally:
            replacement.close()

    def test_bad_wait_parameter_cannot_commit_and_actor_fields_rejected(self):
        current, action = self.prepared_action()
        bad = {**action, 'wait_ms': 5001}
        with self.assertRaisesRegex(ValueError, 'invalid_wait_ms'):
            self.call('submit_action_and_wait_result', **bad)
        self.assertFalse(self.runtime.actions)
        with self.assertRaisesRegex(ValueError, 'unknown_tool_or_arguments'):
            self.call('bridge_status', actor_id='different-actor')

    def test_mutated_callback_rejected_without_poisoning_context(self):
        current, action = self.prepared_action()
        self.call('submit_action_and_wait_result', **action)
        self.assertEqual(http_post(self.port, self.cfg['http_bearer'], request())[0], 200)
        following = request()
        following['input'] += [response(1)['output'][0], {'type': 'function_call_output',
                               'call_id': 'fixture-call-1', 'output': 'SYNTHETIC_RESULT'}]
        mutated = copy.deepcopy(following)
        mutated['input'][1]['arguments'] = '{"round": 42}'
        with self.assertRaisesRegex(ValueError, 'tool_history_does_not_match_delivered_calls'):
            self.runtime.ingest(mutated)
        self.assertEqual(self.runtime.context.revision['revision'], 1)
        self.assertEqual(self.runtime.ingest(following)['seq'], 2)



    def test_non_ascii_auth_and_huge_content_length_fail_closed(self):
        import socket
        for auth, length, expected in [('Bearer \u00e9', '2', 401),
            ('Bearer ' + self.cfg['http_bearer'], '9' * 5000, 413)]:
            with socket.create_connection(('127.0.0.1', self.port), timeout=2) as client:
                body = (f'POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'
                        f'Authorization: {auth}\r\nContent-Length: {length}\r\n\r\n').encode('latin-1')
                client.sendall(body)
                header = client.recv(4096).split(b'\r\n', 1)[0]
                self.assertIn(str(expected).encode(), header)
        self.assertFalse(self.runtime.requests)



    def test_exact_inner_argument_numbers_cannot_bypass_schema(self):
        from facade.wire import validate_response
        source = request()
        source['tools'][0]['parameters'] = {'type': 'object', 'properties': {
            'x': {'type': 'number', 'maximum': .1}}, 'required': ['x']}
        candidate = response(1)
        candidate['output'][0]['arguments'] = '{"x":0.1000000000000000000001}'
        with self.assertRaisesRegex(ValueError, 'numeric_precision_loss'):
            validate_response(candidate, source)
        candidate['output'][0]['arguments'] = '{"x":0.1}'
        self.assertEqual(validate_response(candidate, source), candidate)



    def test_duplicate_snapshot_preserves_json_type_identity(self):
        first = request()
        first['metadata'] = {'fixture': True}
        self.runtime.ingest(first, 'typed-fixture')
        second = copy.deepcopy(first)
        second['metadata']['fixture'] = 1
        with self.assertRaisesRegex(ValueError, 'request_id_conflict'):
            self.runtime.ingest(second, 'typed-fixture')


if __name__ == '__main__':
    unittest.main()
