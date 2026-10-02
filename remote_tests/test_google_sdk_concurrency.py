"""Offline shared-Docs transport regressions, including real SDK refresh.

Only synthetic credentials/resources and local facade sockets are used. The SDK
network boundary is replaced in every SDK test; no provider traffic is allowed.
"""
import concurrent.futures
import datetime
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from examples import google_clients as clients
from remote_transport.model import ProtocolError


class ProbeService:
    """Count overlap from resource construction through execution, not just I/O."""
    def __init__(self, dispatch=None):
        self.guard = threading.Lock()
        self.active = self.maximum = 0
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.hold_next = False
        self.dispatch = dispatch or (lambda method, arguments: {'documentId': arguments['documentId']})

    def documents(self):
        with self.guard:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        return self.Resource(self)

    class Resource:
        def __init__(self, owner): self.owner = owner
        def get(self, **kwargs): return self.Request(self.owner, 'get', kwargs)
        def batchUpdate(self, **kwargs): return self.Request(self.owner, 'write', kwargs)

        class Request:
            def __init__(self, owner, method, arguments):
                self.owner, self.method, self.arguments = owner, method, arguments
            def execute(self, **kwargs):
                owner = self.owner
                with owner.guard:
                    owner.calls.append((self.method, self.arguments, kwargs))
                    hold, owner.hold_next = owner.hold_next, False
                try:
                    if hold:
                        owner.entered.set()
                        if not owner.release.wait(5):
                            raise RuntimeError('synthetic_holder_not_released')
                    return owner.dispatch(self.method, self.arguments)
                finally:
                    with owner.guard: owner.active -= 1


class _ThreadFixture(unittest.TestCase):
    def setUp(self):
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix='facade-docs-test')
        self.addCleanup(self.pool.shutdown, wait=True, cancel_futures=True)

    def holder(self, client, service, *, write=False):
        service.hold_next = True
        # Release before shutting down the pool, even when an assertion fails.
        self.addCleanup(service.release.set)
        future = self.pool.submit(
            client.batch_update_document, 'holder', [], {'requiredRevisionId': 'r1'}
        ) if write else self.pool.submit(client.get_document, 'holder')
        self.assertTrue(service.entered.wait(2))
        return future


