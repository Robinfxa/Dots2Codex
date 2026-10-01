"""Independent fault regressions through the real Docs adapter and signed queue.

Every Google operation is synthetic. Faults execute at the SDK request boundary,
so these tests exercise retry, authentication, CAS reconciliation and liveness
without a credential, network request, real native spawn or external write.
"""
import copy
import concurrent.futures
from contextlib import ExitStack
import json
from pathlib import Path
import secrets
import socket
import tempfile
import time
import types
import unittest
from unittest.mock import patch, MagicMock

from examples import google_clients as clients
from remote_tests.test_global_control import FakeGoogle
from remote_transport import global_control as queue, global_pilot as pilot, global_desktop as desktop
from remote_transport.global_fixture import identity
from remote_transport.global_gateway import Store, private_dir
from remote_transport.global_google import GoogleQueueBridge
from remote_transport.global_native import NativeLedger
from remote_transport.global_timing import TIMING
from remote_transport.model import ProtocolError, hash_bytes
from remote_transport.router_join import _source_hashes
from remote_transport.selection import load_catalog, select


class ProviderHTTPError(Exception):
    """The response attributes exposed by googleapiclient.errors.HttpError."""
    def __init__(self, status):
        self.resp = types.SimpleNamespace(status=status)
        self.content = b'{"error":"private document and credential information"}'
        self.uri = 'https://provider.invalid/private-document?token=secret'
        super().__init__('private provider error, credential=secret')


class FaultRequest:
    def __init__(self, service, operation, arguments):
        self.service, self.operation, self.arguments = service, operation, arguments

    def execute(self, **kwargs):
        self.service.executions.append((self.operation, copy.deepcopy(kwargs)))
        if self.operation == 'get':
            self.service.read_times.append(time.time())
            fault = self.service.read_faults.pop(0) if self.service.read_faults else None
            if fault is not None:
                raise fault
            return self.service.google.get_document(self.arguments['documentId'])
        result = self.service.google.batch_update_document(
            self.arguments['documentId'], self.arguments['body']['requests'],
            self.arguments['body']['writeControl'])
        if self.service.after_write is not None:
            self.service.after_write()
        return result


class FaultDocsService:
    def __init__(self, google):
        self.google = google
        self.read_faults = []
        self.executions = []
        self.read_times = []
        self.after_write = None

    def documents(self):
        return self

    def get(self, **kwargs):
        return FaultRequest(self, 'get', kwargs)

    def batchUpdate(self, **kwargs):
        return FaultRequest(self, 'batch', kwargs)

    def count(self, operation):
        return sum(kind == operation for kind, _ in self.executions)


