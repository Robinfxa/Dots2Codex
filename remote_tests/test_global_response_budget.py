"""Deterministic, offline response-budget regressions.

Clocks, sockets and providers are synthetic. These tests exercise the real
response handlers and durable CAS journal without credentials, Google traffic,
a native task, an installed client, or a wall-clock 15-minute wait.
"""
from contextlib import ExitStack
from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from remote_transport import global_desktop as desktop, global_gateway as gateway
from remote_transport.global_fixture import identity, request, events
from remote_transport.model import ProtocolError, canonical


class FakeClock:
    def __init__(self):
        self.wall = 2_000_000_000.0
        self.mono = 1000.0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds

    def patch(self):
        stack = ExitStack()
        stack.enter_context(patch('time.time', self.time))
        stack.enter_context(patch('time.monotonic', self.monotonic))
        return stack


def frame(kind='response.completed', *, code=None):
    status = 'completed' if kind == 'response.completed' else 'in_progress'
    response = {'id': 'resp_fixture', 'status': status}
    if code is not None:
        response['error'] = {'code': code}
    return b'event: ' + kind.encode() + b'\ndata: ' + canonical(
        {'type': kind, 'response': response}) + b'\n\n'


class FakeSocket:
    def __init__(self, clock):
        self.clock = clock
        self.timeouts = []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append((self.clock.monotonic(), value))

    def shutdown(self, *_):
        self.closed = True

    def close(self):
        self.closed = True

    def recv(self, *_):
        return b'x'


class FakeResponse:
    status = 200

    def __init__(self, clock, chunks=(), *, heartbeat=False):
        self.clock = clock
        self.chunks = list(chunks)
        self.heartbeat = heartbeat
        self.reads = 0
        self.closed = False
        self.headers = {'Content-Type': 'text/event-stream', 'Connection': 'close'}

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def isclosed(self):
        return self.closed

    def read1(self, _):
        self.reads += 1
        if self.heartbeat:
            if self.reads > 12:
                raise AssertionError('Heartbeat reset the absolute response budget')
            self.clock.advance(100)
            return frame('response.in_progress')
        if not self.chunks:
            self.closed = True
            return b''
        delay, block = self.chunks.pop(0)
        self.clock.advance(delay)
        if not block:
            self.closed = True
        return block

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, clock, response, *, connect=0, send=0, headers=0):
        self.clock, self.response = clock, response
        self.delays = {'connect': connect, 'send': send, 'headers': headers}
        self.transport = FakeSocket(clock)
        self.sock = self.transport
        self.calls = []
        self.timeout = None
        self.closed = False

    def construct(self, *args, **kwargs):
        self.timeout = kwargs.get('timeout')
        self.calls.append(('construct', self.clock.monotonic(), self.timeout))
        return self

    def connect(self):
        self.calls.append(('connect', self.clock.monotonic(), self.timeout))
        self.clock.advance(self.delays['connect'])

    def request(self, method, path, body=None, headers=None):
        self.calls.append(('request', self.clock.monotonic(), method, path, headers))
        self.clock.advance(self.delays['send'])

    def getresponse(self):
        self.calls.append(('headers', self.clock.monotonic()))
        self.clock.advance(self.delays['headers'])
        # http.client detaches the socket for a plain Connection: close response.
        self.sock = None
        return self.response

    def close(self):
        self.closed = True
        self.transport.close()