class ThreadTests(_ThreadFixture):
    def test_supervisor_and_several_facade_gets_serialize_construction_and_execution(self):
        service = ProbeService(); client = clients.DocsSDKClient(service)
        holder = self.holder(client, service)
        ready = [threading.Event() for _ in range(5)]
        futures = [self.pool.submit(client.get_document, 'facade-' + str(i), check=event.set)
                   for i, event in enumerate(ready)]
        for event in ready: self.assertTrue(event.wait(2))
        # Each caller has reached the pre-acquisition check while the first
        # transport remains in flight. No second resource may be constructed.
        self.assertEqual(service.maximum, 1)
        service.release.set()
        self.assertEqual(holder.result(2), {'documentId': 'holder'})
        self.assertEqual([f.result(2)['documentId'] for f in futures], ['facade-' + str(i) for i in range(5)])
        self.assertEqual(service.maximum, 1)
        self.assertEqual(service.active, 0)
        self.assertEqual(len(service.calls), 6)
        self.assertTrue(all(kwargs == {'num_retries': 0} for _, _, kwargs in service.calls))

    def test_get_and_exact_revision_cas_share_gate_in_both_directions(self):
        for write_first in (False, True):
            with self.subTest(write_first=write_first):
                service = ProbeService(); client = clients.DocsSDKClient(service)
                holder = self.holder(client, service, write=write_first)
                requests = [{'replaceAllText': {'synthetic': 'request'}}]
                queued = self.pool.submit(client.get_document, 'queued') if write_first else self.pool.submit(
                    client.batch_update_document, 'queued', requests, {'requiredRevisionId': 'r2'})
                service.release.set(); holder.result(2); queued.result(2)
                self.assertEqual(service.maximum, 1)
                self.assertEqual([method for method, _, _ in service.calls],
                                 ['write', 'get'] if write_first else ['get', 'write'])
                write = next(arguments for method, arguments, _ in service.calls if method == 'write')
                self.assertEqual(write['body']['writeControl'], {'requiredRevisionId': 'r1' if write_first else 'r2'})
                self.assertEqual(write['body']['requests'], [] if write_first else requests)

    def test_queued_deadline_expires_while_write_is_still_in_flight(self):
        service = ProbeService(); client = clients.DocsSDKClient(service)
        holder = self.holder(client, service, write=True)
        queued = self.pool.submit(client.get_document, 'expired', deadline=time.monotonic() + .04)
        with self.assertRaisesRegex(ProtocolError, '^docs_read_deadline_exceeded$'):
            queued.result(1)
        self.assertFalse(holder.done())
        self.assertEqual(len(service.calls), 1)
        service.release.set(); holder.result(2)
        self.assertEqual(client.get_document('next')['documentId'], 'next')

    def test_queued_cancellation_stays_unwrapped_and_never_dispatches(self):
        service = ProbeService(); client = clients.DocsSDKClient(service)
        holder = self.holder(client, service)
        started, cancelled = threading.Event(), threading.Event()
        def check():
            started.set()
            if cancelled.is_set(): raise ProtocolError('synthetic_stop_requested')
        queued = self.pool.submit(client.get_document, 'cancelled', check=check)
        self.assertTrue(started.wait(1)); cancelled.set()
        with self.assertRaisesRegex(ProtocolError, '^synthetic_stop_requested$') as caught:
            queued.result(1)
        self.assertNotIsInstance(caught.exception, clients.DocsReadError)
        self.assertFalse(holder.done())
        self.assertEqual(len(service.calls), 1)
        service.release.set(); holder.result(2)

    def test_budget_rechecked_after_lock_acquisition_and_failed_check_releases_it(self):
        service = ProbeService(); client = clients.DocsSDKClient(service)
        calls = []
        def check():
            calls.append(True)
            if len(calls) == 2: raise ProtocolError('synthetic_cancel_at_dispatch')
        with self.assertRaisesRegex(ProtocolError, '^synthetic_cancel_at_dispatch$'):
            client.get_document('cancelled', check=check)
        self.assertEqual(service.calls, [])
        self.assertEqual(self.pool.submit(client.get_document, 'next').result(1)['documentId'], 'next')

    def test_liveness_callback_can_make_nested_read_after_acquiring_gate(self):
        service = ProbeService(); client = clients.DocsSDKClient(service)
        checks = []
        def check():
            checks.append(True)
            if len(checks) == 2:
                self.assertEqual(client.get_document('nested')['documentId'], 'nested')
        self.assertEqual(self.pool.submit(client.get_document, 'outer', check=check).result(1)['documentId'], 'outer')
        self.assertEqual([args['documentId'] for _, args, _ in service.calls], ['nested', 'outer'])
        self.assertEqual(service.maximum, 1)

    def test_deadline_rechecked_after_successful_lock_acquisition(self):
        service = ProbeService(); client = clients.DocsSDKClient(service)
        now, checks = [100.0], []
        def check():
            checks.append(True)
            if len(checks) == 2: now[0] = 102.0
        with patch.object(clients.time, 'monotonic', side_effect=lambda: now[0]):
            with self.assertRaisesRegex(ProtocolError, '^docs_read_deadline_exceeded$'):
                client.get_document('expired', deadline=101.0, check=check)
        self.assertEqual(service.calls, [])
        self.assertEqual(self.pool.submit(client.get_document, 'next').result(1)['documentId'], 'next')

    def test_queued_write_deadline_and_stop_never_dispatch_or_replay(self):
        for mode in ('deadline', 'stop'):
            with self.subTest(mode=mode):
                service = ProbeService(); client = clients.DocsSDKClient(service)
                holder = self.holder(client, service)
                entered, cancelled = threading.Event(), threading.Event()
                def check():
                    entered.set()
                    if cancelled.is_set(): raise ProtocolError('synthetic_write_stopped')
                future = self.pool.submit(client.batch_update_document_guarded, 'queued', [],
                    {'requiredRevisionId': 'r1'}, deadline=time.monotonic() + .04 if mode == 'deadline' else None,
                    check=check)
                self.assertTrue(entered.wait(1))
                if mode == 'stop': cancelled.set()
                error = 'docs_write_deadline_exceeded' if mode == 'deadline' else 'synthetic_write_stopped'
                with self.assertRaisesRegex(ProtocolError, '^' + error + '$'): future.result(1)
                self.assertFalse(holder.done()); self.assertEqual(len(service.calls), 1)
                service.release.set(); holder.result(2)
                self.assertEqual(client.get_document('next')['documentId'], 'next')

    def test_queued_write_keeps_original_mutable_request_and_revision(self):
        service = ProbeService(); client = clients.DocsSDKClient(service)
        holder = self.holder(client, service); entered = threading.Event()
        requests, revision = [{'replaceAllText': {'replaceText': 'original'}}], {'requiredRevisionId': 'original'}
        future = self.pool.submit(client.batch_update_document, 'queued', requests, revision, check=entered.set)
        self.assertTrue(entered.wait(1))
        requests[0]['replaceAllText']['replaceText'] = 'changed'; revision['requiredRevisionId'] = 'changed'
        service.release.set(); holder.result(2); future.result(2)
        self.assertEqual(service.calls[-1][1]['body'], {'requests': [{'replaceAllText': {'replaceText': 'original'}}],
                                                       'writeControl': {'requiredRevisionId': 'original'}})

    def test_invalid_write_budgets_never_touch_transport(self):
        service = ProbeService(); client = clients.DocsSDKClient(service)
        for deadline in (True, '100', float('inf'), float('nan')):
            with self.assertRaisesRegex(ProtocolError, '^invalid_docs_write_deadline$'):
                client.batch_update_document('invalid', [], {'requiredRevisionId': 'r1'}, deadline=deadline)
        with self.assertRaisesRegex(ProtocolError, '^invalid_docs_write_check$'):
            client.batch_update_document('invalid', [], {'requiredRevisionId': 'r1'}, check=True)
        self.assertEqual(service.calls, [])

    def test_backoff_releases_gate_and_write_error_is_not_replayed(self):
        read_attempts = []
        def dispatch(method, arguments):
            if method == 'write': raise TimeoutError('private write reply')
            read_attempts.append(True)
            if len(read_attempts) == 1: raise TimeoutError('private read reply')
            return {'ok': True}
        service = ProbeService(dispatch); client = clients.DocsSDKClient(service)
        def backoff(delay):
            self.assertEqual(delay, 1)
            future = self.pool.submit(client.batch_update_document, 'write', [], {'requiredRevisionId': 'r1'})
            with self.assertRaisesRegex(ProtocolError, '^docs_write_outcome_unknown$'):
                future.result(1)
        with patch.object(clients.time, 'sleep', side_effect=backoff):
            self.assertEqual(client.get_document('read'), {'ok': True})
        self.assertEqual([method for method, _, _ in service.calls], ['get', 'write', 'get'])
        self.assertEqual(service.maximum, 1)

    def test_several_real_facades_share_client_and_cleanup_threads(self):
        from remote_transport import deployment, Journal, LocalFSBackend, CASController
        from remote_transport.control import initial_state, GoogleDocsCASControlStore, SessionCoordinator
        from remote_transport.facade import RemoteResponsesFacade
        from remote_tests.test_docs_cas import FakeDocs
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name); resources = {}
        def dispatch(method, arguments):
            target = resources[arguments['documentId']]
            if method == 'get': return target.get_document(arguments['documentId'])
            return target.batch_update_document(arguments['documentId'], arguments['body']['requests'],
                                                arguments['body']['writeControl'])
        service = ProbeService(dispatch); client = clients.DocsSDKClient(service); facades = []
        for index in range(3):
            session, document = 'session-' + str(index), 'document-' + str(index)
            pin = deployment(session, 'synthetic/native-' + str(index))
            resources[document] = FakeDocs(initial_state(pin, 'control'))
            messages = LocalFSBackend(root / session / 'messages', create=True)
            control = GoogleDocsCASControlStore(client, document, 'tab', 'control', session, 'writer')
            controller = CASController(Journal.provision(root / session / 'controller', pin, 'controller'),
                                       messages, SessionCoordinator(control, messages))
            facade = RemoteResponsesFacade(controller, long_session=True).start()
            self.addCleanup(facade.close); facades.append(facade)
        resources['holder'] = resources['document-0']
        holder = self.holder(client, service)
        def close_remote(index):
            connection = http.client.HTTPConnection('127.0.0.1', facades[index].server.server_port, timeout=3)
            try:
                connection.request('POST', '/v1/bridge/close', body='{"confirm":true}', headers={
                    'Content-Type': 'application/json', 'session-id': 'synthetic-session-' + str(index),
                    'thread-id': '00000000-0000-0000-0000-' + str(index).zfill(12)})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally: connection.close()
        futures = [self.pool.submit(close_remote, index) for index in range(3)]
        service.release.set(); holder.result(2)
        for future in futures:
            status, value = future.result(3)
            self.assertEqual(status, 200, value); self.assertTrue(value['closed'])
        self.assertEqual(service.maximum, 1)
        self.assertEqual(sum(method == 'write' for method, _, _ in service.calls), 3)
        for facade in facades:
            facade.close()
            self.assertFalse(facade.thread.is_alive())
            self.assertEqual(facade.server.fileno(), -1)


