"""Offline Docs read retry boundaries; no credentials or live Google calls."""
import errno
import http.client
import json
from pathlib import Path
import socket
import ssl
import sys
import types
import unittest
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from examples import google_clients as clients
from remote_transport.model import ProtocolError


class HttpFailure(Exception):
    def __init__(self, status):
        super().__init__('private-provider-body token=secret document-id https://private.invalid')
        self.resp = types.SimpleNamespace(status=status)


class FakeService:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def documents(self):
        return self

    def get(self, **kwargs):
        self.calls.append(('get', kwargs))
        return self

    def batchUpdate(self, **kwargs):
        self.calls.append(('write', kwargs))
        return self

    def execute(self, **kwargs):
        self.calls.append(('execute', kwargs))
        value = next(self.outcomes)
        if isinstance(value, BaseException):
            raise value
        return value


def expected_diagnostics(category, attempts, exception_class, http_status=None, *, truncated=False):
    value = {'category': category, 'attempts': attempts,
             'exception_class': exception_class, 'exception_chain': [exception_class]}
    if http_status is not None:
        value['http_status'] = http_status
    if truncated:
        value['exception_chain_truncated'] = True
    return value


class ReadRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch.object(clients.time, 'sleep').start()
        self.addCleanup(patch.stopall)

    def read_failure(self, outcomes):
        service = FakeService(outcomes)
        with self.assertRaises(clients.DocsReadError) as caught:
            clients.DocsSDKClient(service).get_document('synthetic-doc')
        return service, caught.exception

    def assert_sanitized(self, error):
        self.assertIsInstance(error, ProtocolError)
        diagnostic = json.dumps(error.diagnostics)
        self.assertNotIn('secret', diagnostic)
        self.assertNotIn('private', diagnostic)
        self.assertNotIn('https:', diagnostic)
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)

    def test_timeout_then_success_retries_identical_read_only(self):
        result = {'documentId': 'synthetic-doc', 'revisionId': 'fresh'}
        service = FakeService([TimeoutError('private secret'), result])
        self.assertEqual(clients.DocsSDKClient(service).get_document('synthetic-doc'), result)
        get_calls = [value for kind, value in service.calls if kind == 'get']
        self.assertEqual(get_calls, [dict(documentId='synthetic-doc', includeTabsContent=True,
                                         suggestionsViewMode='SUGGESTIONS_INLINE')] * 2)
        self.assertEqual([value for kind, value in service.calls if kind == 'execute'],
                         [{'num_retries': 0}] * 2)
        self.assertNotIn('write', [kind for kind, _ in service.calls])
        self.sleep.assert_called_once_with(1.0)

    def test_connection_reset_then_success(self):
        service = FakeService([ConnectionResetError('secret'), {'ok': True}])
        self.assertEqual(clients.DocsSDKClient(service).get_document('doc'), {'ok': True})
        self.sleep.assert_called_once_with(1.0)

    def test_http_transient_statuses_retry(self):
        for status in (408, 429, 500, 502, 503, 504, 599):
            with self.subTest(status=status):
                self.sleep.reset_mock()
                service = FakeService([HttpFailure(status), {'ok': True}])
                self.assertEqual(clients.DocsSDKClient(service).get_document('doc'), {'ok': True})
                self.sleep.assert_called_once_with(1.0)

    def test_transient_exhaustion_is_typed_bounded_and_sanitized(self):
        service, error = self.read_failure([HttpFailure(503)] * 3 + [{'must_not_be_read': True}])
        self.assertTrue(error.retryable)
        self.assertEqual(str(error), 'docs_read_transient_exhausted')
        self.assertEqual(error.diagnostics,
                         expected_diagnostics('http_transient', 3, 'unrecognized', 503))
        self.assertEqual(self.sleep.call_args_list, [call(1.0), call(2.0)])
        self.assertEqual(len(service.calls), 6)
        self.assert_sanitized(error)

    def test_http_permanent_statuses_never_retry(self):
        for status in (301, 400, 401, 403, 404, 409, 410, 422):
            with self.subTest(status=status):
                service, error = self.read_failure([HttpFailure(status), {'must_not_be_read': True}])
                self.assertFalse(error.retryable)
                self.assertEqual(str(error), 'docs_read_failed')
                self.assertEqual(error.diagnostics['http_status'], status)
                self.assertEqual(error.diagnostics['attempts'], 1)
                self.assertEqual(len(service.calls), 2)
                self.sleep.assert_not_called()
                self.assert_sanitized(error)

    def test_transient_then_auth_stops_immediately(self):
        service, error = self.read_failure([HttpFailure(503), HttpFailure(401), {'must_not_be_read': True}])
        self.assertFalse(error.retryable)
        self.assertEqual(error.diagnostics, expected_diagnostics('authorization', 2, 'unrecognized', 401))
        self.assertEqual(len(service.calls), 4)
        self.sleep.assert_called_once_with(1.0)

    def test_exception_message_is_not_parsed_for_retry(self):
        service, error = self.read_failure([ValueError('503 429 timeout private secret'), {'ok': True}])
        self.assertEqual(error.diagnostics, expected_diagnostics('unexpected', 1, 'builtins.ValueError'))
        self.assertFalse(error.retryable)
        self.assertEqual(len(service.calls), 2)
        self.sleep.assert_not_called()
        self.assert_sanitized(error)

    def test_invalid_status_not_retained_or_retried(self):
        for status in (True, '503', 'secret', 999, 0):
            with self.subTest(status=status):
                service, error = self.read_failure([HttpFailure(status)])
                self.assertEqual(error.diagnostics, expected_diagnostics('unexpected', 1, 'unrecognized'))
                self.assertFalse(error.retryable)
                self.assertEqual(len(service.calls), 2)
                self.sleep.assert_not_called()

    def test_validation_error_cannot_be_retried(self):
        service, error = self.read_failure([ProtocolError('tampered_private_state')])
        self.assertEqual(error.diagnostics, expected_diagnostics('protocol', 1, 'remote_transport.model.ProtocolError'))
        self.assertFalse(error.retryable)
        self.sleep.assert_not_called()
        self.assertEqual(len(service.calls), 2)

    def test_tls_certificate_errors_are_permanent(self):
        for failure in (ssl.SSLCertVerificationError('secret'), ssl.SSLError('secret')):
            with self.subTest(failure=type(failure).__name__):
                service, error = self.read_failure([failure])
                self.assertEqual(error.diagnostics, expected_diagnostics('tls', 1, 'ssl.' + type(failure).__name__))
                self.assertFalse(error.retryable)
                self.sleep.assert_not_called()
                self.assertEqual(len(service.calls), 2)

    def test_wrapped_tls_and_validation_override_network(self):
        for inner in (ssl.SSLCertVerificationError('secret'), ProtocolError('tampered')):
            with self.subTest(inner=type(inner).__name__):
                outer = ConnectionError('private outer')
                outer.__cause__ = inner
                _, error = self.read_failure([outer])
                self.assertFalse(error.retryable)
                self.sleep.assert_not_called()

    def test_auth_error_inside_transient_http_wrapper_is_permanent(self):
        outer = HttpFailure(503)
        outer.__cause__ = HttpFailure(403)
        _, error = self.read_failure([outer])
        self.assertFalse(error.retryable)
        self.assertEqual(error.diagnostics['http_status'], 403)
        self.sleep.assert_not_called()

    def test_unexamined_deep_exception_chain_fails_closed(self):
        outer = ssl.SSLCertVerificationError('private certificate details')
        for _ in range(10):
            wrapper = ConnectionError('private transport wrapper')
            wrapper.__cause__ = outer
            outer = wrapper
        service, error = self.read_failure([outer])
        self.assertFalse(error.retryable)
        self.assertEqual(error.diagnostics, expected_diagnostics('unexpected', 1, 'builtins.ConnectionError', truncated=True))
        self.assertEqual(len(service.calls), 2)
        self.sleep.assert_not_called()

    def test_cyclic_transport_cause_terminates_and_remains_bounded(self):
        failure = ConnectionError('private transport wrapper')
        failure.__cause__ = failure
        service, error = self.read_failure([failure] * 3)
        self.assertTrue(error.retryable)
        self.assertEqual(len(service.calls), 6)
        self.assertEqual(self.sleep.call_args_list, [call(1.0), call(2.0)])

    def test_network_oserror_retries_but_filesystem_error_does_not(self):
        for failure in (OSError(errno.ENETUNREACH, 'secret'),
                        socket.gaierror(socket.EAI_AGAIN, 'secret'),
                        http.client.IncompleteRead(b'private', 30)):
            with self.subTest(failure=type(failure).__name__):
                self.sleep.reset_mock()
                service = FakeService([failure, {'ok': True}])
                self.assertEqual(clients.DocsSDKClient(service).get_document('doc'), {'ok': True})
                self.sleep.assert_called_once_with(1.0)
        self.sleep.reset_mock()
        _, error = self.read_failure([PermissionError(errno.EACCES, 'private')])
        self.assertFalse(error.retryable)
        self.sleep.assert_not_called()

    def test_optional_refresh_error_never_retries(self):
        class RefreshError(Exception):
            retryable = True
        module = types.SimpleNamespace(RefreshError=RefreshError)
        with patch.dict(sys.modules, {'google.auth.exceptions': module}):
            _, error = self.read_failure([RefreshError('private auth details')])
        self.assertEqual(error.diagnostics, expected_diagnostics('authorization', 1, 'google.auth.exceptions.RefreshError'))
        self.assertFalse(error.retryable)
        self.sleep.assert_not_called()

    def test_optional_transport_wrapper_retries_without_parsing_message(self):
        class TransportError(Exception):
            pass
        module = types.SimpleNamespace(TransportError=TransportError)
        with patch.dict(sys.modules, {'google.auth.exceptions': module}):
            service = FakeService([TransportError('private'), {'ok': True}])
            self.assertEqual(clients.DocsSDKClient(service).get_document('doc'), {'ok': True})
        self.sleep.assert_called_once_with(1.0)

    def test_keyboard_interrupt_is_not_swallowed(self):
        service = FakeService([KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            clients.DocsSDKClient(service).get_document('doc')
        self.sleep.assert_not_called()
        self.assertEqual(len(service.calls), 2)

    def test_write_timeout_or_http_failure_never_gets_application_retry(self):
        for failure in (TimeoutError('secret'), HttpFailure(429), HttpFailure(503)):
            with self.subTest(failure=type(failure).__name__):
                service = FakeService([failure, {'must_not_be_read': True}])
                with self.assertRaisesRegex(ProtocolError, '^docs_write_outcome_unknown$'):
                    clients.DocsSDKClient(service).batch_update_document('doc', [], {'requiredRevisionId': 'r1'})
                self.assertEqual(len(service.calls), 2)
                self.assertEqual(service.calls[0], ('write', {
                    'documentId': 'doc', 'body': {'requests': [], 'writeControl': {'requiredRevisionId': 'r1'}}}))
                self.assertEqual(service.calls[1], ('execute', {'num_retries': 0}))
                self.sleep.assert_not_called()


class ReadBudgetTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        patch.object(clients.time, 'monotonic', side_effect=lambda: self.now).start()
        self.sleep = patch.object(clients.time, 'sleep', side_effect=self.advance).start()
        self.addCleanup(patch.stopall)

    def advance(self, seconds):
        self.now += seconds

    def test_expired_deadline_does_not_start_read(self):
        service = FakeService([{'ok': True}])
        with self.assertRaisesRegex(ProtocolError, '^docs_read_deadline_exceeded$'):
            clients.DocsSDKClient(service).get_document('doc', deadline=self.now)
        self.assertEqual(service.calls, [])
        self.sleep.assert_not_called()

    def test_invalid_budgets_rejected_without_read(self):
        service = FakeService([{'ok': True}])
        client = clients.DocsSDKClient(service)
        for deadline in (float('inf'), float('nan'), True, '100'):
            with self.assertRaisesRegex(ProtocolError, '^invalid_docs_read_deadline$'):
                client.get_document('doc', deadline=deadline)
        with self.assertRaisesRegex(ProtocolError, '^invalid_docs_read_check$'):
            client.get_document('doc', check=True)
        self.assertEqual(service.calls, [])

    def test_late_success_is_rejected(self):
        service = FakeService([{'must_not_be_returned': True}])
        execute = service.execute
        def late(**kwargs):
            self.advance(2)
            return execute(**kwargs)
        with patch.object(service, 'execute', side_effect=late):
            with self.assertRaisesRegex(ProtocolError, '^docs_read_deadline_exceeded$'):
                clients.DocsSDKClient(service).get_document('doc', deadline=101)
        self.assertEqual(len(service.calls), 2)

    def test_late_transient_failure_is_not_retried(self):
        service = FakeService([TimeoutError('secret'), {'ok': True}])
        execute = service.execute
        def late(**kwargs):
            self.advance(2)
            return execute(**kwargs)
        with patch.object(service, 'execute', side_effect=late):
            with self.assertRaisesRegex(ProtocolError, '^docs_read_deadline_exceeded$'):
                clients.DocsSDKClient(service).get_document('doc', deadline=101)
        self.assertEqual(len(service.calls), 2)
        self.sleep.assert_not_called()

    def test_backoff_is_clamped_and_no_retry_starts_after_deadline(self):
        service = FakeService([TimeoutError('secret'), {'ok': True}])
        with self.assertRaisesRegex(ProtocolError, '^docs_read_deadline_exceeded$'):
            clients.DocsSDKClient(service).get_document('doc', deadline=100.25)
        self.sleep.assert_called_once_with(0.25)
        self.assertEqual(len(service.calls), 2)

    def test_check_runs_before_and_after_success(self):
        service = FakeService([{'ok': True}])
        checks = []
        self.assertEqual(clients.DocsSDKClient(service).get_document(
            'doc', deadline=110, check=lambda: checks.append(self.now)), {'ok': True})
        self.assertEqual(checks, [100, 100, 100])

    def test_stop_after_failure_is_preserved_and_prevents_retry(self):
        service = FakeService([TimeoutError('secret'), {'ok': True}])
        calls = []
        def check():
            calls.append(True)
            if len(calls) == 3:
                raise ProtocolError('global_session_stopped_new_activation_required')
        with self.assertRaisesRegex(ProtocolError, '^global_session_stopped_new_activation_required$'):
            clients.DocsSDKClient(service).get_document('doc', deadline=110, check=check)
        self.assertEqual(len(service.calls), 2)
        self.sleep.assert_not_called()

    def test_stop_during_backoff_is_checked_before_retry(self):
        service = FakeService([TimeoutError('secret'), {'ok': True}])
        def check():
            if self.now > 100:
                raise ProtocolError('global_session_stopped_new_activation_required')
        with self.assertRaisesRegex(ProtocolError, '^global_session_stopped_new_activation_required$'):
            clients.DocsSDKClient(service).get_document('doc', deadline=110, check=check)
        self.assertEqual(len(service.calls), 2)
        self.sleep.assert_called_once_with(1.0)


class SDKReadRecoveryTests(unittest.TestCase):
    def setUp(self):
        try:
            import httplib2
            from googleapiclient.discovery import build
        except ImportError:
            self.skipTest('optional Google SDK dependencies are not installed')
        self.httplib2 = httplib2
        self.build = build
        self.sleep = patch.object(clients.time, 'sleep').start()
        patch('socket.socket', side_effect=AssertionError('offline test forbids network')).start()
        self.addCleanup(patch.stopall)

    def client(self, statuses):
        httplib2 = self.httplib2
        class HTTP:
            calls = []
            outcomes = iter(statuses)
            def request(self, uri, method='GET', body=None, headers=None, **kwargs):
                self.calls.append((uri, method, body))
                status = next(self.outcomes)
                return httplib2.Response({'status': str(status), 'content-type': 'application/json'}), (
                    b'{"documentId":"synthetic-doc","revisionId":"fresh"}' if status == 200 else
                    b'{"error":{"message":"private-provider-body secret"}}')
        http = HTTP()
        service = self.build('docs', 'v1', http=http, cache_discovery=False,
                             static_discovery=True, num_retries=0)
        return clients.DocsSDKClient(service), http

    def test_real_http_error_rate_limit_then_server_error_recovers(self):
        client, http = self.client([429, 503, 200])
        self.assertEqual(client.get_document('synthetic-doc')['revisionId'], 'fresh')
        self.assertEqual(len(http.calls), 3)
        self.assertTrue(all(method == 'GET' and body is None for _, method, body in http.calls))
        self.assertEqual(self.sleep.call_args_list, [call(1.0), call(2.0)])

    def test_real_http_error_auth_stays_terminal_and_sanitized(self):
        client, http = self.client([403, 200])
        with self.assertRaises(clients.DocsReadError) as caught:
            client.get_document('synthetic-doc')
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.diagnostics,
                         expected_diagnostics('authorization', 1, 'googleapiclient.errors.HttpError', 403))
        self.assertEqual(len(http.calls), 1)
        self.sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
