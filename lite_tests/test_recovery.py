"""Offline existing-request retrieval/retry regressions; no native/provider I/O."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from dots_lite.gateway import MacGateway, ResponsesServer, recovery_token
from dots_lite.protocol import canonical, ProtocolError
from dots_lite.wire import validate_request
from lite_tests.gateway_fixtures import KEY, FakeGoogle, grant, request, response


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.provider = FakeGoogle(); self.grant = grant()
        self.grant['limits']['max_requests_per_route'] = 2
        self.gateway = MacGateway(Path(self.tmp.name)/'gateway', self.grant, KEY,
            self.provider, self.provider, create=True, poll_seconds=.01, wait_seconds=.2)
        self.addCleanup(self.gateway.close); self.gateway.initialize()
        self.identity = {'session-id': 'session-A', 'thread-id': 'thread-A'}
        self.server = None

    def submit(self, body=None, key=None, identity=None, **kwargs):
        return self.gateway.submit(identity or self.identity, canonical(body or request()), key, **kwargs)

    def inspect(self, ticket, **kwargs):
        if not kwargs: kwargs = {'identity': self.identity}
        return self.gateway.existing_request(ticket['request_id'], **kwargs)

    def http(self, method, suffix='', body=None, headers=None, path=None):
        if self.server is None:
            self.server = ResponsesServer(self.gateway)
            worker = threading.Thread(target=self.server.serve_forever, daemon=True); worker.start()
            self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            conn.request(method, path or '/activations/'+self.grant['activation_id']+'/v1/responses'+suffix,
                         body, headers or {})
            result = conn.getresponse()
            return result.status, dict(result.getheaders()), result.read()
        finally: conn.close()

    def test_keyless_duplicate_is_current_exact_bytes_only_and_does_not_refresh_begin(self):
        original = self.submit(begin_before=int(time.time()) + 30)
        before = copy.deepcopy(self.gateway.journal.read()); calls = list(self.provider.calls)
        self.assertEqual(self.submit(begin_before=int(time.time()) + 300), original)
        self.assertEqual(self.gateway.journal.read(), before)
        self.assertEqual(self.provider.calls, calls)
        altered = request(); altered['input'][0]['content'][0]['text'] = 'changed'
        with self.assertRaisesRegex(ProtocolError, 'route_request_inflight'): self.submit(altered)
        with self.assertRaisesRegex(ProtocolError, 'route_request_inflight'):
            self.gateway.submit(self.identity, json.dumps(request()).encode())
        self.assertEqual(self.provider.calls, calls)

    def test_keyless_does_not_collapse_explicit_keys_or_other_routes(self):
        original = self.submit()
        before = list(self.provider.calls)
        with self.assertRaisesRegex(ProtocolError, 'route_request_inflight'): self.submit(key='explicit-new')
        self.assertEqual(self.provider.calls, before)
        second_identity = {'session-id': 'session-B', 'thread-id': 'thread-B'}
        keyed = self.submit(key='explicit-original', identity=second_identity)
        with self.assertRaisesRegex(ProtocolError, 'route_request_inflight'): self.submit(identity=second_identity)
        self.assertNotEqual(original['route_id'], keyed['route_id'])
        self.assertEqual(len(self.gateway.journal.read()['requests']), 2)

    def test_keyless_recovery_allowed_after_stop_and_expiry_without_new_authority(self):
        original = self.submit(); self.gateway.request_stop()
        with patch('dots_lite.gateway.time.time', return_value=self.grant['expires_at'] + 1):
            before = self.gateway.journal.read()
            self.assertEqual(self.submit(), original)
            self.assertEqual(self.gateway.journal.read(), before)
            self.provider.publish_result(self.gateway, original, response(original['request_id']))
            inspected = self.inspect(original)
            self.assertTrue(inspected['result_available'])
            self.assertFalse(inspected['delivery_started'])
        self.assertEqual(len(self.gateway.journal.read()['requests']), 1)

    def test_unknown_upload_keyless_retry_and_get_never_retry_upload(self):
        with patch.object(self.provider, 'create_bytes', side_effect=TimeoutError('synthetic')) as upload:
            with self.assertRaisesRegex(ProtocolError, 'request_upload_unknown'): self.submit()
            ticket = self.submit()
            self.assertEqual(ticket['state'], 'upload_unknown')
            before = list(self.provider.calls)
            result = self.inspect(ticket)
            self.assertFalse(result['result_available'])
            self.assertEqual(self.provider.calls, before)
            self.assertEqual(upload.call_count, 1)
        job = self.gateway.journal.read()['requests'][ticket['request_id']]
        self.assertEqual(job['upload_attempts'], 1)

    def test_get_does_not_reconcile_or_publish_unknown_request(self):
        self.provider.lose_write_reply = True; self.provider.hide_write = True
        with self.assertRaisesRegex(ProtocolError, 'inbox_write_unknown'): self.submit()
        ticket = self.submit(); before = list(self.provider.calls)
        self.assertFalse(self.inspect(ticket)['result_available'])
        new_calls = self.provider.calls[len(before):]
        self.assertEqual([c[0] for c in new_calls], ['get_document'])
        self.assertNotEqual(new_calls[0][1], self.grant['inbox_id'])
        self.assertIsNotNone(self.gateway.journal.read()['write_intent'])

    def test_recovery_authorization_and_known_id_are_checked_before_provider_io(self):
        ticket = self.submit(); before = list(self.provider.calls)
        wrong = {'session-id': 'session-B', 'thread-id': 'thread-B'}
        cases = [({'identity': wrong}, 'recovery_route_identity_mismatch'),
                 ({'token': '0'*64}, 'invalid_recovery_token'),
                 ({'identity': self.identity, 'token': '0'*64}, 'recovery_identity_or_token_required')]
        for args, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(ProtocolError, code): self.inspect(ticket, **args)
        with self.assertRaisesRegex(ProtocolError, 'unknown_request_id'):
            self.gateway.existing_request('0'*32, identity=self.identity)
        self.assertEqual(self.provider.calls, before)
        token = recovery_token(KEY, self.grant['activation_id'], ticket['request_id'])
        self.assertFalse(self.inspect(ticket, token=token)['result_available'])
        self.assertNotEqual(token, KEY)
        self.assertNotEqual(token, recovery_token(KEY, 'other-activation', ticket['request_id']))
        self.assertNotEqual(token, recovery_token(KEY, self.grant['activation_id'], 'f'*32))

    def test_text_inspection_does_not_emit_or_change_delivery_barrier(self):
        ticket = self.submit(); expected = response(ticket['request_id'])
        self.provider.publish_result(self.gateway, ticket, expected)
        before = list(self.provider.calls)
        result = self.inspect(ticket)
        self.assertEqual(result['ticket']['request_id'], ticket['request_id'])
        self.assertEqual(result['result'], expected)
        self.assertTrue(result['read_only']); self.assertFalse(result['client_connection_resumed'])
        self.assertFalse(result['sse_emitted']); self.assertFalse(result['delivery_started'])
        self.assertNotIn('upload', [c[0] for c in self.provider.calls[len(before):]])
        retry = self.submit(); self.assertEqual(retry['request_id'], ticket['request_id'])
        self.assertIn(b'response.completed', self.gateway.delivery(retry))

    def test_tools_are_withheld_from_inspection_and_uncertain_emission_is_never_replayed(self):
        ticket = self.submit(); self.provider.publish_result(self.gateway, ticket, response(ticket['request_id'], 'function_call'))
        result = self.inspect(ticket)
        self.assertTrue(result['result_available']); self.assertTrue(result['tool_output_withheld'])
        self.assertIsNone(result['result']); self.assertFalse(result['delivery_started'])
        self.assertIn(b'function_call', self.gateway.delivery(ticket))
        before = list(self.provider.calls)
        self.assertIsNone(self.inspect(ticket)['result'])
        with self.assertRaisesRegex(ProtocolError, 'tool_delivery_unknown_do_not_replay'): self.submit()
        with self.assertRaisesRegex(ProtocolError, 'tool_delivery_unknown_do_not_replay'): self.gateway.delivery(ticket)
        self.assertEqual(self.provider.calls, before)
        self.assertEqual(len(self.gateway.journal.read()['requests']), 1)

    def test_future_sequence_is_distinct_history_is_not_merged_and_quota_stays_bounded(self):
        body = request(); first = self.submit(body)
        out = response(first['request_id']); self.provider.publish_result(self.gateway, first, out)
        self.gateway.poll(first); self.gateway.delivery(first)
        body['input'] += out['output'] + [{'role': 'user', 'content': 'Continue'}]
        second = self.submit(body)
        self.assertEqual(second['seq'], 2); self.assertNotEqual(first['request_id'], second['request_id'])
        before = list(self.provider.calls)
        self.assertEqual(self.submit(body), second)
        with self.assertRaisesRegex(ProtocolError, 'route_request_inflight'): self.submit(request())
        archived = self.inspect(first)
        self.assertEqual(archived['ticket']['state'], 'acknowledged'); self.assertFalse(archived['result_available'])
        self.assertIsNone(archived['result']); self.assertEqual(self.provider.calls, before)
        out = response(second['request_id']); self.provider.publish_result(self.gateway, second, out)
        self.gateway.poll(second); self.gateway.delivery(second)
        body['input'] += out['output'] + [{'role': 'user', 'content': 'Continue again'}]
        before = list(self.provider.calls)
        with self.assertRaisesRegex(ProtocolError, 'route_request_quota_exhausted'): self.submit(body)
        self.assertEqual(self.provider.calls, before)
        self.assertEqual(len(self.gateway.journal.read()['requests']), 2)

    def test_keyless_http_timeout_inspection_then_original_post_delivers_same_late_result(self):
        headers = {'Content-Type': 'application/json', **self.identity}
        code, _, raw = self.http('POST', body=canonical(request()), headers=headers)
        timed_out = json.loads(raw)
        self.assertEqual(code, 409)
        self.assertEqual(timed_out['error']['code'], 'response_wait_expired_same_request_recoverable')
        ticket = self.submit(); self.assertEqual(ticket['request_id'], timed_out['request_id'])
        token = recovery_token(KEY, self.grant['activation_id'], ticket['request_id'])
        inspect_headers = {'X-Dots-Recovery-Token': token}
        code, _, raw = self.http('GET', '/'+ticket['request_id'], headers=inspect_headers)
        self.assertEqual(code, 202); self.assertFalse(json.loads(raw)['result_available'])
        self.provider.publish_result(self.gateway, ticket, response(ticket['request_id']))
        writes_before = [call for call in self.provider.calls if call[0] in {'upload', 'create_document', 'batch_update'}]
        code, got_headers, raw = self.http('GET', '/'+ticket['request_id'], headers=inspect_headers)
        self.assertEqual(code, 200); self.assertEqual(got_headers['Cache-Control'], 'no-store')
        self.assertFalse(json.loads(raw)['delivery_started'])
        code, got_headers, raw = self.http('POST', body=canonical(request()), headers=headers)
        self.assertEqual(code, 200); self.assertEqual(got_headers['X-Request-ID'], ticket['request_id'])
        self.assertIn(b'response.completed', raw)
        self.assertEqual([call for call in self.provider.calls if call[0] in {'upload', 'create_document', 'batch_update'}], writes_before)
        self.assertEqual(len(self.gateway.journal.read()['requests']), 1)

    def test_http_recovery_rejects_wrong_activation_missing_identity_browser_and_wrong_token(self):
        ticket = self.submit(); token = recovery_token(KEY, self.grant['activation_id'], ticket['request_id'])
        before = list(self.provider.calls)
        for headers, path in [({}, None), ({'X-Dots-Recovery-Token': '0'*64}, None),
            ({'X-Dots-Recovery-Token': token, **self.identity}, None),
            ({'X-Dots-Recovery-Token': token, 'Origin': 'https://example.invalid'}, None),
            ({'X-Dots-Recovery-Token': token}, '/activations/wrong/v1/responses/'+ticket['request_id']),
            ({'X-Dots-Recovery-Token': token}, '/activations/'+self.grant['activation_id']+'/v1/responses/')]:
            with self.subTest(headers=list(headers), path=path):
                status, _, _ = self.http('GET', '/'+ticket['request_id'], headers=headers, path=path)
                self.assertEqual(status, 400)
        self.assertEqual(self.provider.calls, before)

    def test_existing_result_http_waiter_capacity_and_expired_deadline_are_bounded(self):
        ticket = self.submit()
        # Start the local server without admitting another request.
        self.http('GET', path='/health')
        capacity = self.grant['limits']['max_routes'] * 2
        for _ in range(capacity): self.assertTrue(self.server.waiters.acquire(blocking=False))
        before = list(self.provider.calls)
        try:
            status, _, raw = self.http('GET', '/'+ticket['request_id'], headers=self.identity)
            self.assertEqual(status, 400)
            self.assertEqual(json.loads(raw)['error']['code'], 'http_waiter_capacity_exhausted')
        finally:
            for _ in range(capacity): self.server.waiters.release()
        with self.assertRaisesRegex(ProtocolError, 'response_wait_expired'):
            self.gateway.existing_request(ticket['request_id'], identity=self.identity, deadline=time.monotonic()-1)
        self.assertEqual(self.provider.calls, before)

    def test_tool_delivery_fence_survives_restart_for_keyless_retry_and_inspection(self):
        ticket = self.submit(); self.provider.publish_result(self.gateway, ticket, response(ticket['request_id'], 'custom_tool_call'))
        self.gateway.poll(ticket); self.gateway.delivery(ticket); self.gateway.close()
        restored = MacGateway(Path(self.tmp.name)/'gateway', self.grant, KEY, self.provider, self.provider)
        self.addCleanup(restored.close)
        result = restored.existing_request(ticket['request_id'], identity=self.identity)
        self.assertIsNone(result['result']); self.assertTrue(result['tool_output_withheld'])
        self.assertTrue(result['delivery_started'])
        with self.assertRaisesRegex(ProtocolError, 'tool_delivery_unknown_do_not_replay'):
            restored.submit(self.identity, canonical(request()))
        self.assertEqual(len(restored.journal.read()['requests']), 1)

    def test_http_type_first_tool_diagnostics_are_bounded_and_do_not_log_private_data(self):
        for kind, error in [('web_search', 'unsupported_hosted_tool'), ('tool_search', 'unsupported_client_tool_search')]:
            body = request(); body['tools'].append({'type': kind, 'secret': 'PRIVATE-SCHEMA-CONTENT'})
            before = list(self.provider.calls)
            status, _, raw = self.http('POST', body=canonical(body), headers={'Content-Type':'application/json', **self.identity})
            data = json.loads(raw)
            self.assertEqual(status, 409); self.assertEqual(data['error']['code'], error)
            self.assertEqual(data['error']['details'], {'tool_index':1, 'tool_type':kind, 'name_present':False})
            self.assertNotIn(b'PRIVATE-SCHEMA-CONTENT', raw)
            self.assertEqual(self.provider.calls, before)
        status = self.gateway.status()['http_ingress']
        self.assertEqual(status['responses_posts'], 2); self.assertEqual(status['responses_errors'], 2)
        self.assertEqual(status['last_error']['code'], 'unsupported_client_tool_search')
        self.assertNotIn('PRIVATE-SCHEMA-CONTENT', json.dumps(status))
        self.assertEqual(self.gateway.journal.read()['requests'], {})

    def test_unknown_tool_type_and_pair_names_are_not_echoed(self):
        body = request(); body['tools'] = [{'type': 'PRIVATE-TYPE', 'name': 'PRIVATE-NAME'}]
        with self.assertRaises(ProtocolError) as caught: validate_request(body)
        self.assertEqual(caught.exception.details['tool_type'], 'unknown')
        body = request(model='PRIVATE-MODEL')
        status, _, raw = self.http('POST', body=canonical(body), headers={'Content-Type':'application/json', **self.identity})
        self.assertEqual(status, 409); self.assertNotIn(b'PRIVATE-MODEL', raw)
        self.assertEqual(json.loads(raw)['error']['details']['requested_pair']['model'], 'other')


if __name__ == '__main__': unittest.main()