class SafeDiagnosticsTests(unittest.TestCase):
    def test_http_state_errors_remain_terminal_and_identifiable(self):
        for kind in (http.client.ResponseNotReady, http.client.CannotSendRequest, http.client.BadStatusLine):
            with self.subTest(kind=kind):
                failure = clients._read_failure(kind('private body https://secret.invalid token=secret'), 1)
                self.assertFalse(failure.retryable)
                self.assertEqual(failure.diagnostics, {'category': 'unexpected', 'attempts': 1,
                    'exception_class': 'http.client.' + kind.__name__,
                    'exception_chain': ['http.client.' + kind.__name__]})

    def test_custom_secret_class_name_and_formatted_exception_are_never_exposed(self):
        class Dangerous(Exception):
            def __str__(self): raise AssertionError('must not format provider error')
        for name in ('secret_token_abc', 'https://private.invalid/token', 'X' * 10000):
            kind = type(name, (Dangerous,), {'__module__': 'private_secret_module'})
            inner = kind('secret_body'); outer = http.client.ResponseNotReady('secret_url')
            outer.__cause__ = inner
            failure = clients._read_failure(outer, 1)
            self.assertEqual(failure.diagnostics['exception_chain'], ['http.client.ResponseNotReady', 'unrecognized'])
            serialized = json.dumps(failure.diagnostics)
            self.assertNotIn('secret', serialized); self.assertNotIn('private', serialized)
            self.assertLess(len(serialized), 350)

    def test_cyclic_and_overdeep_causes_are_bounded(self):
        cyclic = http.client.ResponseNotReady('private'); cyclic.__cause__ = cyclic
        self.assertEqual(clients._read_failure(cyclic, 1).diagnostics['exception_chain'],
                         ['http.client.ResponseNotReady'])
        failure = ValueError('private')
        for _ in range(12):
            outer = ConnectionError('private'); outer.__cause__ = failure; failure = outer
        diagnostic = clients._read_failure(failure, 1)
        self.assertFalse(diagnostic.retryable)
        self.assertTrue(diagnostic.diagnostics['exception_chain_truncated'])
        self.assertLessEqual(len(diagnostic.diagnostics['exception_chain']), 8)