class ResponseDeadlineTests(unittest.TestCase):
    def test_default_and_strict_900_second_boundary(self):
        from remote_transport.global_response import GLOBAL_RESPONSE_WAIT_SECONDS, ResponseDeadline
        self.assertEqual(GLOBAL_RESPONSE_WAIT_SECONDS, 900)
        clock = FakeClock()
        with clock.patch():
            budget = ResponseDeadline()
            self.assertEqual(budget.expires, clock.wall + 900)
            for elapsed in (240, 700, 899.999):
                clock.advance(elapsed - (clock.mono - 1000))
                self.assertGreater(budget.remaining(), 0)
            clock.advance(.001)
            with self.assertRaisesRegex(ProtocolError, 'budget_expired'):
                budget.remaining()

    def test_every_earlier_cap_wins_and_tightening_never_refreshes(self):
        from remote_transport.global_response import ResponseDeadline
        for index in range(6):
            with self.subTest(cap=index):
                clock = FakeClock()
                with clock.patch():
                    caps = [clock.wall + 1800] * 6
                    caps[index] = clock.wall + 300
                    budget = ResponseDeadline(*caps)
                    until, expires = budget.until, budget.expires
                    clock.advance(240)
                    budget.tighten(clock.wall + 900, clock.wall + 1800)
                    self.assertEqual((budget.until, budget.expires), (until, expires))
                    self.assertAlmostEqual(budget.remaining(), 60)
                    clock.advance(60)
                    with self.assertRaises(ProtocolError):
                        budget.remaining()

    def test_existing_plan_age_counts_toward_total(self):
        from remote_transport.global_response import ResponseDeadline
        clock = FakeClock()
        with clock.patch():
            budget = ResponseDeadline(issued_at=clock.wall - 700)
            self.assertAlmostEqual(budget.remaining(), 200)
            clock.advance(200)
            with self.assertRaises(ProtocolError):
                budget.remaining()

    def test_wall_clock_rollback_cannot_extend_monotonic_deadline(self):
        from remote_transport.global_response import ResponseDeadline
        clock = FakeClock()
        with clock.patch():
            budget = ResponseDeadline()
            clock.advance(899.999)
            clock.wall -= 600
            # Early fail-closed rollback detection is also valid.
            try:
                self.assertLessEqual(budget.remaining(), .0011)
            except ProtocolError:
                pass
            clock.advance(.001)
            with self.assertRaises(ProtocolError):
                budget.remaining()

    def test_socket_guard_closes_an_inflight_trickling_attempt_at_fixed_deadline(self):
        from remote_transport.global_response import ResponseDeadline
        clock = FakeClock()
        transport = FakeSocket(clock)
        shut_down = threading.Event()
        def shutdown(*args):
            transport.closed = True
            shut_down.set()
        transport.shutdown = shutdown
        with clock.patch():
            budget = ResponseDeadline()
            with budget.watch_socket(transport):
                clock.advance(900)
                self.assertTrue(shut_down.wait(3), 'In-flight transport was not fenced at its deadline')
        self.assertTrue(transport.closed)

    def test_duplicate_nonfinite_and_malformed_headers_fail_closed(self):
        from remote_transport.global_response import parse_response_deadline, RESPONSE_DEADLINE_HEADER
        for values in (['nan'], ['inf'], ['-1'], ['0'], ['true'], ['123', '124']):
            with self.subTest(values=values):
                headers = Message()
                for value in values:
                    headers.add_header(RESPONSE_DEADLINE_HEADER, value)
                with self.assertRaises(ProtocolError):
                    parse_response_deadline(headers)
        self.assertIsNone(parse_response_deadline(Message()))


class PreflightResponseBudgetTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.generation = '1' * 32
        self.plan = {'created': self.clock.wall, 'expires': self.clock.wall + 900,
                     'route_id': '2' * 64, 'generation': self.generation,
                     'body': request('offline preflight'), 'identity': identity()}
        self.caps = [self.clock.wall + 1800] * 6
        self.store = SimpleNamespace(config=lambda: {'port': 54321},
            activation=lambda: {'id': self.generation},
            response_deadline_caps=lambda rid: self.caps)

    def invoke(self, response=None, **delays):
        response = response or FakeResponse(self.clock, [(0, frame())])
        connection = FakeConnection(self.clock, response, **delays)
        with self.clock.patch(), patch.object(desktop.http.client, 'HTTPConnection', connection.construct):
            outcome = desktop.post_preflight(self.store, self.plan)
        return outcome, connection

    def assert_expiry(self, response=None, **delays):
        response = response or FakeResponse(self.clock, [(0, frame())])
        connection = FakeConnection(self.clock, response, **delays)
        with self.clock.patch(), patch.object(desktop.http.client, 'HTTPConnection', connection.construct):
            with self.assertRaisesRegex(ProtocolError, 'deadline|budget_expired'):
                desktop.post_preflight(self.store, self.plan)
        self.assertLessEqual(sum(row[0] == 'request' for row in connection.calls), 1)
        self.assertTrue(connection.closed)
        return connection

    def test_240_700_and_899_999_second_success_without_content_length(self):
        for elapsed in (240, 700, 899.999):
            with self.subTest(elapsed=elapsed):
                self.setUp()
                result, connection = self.invoke(FakeResponse(self.clock, [(elapsed, frame())]))
                self.assertEqual(result, {'http_status': 200})
                requests = [row for row in connection.calls if row[0] == 'request']
                self.assertEqual(len(requests), 1)
                self.assertTrue(connection.closed)
                self.assertTrue(connection.transport.timeouts)
                from remote_transport.global_response import RESPONSE_DEADLINE_HEADER
                self.assertEqual(float(requests[0][4][RESPONSE_DEADLINE_HEADER]), self.plan['expires'])

    def test_strict_900_body_return_is_rejected_even_when_completed(self):
        connection = self.assert_expiry(FakeResponse(self.clock, [(900, frame())]))
        self.assertEqual(sum(row[0] == 'request' for row in connection.calls), 1)

    def test_connect_send_headers_and_body_share_one_unreset_budget(self):
        for stage in ('connect', 'send', 'headers'):
            with self.subTest(stage=stage):
                self.setUp()
                connection = self.assert_expiry(**{stage: 900})
                if stage == 'connect':
                    self.assertFalse(any(row[0] == 'request' for row in connection.calls))
        self.setUp()
        self.assert_expiry(FakeResponse(self.clock, [(201, frame())]), connect=240, send=240, headers=219)

    def test_every_phase_clamps_timeout_to_remaining_total(self):
        result, connection = self.invoke(FakeResponse(self.clock, [(1, frame())]), connect=240, send=240, headers=219)
        self.assertEqual(result['http_status'], 200)
        for at, timeout in connection.transport.timeouts:
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 1900 - at + 1e-6)
        self.assertLessEqual(connection.calls[0][2], 900)

    def test_heartbeat_stream_cannot_refresh_total(self):
        response = FakeResponse(self.clock, heartbeat=True)
        self.assert_expiry(response)
        self.assertEqual(response.reads, 9)
        self.assertEqual(self.clock.mono, 1900)

    def test_earlier_authority_and_plan_deadlines_remain_independent(self):
        for index in range(7):
            with self.subTest(cap=index):
                self.setUp()
                if index == 6:
                    self.plan['expires'] = self.clock.wall + 300
                else:
                    self.caps[index] = self.clock.wall + 300
                self.assert_expiry(FakeResponse(self.clock, [(300, frame())]))

    def test_persisted_plan_keeps_original_monotonic_deadline_after_wall_slew(self):
        from remote_transport.global_response import ResponseDeadline
        with self.clock.patch():
            self.plan['response_deadline'] = ResponseDeadline().checkpoint()
        self.clock.mono += 800
        self.clock.wall += 100
        self.assert_expiry(FakeResponse(self.clock, [(100, frame())]))

    def test_plan_age_not_refreshed_when_post_begins(self):
        self.clock.advance(700)
        self.assert_expiry(FakeResponse(self.clock, [(200, frame())]))

    def test_truncated_or_mismatched_sse_frame_cannot_certify_success(self):
        for raw in (frame().rstrip(b'\n'), frame().replace(b'event: response.completed', b'event: response.failed'),
                    frame() + b'partial unfinished frame', b'garbage\n\n' + frame(),
                    frame().replace(b'\n\n', b'\ndata: {}\n\n')):
            with self.subTest(raw=raw):
                self.setUp()
                connection = FakeConnection(self.clock, FakeResponse(self.clock, [(0, raw)]))
                with self.clock.patch(), patch.object(desktop.http.client, 'HTTPConnection', connection.construct):
                    with self.assertRaisesRegex(ProtocolError, 'invalid_response_stream|request_incomplete'):
                        desktop.post_preflight(self.store, self.plan)
                self.assertEqual(sum(row[0] == 'request' for row in connection.calls), 1)

    def test_transport_failures_have_truthful_sanitized_phase_diagnostics(self):
        import http.client
        failures = [(TimeoutError('token=private'), 'transport_timeout'),
                    (http.client.RemoteDisconnected('private URL'), 'request_incomplete'),
                    (http.client.BadStatusLine('private payload'), 'invalid_http_response'),
                    (OSError('secret provider response'), 'transport_failed')]
        for failure, code in failures:
            with self.subTest(code=code):
                self.setUp()
                connection = FakeConnection(self.clock, FakeResponse(self.clock))
                with self.clock.patch(), patch.object(desktop.http.client, 'HTTPConnection', connection.construct), \
                        patch.object(connection, 'getresponse', side_effect=failure):
                    with self.assertRaisesRegex(ProtocolError, code) as caught:
                        desktop.post_preflight(self.store, self.plan)
                diagnostic = caught.exception.preflight_diagnostics
                self.assertEqual(diagnostic['response_phase'], 'headers')
                self.assertEqual(diagnostic['response_code'], 'global_preflight_' + code)
                self.assertEqual(diagnostic['response_budget_seconds'], 900)
                self.assertNotIn('private', json.dumps(diagnostic))
                self.assertNotIn('secret', json.dumps(diagnostic))
                self.assertEqual(sum(row[0] == 'request' for row in connection.calls), 1)

    def test_malformed_eof_and_explicit_failure_are_never_success(self):
        cases = [(b'event: response.completed\ndata: not-json\n\n', 'invalid_response_stream'),
                 (frame('response.in_progress'), 'request_incomplete'),
                 (frame('response.failed', code='native_child_expired'), 'request_failed')]
        for raw, code in cases:
            with self.subTest(code=code):
                self.setUp()
                connection = FakeConnection(self.clock, FakeResponse(self.clock, [(0, raw)]))
                with self.clock.patch(), patch.object(desktop.http.client, 'HTTPConnection', connection.construct):
                    with self.assertRaisesRegex(ProtocolError, code):
                        desktop.post_preflight(self.store, self.plan)
                self.assertEqual(sum(row[0] == 'request' for row in connection.calls), 1)


class GatewayResponseBudgetTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.generation = '1' * 32
        self.rid = '2' * 64
        self.route = {'id': self.rid, 'state': 'ready', 'created': self.clock.wall,
                      'expires': self.clock.wall + 1800, 'generation': self.generation}
        self.caps = [self.clock.wall + 1800] * 6
        self.dispatches, self.finishes, self.recorded = [], [], []
        self.store = SimpleNamespace(admission=lambda *args: self.route,
            response_deadline_caps=lambda rid: self.caps,
            request_start=self.request_start, request_finish=self.request_finish,
            record_backend_response=lambda *args: self.recorded.append(args))
        self.lock, self.completion = threading.Lock(), threading.Event()
        self.owner = SimpleNamespace(store=self.store, closed=False,
            request_deadline=900, admission_wait=0,
            lock_for=lambda rid: self.lock, completion_for=lambda rid: self.completion)

    def request_start(self, *args, **kwargs):
        self.dispatches.append((args, kwargs))
        budget = kwargs['response_deadline']
        budget.tighten(*self.caps)
        return {'endpoint': 'http://127.0.0.1:54322/v1', 'response_deadline': budget.expires}

    def request_finish(self, *args):
        self.finishes.append(args)
        parsed = events(args[-1])
        return 'text_complete' if parsed and parsed[-1]['type'] == 'response.completed' else 'unknown'

    def invoke(self, response=None, **delays):
        response = response or FakeResponse(self.clock, [(0, frame())])
        connection = FakeConnection(self.clock, response, **delays)
        handler = gateway.Handler.__new__(gateway.Handler)
        handler.path = f'/activations/{self.generation}/v1/responses'
        handler.server = SimpleNamespace(owner=self.owner, server_port=54321)
        handler.connection = FakeSocket(self.clock)
        handler.client_address = ('127.0.0.1', 12345)
        handler.headers = Message()
        handler.headers.add_header('Host', '127.0.0.1:54321')
        for name, value in identity().items():
            handler.headers.add_header(name, value)
        raw = canonical(request('offline gateway'))
        handler.headers.add_header('Content-Type', 'application/json')
        handler.headers.add_header('Content-Length', str(len(raw)))
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        handler.statuses, handler.sent_headers = [], []
        handler.send_response = handler.statuses.append
        handler.send_header = lambda *values: handler.sent_headers.append(values)
        handler.end_headers = lambda: None
        with self.clock.patch(), patch.object(gateway.http.client, 'HTTPConnection', connection.construct), \
                patch.object(gateway.socket_select, 'select', return_value=([], [], [])):
            handler.do_POST()
        return handler, connection

    def test_long_responses_under_900_complete_once(self):
        for elapsed in (240, 700, 899.999):
            with self.subTest(elapsed=elapsed):
                self.setUp()
                handler, connection = self.invoke(FakeResponse(self.clock, [(elapsed, frame())]))
                self.assertEqual(events(handler.wfile.getvalue())[-1]['type'], 'response.completed')
                self.assertEqual(len(self.finishes), 1)
                self.assertEqual(len(self.dispatches), 1)
                self.assertEqual(sum(row[0] == 'request' for row in connection.calls), 1)
                from remote_transport.global_response import RESPONSE_DEADLINE_HEADER
                sent = next(row[4] for row in connection.calls if row[0] == 'request')
                self.assertEqual(float(sent[RESPONSE_DEADLINE_HEADER]), 2_000_000_900)
                self.assertEqual(connection.auto_open, 0)

    def test_body_return_at_deadline_cannot_emit_completed(self):
        handler, connection = self.invoke(FakeResponse(self.clock, [(900, frame())]))
        self.assertNotIn(b'event: response.completed', handler.wfile.getvalue())
        self.assertEqual(self.finishes, [])
        self.assertEqual(sum(row[0] == 'request' for row in connection.calls), 1)

    def test_gateway_clamps_connect_send_headers_and_body(self):
        for stage in ('connect', 'send', 'headers'):
            with self.subTest(stage=stage):
                self.setUp()
                handler, connection = self.invoke(**{stage: 900})
                self.assertNotIn(b'event: response.completed', handler.wfile.getvalue())
                self.assertEqual(self.finishes, [])
                self.assertLessEqual(sum(row[0] == 'request' for row in connection.calls), 1)
        self.setUp()
        handler, connection = self.invoke(FakeResponse(self.clock, [(201, frame())]), connect=240, send=240, headers=219)
        self.assertNotIn(b'event: response.completed', handler.wfile.getvalue())

    def test_gateway_honors_all_earlier_caps(self):
        for index in range(6):
            with self.subTest(cap=index):
                self.setUp()
                self.caps[index] = self.clock.wall + 300
                handler, _ = self.invoke(FakeResponse(self.clock, [(300, frame())]))
                self.assertNotIn(b'event: response.completed', handler.wfile.getvalue())
                self.assertEqual(self.finishes, [])

    def test_gateway_eof_and_malformed_frames_report_failure_without_retry(self):
        for raw, code in ((frame('response.in_progress'), 'downstream_response_incomplete'),
                          (frame().rstrip(b'\n'), 'truncated_downstream_sse_frame'),
                          (b'event: response.completed\ndata: broken-json\n\n', 'invalid_json')):
            with self.subTest(code=code):
                self.setUp()
                handler, connection = self.invoke(FakeResponse(self.clock, [(0, raw)]))
                output = events(handler.wfile.getvalue())
                self.assertEqual(output[-1]['type'], 'response.failed')
                self.assertIn(code, output[-1]['response']['error']['code'])
                self.assertNotIn(b'event: response.completed', handler.wfile.getvalue())
                self.assertEqual(sum(row[0] == 'request' for row in connection.calls), 1)

    def test_gateway_preserves_explicit_downstream_failure_code(self):
        handler, connection = self.invoke(FakeResponse(self.clock,
            [(0, frame('response.failed', code='remote_wait_budget_expired'))]))
        output = events(handler.wfile.getvalue())
        self.assertEqual(output[-1]['type'], 'response.failed')
        self.assertEqual(output[-1]['response']['error']['code'], 'remote_wait_budget_expired')
        self.assertEqual(sum(row[0] == 'request' for row in connection.calls), 1)

    def test_gateway_heartbeat_does_not_reset_deadline(self):
        response = FakeResponse(self.clock, heartbeat=True)
        handler, _ = self.invoke(response)
        self.assertEqual(response.reads, 9)
        self.assertNotIn(b'event: response.completed', handler.wfile.getvalue())
        self.assertEqual(len(self.dispatches), 1)