class GlobalReadRecoveryFaultTests(unittest.TestCase):
    def setUp(self):
        self.now = 2_000_000_000.0
        clock = patch('time.time', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.selection = select(load_catalog(), 'gpt-6.1-sol', 'high')
        self.store, self.generation = Store.initialize(self.root/'gateway', self.selection)
        self.google = FakeGoogle()
        self.service = FaultDocsService(self.google)
        self.docs = clients.DocsSDKClient(self.service)
        self.code = secrets.token_hex(32)
        document_id = self.google.create_document_once('folder', 'offline fault fixture')
        self.initial = queue.initial(
            activation_id=self.generation, queue_id=secrets.token_hex(16), folder_id='folder',
            document_id=document_id, tab_id='t.0', join_code=self.code,
            created=int(self.now), expires=int(self.now)+1800,
            runtime_source_hashes=_source_hashes())
        self.bridge = GoogleQueueBridge(
            self.store, self.root/'bridge', self.docs, self.google, self.initial, self.code)
        self.addCleanup(self.bridge.close)
        self.bridge.initialize_blank_queue()
        self.ledger = NativeLedger(self.root/'native', self.initial, self.code, '/root/fault_fixture')
        self.serial = 0
        self.sleep_durations = []
        # The adapter imports time only when its recovery implementation is present.
        # Patching the stdlib object also reaches clients.time without any real delay.
        sleeper = patch('time.sleep', side_effect=self.advance)
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def advance(self, seconds):
        self.sleep_durations.append(seconds)
        self.now += seconds

    def plan(self, kind, **kwargs):
        self.serial += 1
        path = self.root/'native'/f'{self.serial}-{kind}.json'
        out = self.ledger.plan_event(self.bridge.read(), kind, path, **kwargs)
        return path, out

    def event(self, kind, **kwargs):
        path, out = self.plan(kind, **kwargs)
        response = self.docs.batch_update_document(**out['tool_arguments'])
        return self.ledger.verify_plan(path, response, self.docs.get_document(self.initial['document_id']))

    def join(self, seconds=1200):
        self.event('join', capacity=2, seconds=seconds)
        self.now += 1
        self.event('heartbeat')
        self.bridge.sync_heartbeat()
        return copy.deepcopy(self.bridge.read().state['logical']['controller'])

    def assert_sdk_replay_disabled(self):
        self.assertTrue(self.service.executions)
        self.assertTrue(all(kwargs == {'num_retries': 0} for _, kwargs in self.service.executions))

    def recovering_docs(self, expires=None, on_pause=None):
        runtime = self.root/'runs'/'supervisor'
        private_dir(runtime, create=True)
        self.pause_reports = []
        self.resume_reports = []
        clock = patch('time.monotonic', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        def paused(**values):
            self.pause_reports.append(values)
            if on_pause is not None:
                on_pause(values)
        proxy = desktop.RecoveringDocs(
            self.docs, store=self.store, runtime=runtime,
            spec={'generation': self.generation, 'expires': expires or self.initial['expires']},
            on_pause=paused, on_resume=lambda: self.resume_reports.append(True),
            now=lambda: self.now, monotonic=lambda: self.now, wait=self.advance)
        self.bridge.docs = proxy
        return proxy

    def test_partial_child_create_recovery_does_not_restart_or_duplicate_effects(self):
        self.join()
        route = self.store.admission(self.generation, identity(), self.selection)
        original_create = self.google.create_document_once
        counts = {'create': 0}
        def create_with_post_effect_read_fault(folder, title):
            result = original_create(folder, title)
            counts['create'] += 1
            if counts['create'] == 1:
                self.service.read_faults = [ProviderHTTPError(503) for _ in range(3)]
            return result
        def while_paused(_):
            self.assertTrue(self.store.status()['docs_read_paused'])
            self.assertFalse(self.store.status()['controller_active'])
            with self.assertRaisesRegex(ProtocolError, 'read_recovery_pending'):
                self.store.admission(self.generation, identity(), self.selection)
        proxy = self.recovering_docs(on_pause=while_paused)
        with patch.object(self.google, 'create_document_once', side_effect=create_with_post_effect_read_fault):
            self.bridge.prepare_child(route['id'])
        self.assertEqual(counts['create'], 2)
        self.assertEqual(len(self.pause_reports), 1)
        self.assertTrue(proxy.paused)
        self.assertEqual(self.resume_reports, [])
        # Success on a child-document GET cannot reopen local client admission.
        self.assertTrue(self.store.status()['docs_read_paused'])
        source = self.bridge.read().state
        self.assertEqual(list(source['logical']['demands']), [route['id']])
        proxy.reconcile(self.bridge)
        self.assertFalse(proxy.paused)
        self.assertFalse(self.store.status()['docs_read_paused'])
        self.assertEqual(self.resume_reports, [True])
        self.assertTrue(self.store.activation()['enabled'])
        self.assertEqual(len(self.google.docs), 3)

    def test_proxy_recovers_mac_write_readback_without_resending_unknown_write(self):
        proxy = self.recovering_docs()
        original_batch = self.google.batch_update_document
        def applied_with_lost_reply(*args, **kwargs):
            original_batch(*args, **kwargs)
            self.service.read_faults = [ProviderHTTPError(503) for _ in range(3)]
            raise TimeoutError('secret lost reply')
        before_writes = self.service.count('batch')
        with patch.object(self.google, 'batch_update_document', side_effect=applied_with_lost_reply):
            result = self.bridge.event('close', {'confirm': True})
        self.assertTrue(result.state['logical']['closed'])
        self.assertEqual(self.service.count('batch')-before_writes, 1)
        self.assertEqual(len(self.pause_reports), 1)
        self.assertTrue(proxy.paused)
        record = json.loads((self.bridge.root/'queue-close-controller.json').read_text())
        self.assertEqual(record['status'], 'verified')
        with self.assertRaisesRegex(ProtocolError, 'queue_closed'):
            proxy.reconcile(self.bridge)
        self.assertTrue(self.store.status()['docs_read_paused'])
        self.assertEqual(self.resume_reports, [])

    def test_successful_http_read_does_not_unpause_a_tampered_signed_queue(self):
        proxy = self.recovering_docs()
        self.service.read_faults = [ProviderHTTPError(503) for _ in range(3)]
        proxy.get_document(self.initial['document_id'])
        self.assertTrue(proxy.paused)
        record = self.google.docs[self.initial['document_id']]
        record[0] = record[0].replace('"closed":false', '"closed":true')
        record[1] += 1
        with self.assertRaisesRegex(ProtocolError, 'projection'):
            proxy.reconcile(self.bridge)
        self.assertTrue(self.store.status()['docs_read_paused'])
        self.assertEqual(self.resume_reports, [])

    def test_paused_read_recovery_stops_at_activation_expiry_without_new_write(self):
        proxy = self.recovering_docs(expires=self.now+5)
        self.service.read_faults = [ProviderHTTPError(503) for _ in range(30)]
        before_writes = self.service.count('batch')
        before_gets = self.service.count('get')
        with self.assertRaisesRegex(ProtocolError, 'recovery_expired'):
            proxy.get_document(self.initial['document_id'])
        self.assertEqual(self.now, proxy.expires)
        self.assertTrue(proxy.paused)
        self.assertEqual(self.resume_reports, [])
        self.assertEqual(self.service.count('batch'), before_writes)
        self.assertLessEqual(self.service.count('get')-before_gets, 4)
        self.assertTrue(all(at < proxy.expires for at in self.service.read_times[before_gets:]))
        self.assertEqual(len(self.pause_reports), 1)
        snapshot = queue.snapshot(self.google.get_document(self.initial['document_id']), self.initial['document_id'], 't.0')
        self.assertFalse(snapshot.state['logical']['closed'])

    def test_pause_respects_explicit_stop_before_another_sdk_attempt(self):
        def stop_on_pause(_):
            desktop.save(self.root/'runs'/'supervisor'/'stop.json', {'requested': self.now})
        proxy = self.recovering_docs(on_pause=stop_on_pause)
        self.service.read_faults = [TimeoutError('secret') for _ in range(3)]
        before_gets = self.service.count('get')
        with self.assertRaisesRegex(ProtocolError, '^global_stop_requested$'):
            proxy.get_document(self.initial['document_id'])
        self.assertEqual(self.service.count('get')-before_gets, 3)
        self.assertEqual(self.resume_reports, [])
        self.assertTrue(proxy.paused)

    def test_supervisor_preflight_race_keeps_activation_without_replaying_post(self):
        self.join()
        runtime = self.root
        raw = b'{}'
        desktop.private_write(runtime/'router-config.json', raw)
        desktop.save(runtime/'spec.json', {
            'contract': desktop.CONTRACT, 'runtime': str(runtime), 'run_id': 'a'*32,
            'generation': self.generation, 'expires': self.now+25,
            'config_hash': hash_bytes(raw), 'cli': {'path': '/fixture/codex'},
            'desktop': {'path': '/fixture/desktop'}, 'codex_home': '/fixture/home'})
        cfg = {'folder_id': 'folder', 'mac_writer_identity': 'global-mac',
               'worker_writer_identity': 'global-native'}
        plan = {'plan_id': 'synthetic-plan', 'route_id': 'b'*32}
        future = concurrent.futures.Future()
        pool = MagicMock()
        statuses, rejected, unresolved_observations = [], [], []
        original_save = desktop.save

        def prepare(_store, _root, docs, _drive, _folder, **_kwargs):
            self.bridge.docs = docs
            return self.bridge

        def submit(*_args):
            # One async POST has been issued. The next polling GET will exhaust
            # its transport budget before that POST reaches gateway admission.
            self.service.read_faults = [ProviderHTTPError(503) for _ in range(3)]
            return future
        pool.submit.side_effect = submit

        def wait(seconds):
            self.advance(seconds)
            if self.store.status()['docs_read_paused'] and not future.done():
                try:
                    self.store.admission(self.generation, identity(), self.selection)
                except ProtocolError as error:
                    rejected.append(str(error))
                    future.set_exception(ProtocolError('global_preflight_request_failed'))

        def save(path, value):
            original_save(path, value)
            if Path(path).name != 'status.json':
                return
            statuses.append(copy.deepcopy(value))
            if value.get('stage') == 'PREFLIGHT_UNRESOLVED':
                source = self.bridge.read().state
                unresolved_observations.append({
                    'enabled': self.store.activation()['enabled'],
                    'closed': source['logical']['closed'],
                    'paused': self.store.status()['docs_read_paused']})
                # Finish only through a separately simulated explicit Stop.
                original_save(runtime/'stop.json', {'requested': self.now})

        gateway = MagicMock()
        with ExitStack() as patches:
            for target, value in (
                ('time.monotonic', lambda: self.now),
                ('time.sleep', wait),
                ('remote_transport.global_desktop.save', save),
                ('remote_transport.global_desktop.Gateway', gateway),
                ('remote_transport.global_desktop.concurrent.futures.ThreadPoolExecutor', lambda **_: pool),
                ('remote_transport.global_desktop.check_clients', lambda _: None),
                ('remote_transport.global_desktop.router._process_identity', lambda _: 'synthetic-process'),
                ('remote_transport.global_desktop.router._load_config', lambda _: cfg),
                ('remote_transport.global_desktop.router._set_google_env', lambda _: None),
                ('examples.google_clients.create_docs_client', lambda: self.docs),
                ('examples.google_clients.create_drive_client', lambda: self.google),
                ('remote_transport.global_google.prepare_session', prepare),
                ('remote_transport.global_pilot.observe_versions', lambda *_: {})):
                patches.enter_context(patch(target, new=value))
            reserve_plan = patches.enter_context(patch.object(pilot, 'reserve_preflight_route',
                return_value={'reservation_id': self.generation, 'route_id': 'b'*32, 'expires': self.now+20}))
            patches.enter_context(patch.object(pilot, 'preflight_route_status', return_value={'ready': True}))
            prepare_plan = patches.enter_context(patch.object(pilot, 'finalize_preflight_route', return_value=plan))
            verify_plan = patches.enter_context(patch.object(pilot, 'verify_preflight'))
            desktop.supervise(runtime)
        self.assertEqual(rejected, ['global_docs_read_recovery_pending'])
        self.assertEqual(pool.submit.call_count, 1)
        self.assertEqual(prepare_plan.call_count, 1)
        self.assertEqual(reserve_plan.call_count, 1)
        verify_plan.assert_not_called()
        self.assertEqual(unresolved_observations, [{'enabled': 1, 'closed': False, 'paused': False}])
        unresolved = next(value for value in statuses if value['stage'] == 'PREFLIGHT_UNRESOLVED')
        self.assertEqual(unresolved['preflight_state'], 'one_attempt_failed_no_replay')
        self.assertIn('PAUSED_DOCS_READ', [value['stage'] for value in statuses])
        self.assertNotIn('FAILED', [value['stage'] for value in statuses])
        self.assertEqual(statuses[-1]['stage'], 'STOPPED')

    def test_http_200_preflight_rejects_failed_incomplete_and_truncated_sse_once(self):
        started = {'type': 'response.created', 'response': {'id': 'resp_fixture', 'status': 'in_progress'}}
        cases = {
            'failed': [started, {'type': 'response.failed', 'response': {
                'status': 'failed', 'error': {'message': 'private provider secret'}}}],
            'incomplete': [started, {'type': 'response.incomplete', 'response': {'status': 'incomplete'}}],
            'truncated': [started],
            'wrong_completed_status': [started, {'type': 'response.completed', 'response': {'status': 'incomplete'}}],
        }
        for label, events in cases.items():
            with self.subTest(stream=label):
                raw = b''.join(b'data: '+json.dumps(event).encode()+b'\n\n' for event in events)
                response = MagicMock(status=200)
                response.isclosed.return_value = False
                # Deliberately split in the middle of JSON and SSE delimiters.
                response.read1.side_effect = [raw[:7], raw[7:31], raw[31:], b'']
                connection = MagicMock()
                connection.getresponse.return_value = response
                with patch.object(desktop.http.client, 'HTTPConnection', return_value=connection) as connect:
                    with self.assertRaises(ProtocolError) as raised:
                        desktop.post_preflight(self.store, {'body': {'input': 'fixture'}, 'identity': identity()})
                self.assertNotIn('private', str(raised.exception))
                self.assertNotIn('secret', str(raised.exception))
                self.assertIn('preflight', str(raised.exception))
                self.assertEqual(connect.call_count, 1)
                self.assertEqual(connection.request.call_count, 1)
                connection.close.assert_called_once()

    def test_http_200_completed_preflight_is_accepted_without_second_request(self):
        event = {'type': 'response.completed', 'response': {'id': 'resp_fixture', 'status': 'completed'}}
        response = MagicMock(status=200)
        response.isclosed.return_value = False
        response.read1.side_effect = [b'data: '+json.dumps(event).encode()+b'\n\n', b'']
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch.object(desktop.http.client, 'HTTPConnection', return_value=connection):
            result = desktop.post_preflight(self.store, {'body': {'input': 'fixture'}, 'identity': identity()})
        self.assertEqual(result['http_status'], 200)
        self.assertEqual(connection.request.call_count, 1)
        connection.close.assert_called_once()

    def test_network_and_rate_limit_read_recovery_preserves_unjoined_activation(self):
        before_writes = self.service.count('batch')
        before_gets = self.service.count('get')
        self.service.read_faults = [socket.timeout('secret'), ProviderHTTPError(429)]
        status = self.bridge.step()
        self.assertEqual(status['state'], 'await_native_join')
        self.assertEqual(self.service.count('get')-before_gets, 3)
        self.assertEqual(self.sleep_durations, [1.0, 2.0])
        self.assertEqual(self.service.count('batch'), before_writes)
        self.assertEqual(self.bridge.read().state, self.initial)
        self.assertTrue(self.store.activation()['enabled'])
        self.assert_sdk_replay_disabled()

    def test_5xx_exhaustion_is_typed_sanitized_and_readonly(self):
        before_writes = self.service.count('batch')
        before_gets = self.service.count('get')
        self.service.read_faults = [ProviderHTTPError(status) for status in (500, 502, 503)]
        with self.assertRaises(ProtocolError) as raised:
            self.bridge.step()
        error = raised.exception
        self.assertEqual(str(error), 'docs_read_transient_exhausted')
        self.assertIs(error.retryable, True)
        self.assertEqual(error.diagnostics['attempts'], 3)
        self.assertEqual(error.diagnostics['http_status'], 503)
        serialized = str(error) + json.dumps(error.diagnostics)
        for secret in ('secret', 'private', 'credential', 'provider.invalid', 'documentId'):
            self.assertNotIn(secret, serialized)
        self.assertEqual(self.service.count('get')-before_gets, 3)
        self.assertEqual(self.service.count('batch'), before_writes)
        self.assertEqual(self.bridge.step()['state'], 'await_native_join')
        self.assertFalse(self.bridge.read().state['logical']['closed'])
        self.assertTrue(self.store.activation()['enabled'])

    def test_permanent_auth_failures_do_not_consume_retry_budget(self):
        for status in (401, 403):
            with self.subTest(status=status):
                before_gets = self.service.count('get')
                before_sleeps = len(self.sleep_durations)
                self.service.read_faults = [ProviderHTTPError(status)]
                with self.assertRaises(ProtocolError) as raised:
                    self.bridge.step()
                self.assertEqual(str(raised.exception), 'docs_read_failed')
                self.assertIs(raised.exception.retryable, False)
                self.assertEqual(raised.exception.diagnostics['http_status'], status)
                self.assertEqual(self.service.count('get')-before_gets, 1)
                self.assertEqual(len(self.sleep_durations), before_sleeps)

    def test_closed_before_join_is_terminal_not_waiting_for_a_controller(self):
        self.bridge.event('close', {'confirm': True})
        snapshot = self.bridge.read()
        self.assertIsNone(snapshot.state['logical']['controller'])
        before = self.service.count('batch')
        self.assertEqual(self.bridge.step()['state'], 'closed')
        self.assertEqual(self.service.count('batch'), before)
        with self.assertRaisesRegex(ProtocolError, 'queue_closed'):
            self.ledger.plan_event(snapshot, 'join', self.root/'native'/'closed-join.json', capacity=2, seconds=100)

    def test_repeated_reads_never_refresh_signed_180_second_heartbeat(self):
        controller = self.join()
        before_writes = self.service.count('batch')
        self.now = controller['heartbeat_at'] + 175
        self.service.read_faults = [ProviderHTTPError(503), ConnectionResetError('secret')]
        self.assertEqual(self.bridge.step()['state'], 'controller_active')
        self.assertEqual(self.now, controller['heartbeat_at'] + 178)
        self.assertTrue(self.store.status()['controller_active'])
        self.assertEqual(self.bridge.read().state['logical']['controller'], controller)
        self.assertEqual(self.service.count('batch'), before_writes)
        self.now = controller['heartbeat_at'] + 180
        self.assertTrue(self.bridge.step()['restart_required'])
        self.assertFalse(self.store.status()['controller_active'])
        self.assertEqual(TIMING['freshness_seconds'], 180)

    def test_read_retry_crossing_freshness_deadline_cannot_revive_controller(self):
        controller = self.join()
        self.now = controller['heartbeat_at'] + 178
        self.service.read_faults = [ProviderHTTPError(503), TimeoutError('secret')]
        self.assertTrue(self.bridge.step()['restart_required'])
        self.assertFalse(self.store.status()['controller_active'])
        self.assertEqual(self.bridge.read().state['logical']['controller'], controller)
        with self.assertRaisesRegex(ProtocolError, 'not_ready'):
            self.store.admission(self.generation, identity(), self.selection)

    def test_read_recovery_never_extends_shorter_controller_lease(self):
        controller = self.join(seconds=60)
        self.now = controller['lease_expires'] - 2
        self.service.read_faults = [TimeoutError('secret'), ProviderHTTPError(504)]
        self.assertTrue(self.bridge.step()['restart_required'])
        self.assertFalse(self.store.status()['controller_active'])
        self.assertEqual(self.bridge.read().state['logical']['controller']['lease_expires'], controller['lease_expires'])

    def test_unobserved_fresh_remote_heartbeat_cannot_erase_local_outage_fence(self):
        controller = self.join()
        self.now = controller['heartbeat_at'] + 100
        # Native writes a genuine new signed heartbeat while the Mac cannot read.
        self.event('heartbeat')
        self.now = controller['heartbeat_at'] + 181
        self.assertTrue(self.bridge.step()['restart_required'])
        self.assertFalse(self.store.status()['controller_active'])
        current = self.bridge.read().state['logical']['controller']
        self.assertEqual(current['heartbeat_at'], controller['heartbeat_at'] + 100)
        self.assertLess(self.now-current['heartbeat_at'], 180)
        # A fresh remote timestamp cannot replace the expired local checkpoint.
        with self.assertRaisesRegex(ProtocolError, 'not_active'):
            self.bridge.sync_heartbeat()

    def test_pause_fences_new_admission_dispatch_and_preflight_with_fresh_heartbeat(self):
        controller = self.join()
        existing = self.store.admission(self.generation, identity(), self.selection)
        # Populate only enough local route state to show the pause gate wins
        # before any dispatch intent or backend request can be recorded.
        with self.store.transaction() as db:
            db.execute("UPDATE routes SET state='ready',controller_epoch=? WHERE id=?",
                       (controller['controller_epoch'], existing['id']))
        token = secrets.token_hex(16)
        self.store.pause_docs_reads(self.generation, token)
        self.assertTrue(self.store.activation()['enabled'])
        self.assertFalse(self.store.status()['controller_active'])
        self.assertTrue(self.store.status()['docs_read_paused'])
        with self.assertRaisesRegex(ProtocolError, 'read_recovery_pending'):
            self.store.admission(self.generation, identity(), self.selection)
        with self.assertRaisesRegex(ProtocolError, 'read_recovery_pending'):
            self.store.request_start(existing['id'], 'a'*64)
        with patch.object(pilot, 'probe', side_effect=lambda _: self.store.status(bound=True)):
            with self.assertRaisesRegex(ProtocolError, 'pilot_live_native_controller_required'):
                pilot._live(self.store)
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 0)
        self.assertEqual(self.bridge.read().state['logical']['controller'], controller)
        self.store.resume_docs_reads(self.generation, token)
        self.assertTrue(self.store.status()['controller_active'])
        self.assertFalse(self.store.status()['docs_read_paused'])

    def test_pause_expiry_latches_liveness_without_erasing_signed_heartbeat(self):
        controller = self.join()
        token = secrets.token_hex(16)
        self.store.pause_docs_reads(self.generation, token)
        self.now = controller['heartbeat_at'] + 180
        with self.assertRaisesRegex(ProtocolError, 'not_active_restart_required'):
            self.store.require_read_recovery_current()
        self.store.resume_docs_reads(self.generation, token)
        self.assertFalse(self.store.status()['controller_active'])
        with self.assertRaisesRegex(ProtocolError, 'not_active'):
            self.bridge.sync_heartbeat()
        self.assertEqual(self.bridge.read().state['logical']['controller'], controller)

    def test_lost_native_write_reply_reconciles_through_retried_get_without_replay(self):
        self.join()
        self.now += 25
        path, out = self.plan('heartbeat')
        self.google.lose = True
        before_writes = self.service.count('batch')
        with self.assertRaisesRegex(ProtocolError, '^docs_write_outcome_unknown$'):
            self.docs.batch_update_document(**out['tool_arguments'])
        self.assertEqual(self.service.count('batch')-before_writes, 1)
        self.service.read_faults = [TimeoutError('secret'), ProviderHTTPError(503)]
        readback = self.docs.get_document(self.initial['document_id'])
        result = self.ledger.verify_plan(path, None, readback)
        self.assertTrue(result['reconciled_from_event'])
        self.assertFalse(result['native_retry_allowed'])
        self.assertEqual(self.service.count('batch')-before_writes, 1)
        self.assert_sdk_replay_disabled()

    def test_exhausted_read_after_unknown_native_write_keeps_one_attempt_fence(self):
        self.join()
        self.now += 25
        path, out = self.plan('heartbeat')
        self.google.lose = True
        before_writes = self.service.count('batch')
        with self.assertRaisesRegex(ProtocolError, 'write_outcome_unknown'):
            self.docs.batch_update_document(**out['tool_arguments'])
        self.service.read_faults = [TimeoutError('secret') for _ in range(3)]
        with self.assertRaisesRegex(ProtocolError, 'read_transient_exhausted'):
            self.docs.get_document(self.initial['document_id'])
        with self.assertRaisesRegex(ProtocolError, 'unresolved'):
            self.plan('heartbeat')
        result = self.ledger.verify_plan(path, None, self.docs.get_document(self.initial['document_id']))
        self.assertTrue(result['verified'])
        self.assertEqual(self.service.count('batch')-before_writes, 1)

    def test_mac_cas_with_failed_readback_stays_unknown_without_rewrite(self):
        # The CAS is applied once; only its verification read exhausts retries.
        def fail_readback():
            self.service.after_write = None
            self.service.read_faults = [TimeoutError('secret') for _ in range(3)]
        self.service.after_write = fail_readback
        before_writes = self.service.count('batch')
        with self.assertRaisesRegex(ProtocolError, 'global_mac_cas_unknown_no_retry'):
            self.bridge.event('close', {'confirm': True})
        self.assertEqual(self.service.count('batch')-before_writes, 1)
        with self.assertRaisesRegex(ProtocolError, 'queue_closed'):
            self.bridge.event('close', {'confirm': True})
        self.assertEqual(self.service.count('batch')-before_writes, 1)
        record = json.loads((self.bridge.root/'queue-close-controller.json').read_text())
        self.assertEqual(record['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