class WriteCallerBudgetTests(_ThreadFixture):
    def test_control_snapshot_ttl_and_begin_authority_rechecked_after_wait(self):
        from remote_transport import deployment
        from remote_transport.control import GoogleDocsCASControlStore, SessionCoordinator, initial_state, block_for
        from remote_transport.model import canonical, hash_bytes
        from remote_tests.test_router_lifecycle import document
        for mode in ('snapshot', 'begin'):
            with self.subTest(mode=mode):
                service = ProbeService(); client = clients.DocsSDKClient(service)
                now, prepared = [100.0], threading.Event()
                class Store(GoogleDocsCASControlStore):
                    def prepare_update(self, *args):
                        value = super().prepare_update(*args); prepared.set(); return value
                store = Store(client, 'control', 't.0', 'cid', 'session', 'writer')
                with patch('time.monotonic', side_effect=lambda: now[0]), patch('time.time', side_effect=lambda: now[0]):
                    pin = deployment('session', 'synthetic/native', seconds=1 if mode == 'begin' else 600)
                    state = initial_state(pin, 'cid'); binding = state['binding']
                    kind, args = 'close', {'binding': binding}
                    if mode == 'begin':
                        state.update(phase='CLAIMED', admissions=1, request={'object_id': 'a' * 64,
                            'locator': {'backend': 'drive', 'folder_id': 'folder', 'file_id': 'file'}, 'message_seq': 1},
                            claim={'id': 'claim', 'worker_id': binding['worker_id'], 'generation': binding['generation']})
                        dispatch = hash_bytes(canonical({'request': 'a' * 64, 'incarnation': binding['journal_id']}))
                        kind, args = 'begin', {'binding': binding, 'claim_id': 'claim', 'dispatch_id': dispatch}
                    snapshot = store.snapshot_from_document(document('control', 't.0', 'r1', block_for(state)))
                    new = SessionCoordinator(store, None).plan(kind, args, 'operation', snapshot=snapshot)['state']
                    holder = self.holder(client, service)
                    future = self.pool.submit(store.compare_and_swap, snapshot, new)
                    self.assertTrue(prepared.wait(1)); now[0] = 102.0 if mode == 'begin' else 131.0
                    service.release.set(); holder.result(2)
                    with self.assertRaises(ProtocolError): future.result(2)
                self.assertFalse(any(method == 'write' for method, _, _ in service.calls))

    def test_recovering_owner_stop_rechecked_after_wait(self):
        from remote_transport.global_desktop import RecoveringDocs
        service = ProbeService(); client = clients.DocsSDKClient(service)
        checked = threading.Event(); now = time.time()
        class Store:
            def activation(self, generation): return {'expires': int(now) + 600, 'enabled': True}
            def require_read_recovery_current(self): checked.set()
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / 'outer' / 'run'; runtime.mkdir(parents=True)
            recovery = RecoveringDocs(client, store=Store(), runtime=runtime,
                spec={'expires': int(now) + 600, 'generation': 'synthetic', 'stop_intent_id': None},
                on_pause=lambda **kwargs: None, on_resume=lambda: None)
            holder = self.holder(client, service)
            def stop():
                if not checked.wait(1): raise AssertionError('owner_check_not_reached')
                (runtime / 'stop.json').write_text('{}'); service.release.set()
            stopper = self.pool.submit(stop)
            with self.assertRaisesRegex(ProtocolError, '^global_stop_requested$'):
                recovery.batch_update_document('control', [], {'requiredRevisionId': 'r1'})
            holder.result(2); stopper.result(2)
        self.assertFalse(any(method == 'write' for method, _, _ in service.calls))

    def test_bootstrap_nonterminal_dispatch_expiry_but_terminal_cleanup_allowed(self):
        from remote_transport import router_mac
        for terminal in (False, True):
            with self.subTest(terminal=terminal), tempfile.TemporaryDirectory() as temporary:
                service = ProbeService(); client = clients.DocsSDKClient(service)
                holder = self.holder(client, service); queued = threading.Event(); now = [100.0]
                packet = {'expected_state': {'epoch': 1, 'stage': 'CLOSED' if terminal else 'BUNDLE_READY',
                          'created': 100, 'expires': 160, 'events': [{'at': 100}]},
                          'tool_arguments': {'document_id': 'child', 'requests': [],
                                             'write_control': {'requiredRevisionId': 'r1'}}}
                original = client.batch_update_document_guarded
                def guarded(*args, **kwargs):
                    queued.set(); return original(*args, **kwargs)
                def verify(packet, response, readback, **kwargs):
                    if response is None: raise ProtocolError('synthetic_operation_absent')
                    return packet['expected_state']
                with patch.object(client, 'batch_update_document_guarded', side_effect=guarded), \
                        patch('time.time', side_effect=lambda: now[0]):
                    future = self.pool.submit(router_mac._dispatch_bootstrap_plan, client, packet, verify,
                                              join_code='synthetic', runtime=temporary)
                    self.assertTrue(queued.wait(1)); now[0] = 161.0; service.release.set(); holder.result(2)
                    if terminal: self.assertEqual(future.result(2)['stage'], 'CLOSED')
                    else:
                        with self.assertRaisesRegex(RuntimeError, '^bootstrap_write_outcome_unknown_no_replay$'):
                            future.result(2)
                self.assertEqual(sum(method == 'write' for method, _, _ in service.calls), int(terminal))

    def test_legacy_three_argument_port_is_called_once_without_keyword_probe(self):
        from remote_transport.control import dispatch_docs_write
        class LegacyPort:
            def __init__(self): self.calls = 0
            def batch_update_document(self, document_id, requests, write_control):
                self.calls += 1; raise TypeError('synthetic outcome unknown')
        port = LegacyPort(); checks = []
        with self.assertRaises(TypeError):
            dispatch_docs_write(port, 'control', [], {'requiredRevisionId': 'r1'}, check=lambda: checks.append(True))
        self.assertEqual(port.calls, 1); self.assertEqual(checks, [True])

    def test_queue_initialization_and_event_budgets_reach_sdk_dispatch(self):
        import secrets
        from remote_tests.test_global_control import FakeGoogle
        from remote_transport import global_control as queue
        from remote_transport.global_gateway import Store
        from remote_transport.global_google import GoogleQueueBridge
        from remote_transport.router_join import _source_hashes
        from remote_transport.selection import load_catalog, select
        for mode in ('initialize', 'event_expiry', 'event_future'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                now = [1000.0]
                google = FakeGoogle()
                def dispatch(method, arguments):
                    if method == 'get': return google.get_document(arguments['documentId'])
                    return google.batch_update_document(arguments['documentId'], arguments['body']['requests'],
                                                        arguments['body']['writeControl'])
                service = ProbeService(dispatch)
                class DelayedClient(clients.DocsSDKClient):
                    jump = None
                    def batch_update_document_guarded(self, *args, **kwargs):
                        if self.jump is not None: now[0] = self.jump
                        return super().batch_update_document_guarded(*args, **kwargs)
                client = DelayedClient(service)
                with patch('time.time', side_effect=lambda: now[0]):
                    store, generation = Store.initialize(Path(temporary) / 'gateway', select(load_catalog(), 'gpt-6.1-sol', 'high'))
                    document = google.create_document_once('folder', 'synthetic')
                    code = secrets.token_hex(32)
                    initial = queue.initial(activation_id=generation, queue_id=secrets.token_hex(16), folder_id='folder',
                        document_id=document, tab_id='t.0', join_code=code, created=1000, expires=1900,
                        runtime_source_hashes=_source_hashes())
                    bridge = GoogleQueueBridge(store, Path(temporary) / 'bridge', client, google, initial, code)
                    try:
                        if mode == 'initialize':
                            client.jump = 1901.0
                            with self.assertRaisesRegex(ProtocolError, '^global_queue_initialization_unknown_no_retry$'):
                                bridge.initialize_blank_queue()
                        else:
                            bridge.initialize_blank_queue()
                            client.jump = 1000 + initial['controller_timing']['operation_budget_seconds'] + 1 if mode == 'event_expiry' else 999.0
                            with self.assertRaisesRegex(ProtocolError, '^global_mac_cas_unknown_no_retry$'):
                                bridge.event('close', {'confirm': True})
                        self.assertEqual(sum(method == 'write' for method, _, _ in service.calls), int(mode != 'initialize'))
                    finally: bridge.close()

    def test_blank_control_initialization_rechecks_pin_expiry_at_dispatch(self):
        from examples.remote_setup import initialize_blank
        from remote_tests.test_google_examples import BlankDocument
        from remote_transport import deployment
        now = [1000.0]; blank = BlankDocument()
        def dispatch(method, arguments):
            if method == 'get': return blank.get_document(arguments['documentId'])
            return blank.batch_update_document(arguments['documentId'], arguments['body']['requests'],
                                                arguments['body']['writeControl'])
        class DelayedClient(clients.DocsSDKClient):
            def batch_update_document_guarded(self, *args, **kwargs):
                now[0] = 1002.0
                return super().batch_update_document_guarded(*args, **kwargs)
        service = ProbeService(dispatch); client = DelayedClient(service)
        with patch('time.time', side_effect=lambda: now[0]):
            pin = deployment('synthetic-session', 'synthetic/native', seconds=1)
            with self.assertRaisesRegex(ProtocolError, '^control_deployment_expired$'):
                initialize_blank(client, pin, 'doc', 'tab', 'control', 'writer')
        self.assertEqual(blank.calls, [])
        self.assertFalse(any(method == 'write' for method, _, _ in service.calls))


class RealSDKConcurrencyTests(_ThreadFixture):
    def setUp(self):
        try:
            import httplib2
            from google.oauth2.credentials import Credentials
            from google_auth_httplib2 import AuthorizedHttp
            from googleapiclient.discovery import build
        except ImportError:
            self.skipTest('optional Google SDK dependencies are not installed')
        super().setUp()
        self.httplib2, self.Credentials, self.AuthorizedHttp, self.build = httplib2, Credentials, AuthorizedHttp, build
        guard = patch('socket.socket', side_effect=AssertionError('SDK tests forbid network'))
        guard.start(); self.addCleanup(guard.stop)

    def test_refresh_and_transport_are_serialized_across_get_and_cas(self):
        credentials = self.Credentials(token=None, refresh_token='synthetic-refresh', client_id='synthetic-client',
                                       client_secret='synthetic-secret', token_uri='https://oauth2.googleapis.com/token')
        entered, release = threading.Event(), threading.Event(); self.addCleanup(release.set)
        counters = {'active': 0, 'maximum': 0, 'refresh': 0}; guard = threading.Lock(); sent = []
        def refresh(request):
            with guard:
                counters['refresh'] += 1; counters['active'] += 1
                counters['maximum'] = max(counters['maximum'], counters['active'])
            try:
                entered.set()
                if not release.wait(3): raise RuntimeError('synthetic_refresh_not_released')
                credentials.token = 'synthetic-access'; credentials.expiry = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(hours=1)
            finally:
                with guard: counters['active'] -= 1
        def transport(uri, method='GET', body=None, headers=None, **kwargs):
            with guard:
                counters['active'] += 1; counters['maximum'] = max(counters['maximum'], counters['active'])
            try:
                sent.append((method, body, headers['authorization']))
                return self.httplib2.Response({'status': '200'}), b'{"documentId":"synthetic-document"}'
            finally:
                with guard: counters['active'] -= 1
        http = self.httplib2.Http(timeout=1)
        authorized = self.AuthorizedHttp(credentials, http=http, max_refresh_attempts=0)
        service = self.build('docs', 'v1', http=authorized, static_discovery=True, cache_discovery=False, num_retries=0)
        client = clients.DocsSDKClient(service)
        with patch.object(credentials, 'refresh', side_effect=refresh), patch.object(http, 'request', side_effect=transport):
            first = self.pool.submit(client.get_document, 'synthetic-document')
            self.assertTrue(entered.wait(1))
            ready = [threading.Event() for _ in range(3)]
            others = [self.pool.submit(client.get_document, 'synthetic-document', check=event.set) for event in ready]
            for event in ready: self.assertTrue(event.wait(1))
            write = self.pool.submit(client.batch_update_document, 'synthetic-document', [], {'requiredRevisionId': 'r1'})
            release.set(); first.result(2)
            for future in others: future.result(2)
            write.result(2)
        self.assertEqual(counters, {'active': 0, 'maximum': 1, 'refresh': 1})
        self.assertEqual(len(sent), 5)
        self.assertEqual(sum(method == 'POST' for method, _, _ in sent), 1)
        self.assertTrue(all(auth == 'Bearer synthetic-access' for _, _, auth in sent))
        self.assertEqual(json.loads(next(body for method, body, _ in sent if method == 'POST'))['writeControl'],
                         {'requiredRevisionId': 'r1'})

    def test_refresh_failure_is_terminal_sanitized_and_releases_gate(self):
        from google.auth.exceptions import RefreshError
        credentials = self.Credentials(token=None, refresh_token='synthetic-refresh', client_id='synthetic-client',
                                       client_secret='synthetic-secret', token_uri='https://oauth2.googleapis.com/token')
        http = self.httplib2.Http(timeout=1)
        authorized = self.AuthorizedHttp(credentials, http=http, max_refresh_attempts=0)
        service = self.build('docs', 'v1', http=authorized, static_discovery=True, cache_discovery=False, num_retries=0)
        client = clients.DocsSDKClient(service)
        with patch.object(credentials, 'refresh', side_effect=RefreshError('private body token=secret')) as refresh, \
                patch.object(http, 'request', return_value=(self.httplib2.Response({'status': '200'}),
                                                            b'{"documentId":"synthetic-document"}')) as transport, \
                patch.object(clients.time, 'sleep') as sleep:
            with self.assertRaises(clients.DocsReadError) as caught:
                client.get_document('synthetic-document')
            failure = caught.exception
            self.assertFalse(failure.retryable)
            self.assertEqual(failure.diagnostics, {'category': 'authorization', 'attempts': 1,
                'exception_class': 'google.auth.exceptions.RefreshError',
                'exception_chain': ['google.auth.exceptions.RefreshError']})
            self.assertNotIn('secret', json.dumps(failure.diagnostics))
            refresh.assert_called_once(); transport.assert_not_called(); sleep.assert_not_called()
            credentials.token = 'synthetic-access'
            credentials.expiry = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(hours=1)
            self.assertEqual(self.pool.submit(client.get_document, 'synthetic-document').result(1),
                             {'documentId': 'synthetic-document'})
            transport.assert_called_once(); refresh.assert_called_once()


if __name__ == '__main__': unittest.main()