class ProviderTransportBudgetTests(unittest.TestCase):
    def setUp(self):
        from remote_transport.global_response import ResponseDeadline, response_operation
        self.clock = FakeClock()
        self.stack = self.clock.patch()
        self.addCleanup(self.stack.close)
        self.budget = ResponseDeadline()
        self.clock.advance(899)
        self.stack.enter_context(response_operation(self.budget))

    def test_drive_transport_timeout_is_clamped_to_last_second(self):
        from remote_tests.test_drive_http import Opener
        from remote_transport.drive_http import DriveHTTPClient
        opener = Opener()
        with patch('urllib.request.build_opener', return_value=opener):
            self.assertEqual(DriveHTTPClient(lambda: 'synthetic-token').get_bytes('id', 100), b'{}')
        self.assertEqual(len(opener.calls), 1)
        self.assertLessEqual(opener.calls[0][1], 1)

    def test_late_token_resolution_cannot_start_drive_http(self):
        from remote_tests.test_drive_http import Opener
        from remote_transport.drive_http import DriveHTTPClient
        opener = Opener()
        def token():
            self.clock.advance(1)
            return 'synthetic-token'
        with patch('urllib.request.build_opener', return_value=opener):
            with self.assertRaisesRegex(ProtocolError, 'budget_expired|deadline'):
                DriveHTTPClient(token).get_bytes('id', 100)
        self.assertEqual(opener.calls, [])

    def test_late_drive_headers_cannot_start_body_read(self):
        from remote_transport.drive_http import DriveHTTPClient
        body = io.BytesIO(b'{}')
        def open_late(*args, **kwargs):
            self.clock.advance(1)
            return body
        with patch('urllib.request.build_opener', return_value=SimpleNamespace(open=open_late)), \
                patch.object(body, 'read', wraps=body.read) as read:
            with self.assertRaisesRegex(ProtocolError, 'budget_expired|deadline'):
                DriveHTTPClient(lambda: 'synthetic-token').get_bytes('id', 100)
            read.assert_not_called()

    def test_late_drive_body_cannot_return_success(self):
        from remote_transport.drive_http import DriveHTTPClient
        body = io.BytesIO(b'{}')
        def read_late(*args):
            self.clock.advance(1)
            return b'{}'
        with patch('urllib.request.build_opener', return_value=SimpleNamespace(open=lambda *a, **k: body)), \
                patch.object(body, 'read', side_effect=read_late):
            with self.assertRaisesRegex(ProtocolError, 'budget_expired|deadline'):
                DriveHTTPClient(lambda: 'synthetic-token').get_bytes('id', 100)

    def test_docs_official_transport_timeout_is_clamped_and_restored(self):
        from examples.google_clients import DocsSDKClient
        observed = []
        transport = SimpleNamespace(timeout=20, connections={})
        service = SimpleNamespace(_http=SimpleNamespace(http=transport))
        service.documents = lambda: service
        service.get = lambda **kwargs: service
        def execute(**kwargs):
            observed.append((transport.timeout, kwargs))
            return {'documentId': 'offline-doc'}
        service.execute = execute
        result = DocsSDKClient(service).get_document('offline-doc')
        self.assertEqual(result, {'documentId': 'offline-doc'})
        self.assertEqual(len(observed), 1)
        self.assertLessEqual(observed[0][0], 1)
        self.assertEqual(observed[0][1], {'num_retries': 0})
        self.assertEqual(transport.timeout, 20)


class SignedResponseAuthorityTests(unittest.TestCase):
    def test_signed_child_handoff_caps_are_mirrored_and_heartbeat_cannot_renew_them(self):
        from remote_tests.test_global_heartbeat import GlobalHeartbeatTests
        from remote_transport.global_response import ResponseDeadline
        fixture = GlobalHeartbeatTests('test_exact_900_root_policy_and_no_old_protocol_migration')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.bridge.bootstrap_seconds = 600
        fixture.join(latency=0, seconds=1800)
        rid = fixture.demand()
        fixture.bridge.sync_heartbeat()
        state = fixture.bridge.read().state
        demand = state['logical']['demands'][rid]
        expected = min(state['expires'], demand['expires'],
                       state['logical']['controller']['lease_expires'],
                       demand['child_bootstrap']['expires'])
        caps = fixture.store.response_deadline_caps(rid)
        self.assertEqual(min(caps), expected)
        budget = ResponseDeadline(*caps)
        self.assertEqual(budget.expires, expected)
        fixture.clock += 240
        fixture.event('heartbeat')
        fixture.bridge.sync_heartbeat()
        refreshed = fixture.store.response_deadline_caps(rid)
        self.assertEqual(min(refreshed), expected)
        budget.tighten(*refreshed)
        self.assertEqual(budget.expires, expected)
        with fixture.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 0)


class FacadeProviderResponseBudgetTests(unittest.TestCase):
    """Real durable request, begin and committed result; provider clocks are fake."""
    def setUp(self):
        from remote_transport.backend import GoogleDriveBackend
        from remote_transport.control import GoogleDocsCASControlStore, SessionCoordinator, initial_state
        from remote_transport.controlled import CASController, CASWorker
        from remote_transport.facade import RemoteResponsesFacade
        from remote_transport.model import deployment
        from remote_transport.session import Journal
        from remote_tests.fakes import FakeDrive
        from remote_tests.test_docs_cas import FakeDocs
        self.clock = FakeClock()
        self.clock_context = self.clock.patch()
        self.addCleanup(self.clock_context.close)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.pin = deployment('offline-budget', 'synthetic/native', seconds=1800,
                              max_requests=128, scope='responses_tools')
        self.drive = FakeDrive()
        self.messages = GoogleDriveBackend(self.drive, 'folder', discovery='control_refs')
        self.docs = FakeDocs(initial_state(self.pin, 'control'))
        self.control = GoogleDocsCASControlStore(self.docs, 'doc', 'tab', 'control',
                                                'offline-budget', 'writer')
        self.coordinator = SessionCoordinator(self.control, self.messages)
        self.controller = CASController(Journal.provision(root/'controller', self.pin, 'controller'),
                                        self.messages, self.coordinator)
        self.worker = CASWorker(Journal.provision(root/'worker', self.pin, 'worker'),
                                self.messages, self.coordinator)
        self.facade = RemoteResponsesFacade(self.controller, long_session=True,
            request_deadline=900, response_expires=self.clock.wall + 1800)
        self.addCleanup(self.facade.close)
        self.body = {'model': 'native-subagent-bridge', 'stream': True, 'tools': [],
            'input': [{'type': 'message', 'role': 'user',
                       'content': [{'type': 'input_text', 'text': 'offline one begin'}]}]}
        self.client = identity()
        self.facade.context.identity = self.client
        self.rid = self.facade.server.store.enqueue(self.body, 'offline-budget', 900)['job_id']
        del self.facade.context.identity
        self.permit = self.worker.start_next()
        self.assertIsNotNone(self.permit)

    def assert_single_begin(self):
        with self.worker.journal.locked() as saved:
            self.assertEqual(len(saved['executions']), 1)
        with self.controller.journal.locked() as saved:
            self.assertEqual(len(saved['requests']), 1)
            self.assertEqual(len(saved['deliveries']), 0)

    def complete(self):
        self.result_id = self.worker.complete(self.permit, 'recoverable offline answer')

    def test_completed_at_240_700_and_899_999_is_observable(self):
        # A separate fixture per elapsed value avoids reusing the same deadline.
        self.complete()
        for elapsed in (240, 700, 899.999):
            self.clock.advance(elapsed - (self.clock.mono - 1000))
            state = self.facade.server.store.response_state(self.rid)
            self.assertEqual(state['state'], 'completed')
        self.assert_single_begin()

    def test_reopen_keeps_original_monotonic_deadline_and_late_result(self):
        from remote_transport.facade import RemoteResponsesFacade
        self.complete()
        original_deadline = self.facade.read_state()['jobs'][self.rid]['deadline']
        self.facade.close()
        self.clock.mono += 800
        self.clock.wall += 100
        self.facade = RemoteResponsesFacade(self.controller, long_session=True,
            request_deadline=900, response_expires=self.clock.wall + 1800)
        self.addCleanup(self.facade.close)
        self.assertEqual(self.facade.read_state()['jobs'][self.rid]['deadline'], original_deadline)
        self.clock.advance(100)
        state = self.facade.server.store.response_state(self.rid)
        self.assertEqual(state['state'], 'expired')
        status = self.facade.request_status(self.rid)
        self.assertTrue(status['wait_expired'])
        self.assertEqual(status['result_id'], self.result_id)
        self.assert_single_begin()

    def test_slow_docs_read_cannot_emit_completed_but_late_result_is_recoverable(self):
        self.complete()
        original = self.docs.get_document
        calls = []
        def slow(*args, **kwargs):
            value = original(*args, **kwargs)
            calls.append(True)
            self.clock.advance(900)
            return value
        with patch.object(self.docs, 'get_document', side_effect=slow):
            try:
                state = self.facade.server.store.response_state(self.rid)
                self.assertNotEqual(state['state'], 'completed')
            except Exception as exc:
                from remote_transport.wire import QueueError
                self.assertIsInstance(exc, QueueError)
                self.assertRegex(str(exc), 'budget_expired|deadline')
        self.assertEqual(len(calls), 1)
        status = self.facade.request_status(self.rid)
        self.assertEqual(status['result_id'], self.result_id)
        self.assert_single_begin()

    def test_slow_drive_fetch_cannot_emit_completed_or_start_another_provider_read(self):
        self.complete()
        original = self.drive.get_bytes
        calls = []
        def slow(*args, **kwargs):
            value = original(*args, **kwargs)
            calls.append(True)
            self.clock.advance(900)
            return value
        with patch.object(self.drive, 'get_bytes', side_effect=slow):
            try:
                state = self.facade.server.store.response_state(self.rid)
                self.assertNotEqual(state['state'], 'completed')
            except Exception as exc:
                from remote_transport.wire import QueueError
                self.assertIsInstance(exc, QueueError)
                self.assertRegex(str(exc), 'budget_expired|deadline')
        self.assertEqual(len(calls), 1)
        status = self.facade.request_status(self.rid)
        self.assertEqual(status['result_id'], self.result_id)
        self.assert_single_begin()

    def test_expiry_does_not_erase_result_or_refresh_on_same_request_retry(self):
        self.clock.advance(900)
        state = self.facade.server.store.response_state(self.rid)
        self.assertEqual(state['state'], 'expired')
        self.complete()
        self.facade.context.identity = self.client
        try:
            try:
                again = self.facade.server.store.enqueue(self.body, 'offline-budget', 900)['job_id']
                self.assertEqual(again, self.rid)
                after = self.facade.server.store.response_state(self.rid)
                self.assertNotEqual(after['state'], 'completed')
            except Exception as exc:
                from remote_transport.wire import QueueError
                self.assertIsInstance(exc, QueueError)
                self.assertRegex(str(exc), 'budget_expired|deadline')
        finally:
            del self.facade.context.identity
        self.assertEqual(self.facade.request_status(self.rid)['result_id'], self.result_id)
        self.assert_single_begin()


if __name__ == '__main__':
    unittest.main()
