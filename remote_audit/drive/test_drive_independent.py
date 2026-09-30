"""Independent adversarial tests. No network, credentials or native admissions."""
import concurrent.futures
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve()
REPO = next(p for p in HERE.parents if (p / 'remote_transport' / 'session.py').is_file())
sys.path.insert(0, str(REPO))
from remote_transport import Object, ProtocolError, deployment, Journal, Controller, Worker, LocalFSBackend
from remote_transport.backend import GoogleDriveBackend, Capabilities, AlreadyExists, filename


class DriveDouble:
    capabilities = Capabilities(complete_listing=True, create_by_id=True)
    def __init__(self):
        self.media = {}
        self.pages = {}
        self.list_calls = []
        self.create_calls = []
        self.next_id = 0
        self.read_override = None
        self.commit_then_raise = False
    def generate_id(self):
        self.next_id += 1
        return 'remote_' + str(self.next_id)
    def create_bytes(self, folder, name, raw, reservation):
        ident = reservation or self.generate_id()
        self.create_calls.append((folder, name, raw, reservation))
        if ident in self.media:
            raise AlreadyExists()
        self.media[ident] = raw
        if self.commit_then_raise:
            self.commit_then_raise = False
            raise TimeoutError('server committed; client lost response')
        return ident
    def get_bytes(self, ident, limit):
        return self.read_override if self.read_override is not None else self.media[ident]
    def list_page(self, folder, token, size):
        self.list_calls.append(token)
        result = self.pages[token]
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)
    def item(self, ident, obj, folder='folder'):
        self.media[ident] = obj.raw
        return {'id': ident, 'name': filename(obj), 'parents': [folder], 'trashed': False}


class IndependentBackendTests(unittest.TestCase):
    def setUp(self):
        self.pin = deployment('audit_session', 'synthetic/native/task', now=1000)
        self.api = DriveDouble()
        self.backend = GoogleDriveBackend(self.api, 'folder')
    def test_strict_mode_gates_unsupported_connector(self):
        self.api.capabilities = Capabilities(complete_listing=True, create_by_id=False)
        with self.assertRaisesRegex(ProtocolError, 'create_by_id_capability_required'):
            GoogleDriveBackend(self.api, 'folder')
        GoogleDriveBackend(self.api, 'folder', mode='duplicate_tolerant')
    def test_lost_create_reply_retries_same_physical_id(self):
        reserved = self.backend.reserve()
        self.api.commit_then_raise = True
        with self.assertRaises(TimeoutError):
            self.backend.publish(self.pin, reserved)
        self.assertEqual(self.backend.publish(self.pin, reserved), reserved)
        self.assertEqual(list(self.api.media), [reserved])
        self.assertEqual({c[3] for c in self.api.create_calls}, {reserved})
    def test_existing_id_wrong_bytes_never_counts_as_success(self):
        reserved = self.backend.reserve()
        self.api.media[reserved] = b'wrong existing content'
        with self.assertRaisesRegex(ProtocolError, 'readback_mismatch'):
            self.backend.publish(self.pin, reserved)
    def test_empty_page_with_continuation_is_followed(self):
        item = self.api.item('id1', self.pin)
        self.api.pages = {None: {'files': [], 'nextPageToken': 'p2'}, 'p2': {'files': [item]}}
        objects = self.backend.scan(self.pin.body['identity']['deployment_id'])
        self.assertEqual([o.oid for o in objects], [self.pin.oid])
        self.assertEqual(self.api.list_calls, [None, 'p2'])
    def test_cursor_fault_does_not_reuse_partial_cursor_next_scan(self):
        item = self.api.item('id1', self.pin)
        self.api.pages = {None: {'files': [item], 'nextPageToken': 'stale'}, 'stale': ProtocolError('invalid_page_token')}
        with self.assertRaises(ProtocolError):
            self.backend.scan(self.pin.body['identity']['deployment_id'])
        self.api.pages = {None: {'files': [item]}}
        self.assertEqual(len(self.backend.scan(self.pin.body['identity']['deployment_id'])), 1)
        self.assertEqual(self.api.list_calls, [None, 'stale', None])
    def test_partial_search_and_missing_media_fail_closed(self):
        item = self.api.item('id1', self.pin)
        self.api.pages = {None: {'files': [item], 'incompleteSearch': True}}
        with self.assertRaisesRegex(ProtocolError, 'incomplete_search'):
            self.backend.scan(self.pin.body['identity']['deployment_id'])
        self.api.pages[None]['incompleteSearch'] = False
        del self.api.media['id1']
        with self.assertRaises(KeyError):
            self.backend.scan(self.pin.body['identity']['deployment_id'])
    def test_repeated_cursor_is_bounded(self):
        self.api.pages = {None: {'files': [], 'nextPageToken': 'loop'}, 'loop': {'files': [], 'nextPageToken': 'loop'}}
        with self.assertRaisesRegex(ProtocolError, 'invalid_page_token'):
            self.backend.scan(self.pin.body['identity']['deployment_id'])
        self.assertEqual(len(self.api.list_calls), 2)
    def test_wrong_folder_or_trashed_item_rejected(self):
        item = self.api.item('id1', self.pin, 'other_folder')
        self.api.pages = {None: {'files': [item]}}
        with self.assertRaisesRegex(ProtocolError, 'unexpected_file_scope'):
            self.backend.scan(self.pin.body['identity']['deployment_id'])
        item['parents'] = ['folder']; item['trashed'] = True
        with self.assertRaisesRegex(ProtocolError, 'unexpected_file_scope'):
            self.backend.scan(self.pin.body['identity']['deployment_id'])
    def test_name_does_not_override_hash_verified_bytes(self):
        item = self.api.item('id1', self.pin)
        obj = json.loads(self.pin.raw)
        obj['body']['identity']['worker_id'] = 'someone_else'
        self.api.media['id1'] = json.dumps(obj).encode()
        self.api.pages = {None: {'files': [item]}}
        with self.assertRaisesRegex(ProtocolError, 'hash_mismatch'):
            self.backend.scan(self.pin.body['identity']['deployment_id'])
    def test_duplicate_json_keys_rejected(self):
        raw = self.pin.raw.replace(b'"attempt":0', b'"attempt":0,"attempt":0', 1)
        with self.assertRaisesRegex(ProtocolError, 'duplicate_json_key'):
            Object.parse(raw)
    def test_conflict_slot_excludes_content_hash(self):
        a = Object.make(self.pin.body['identity'], 'request', 1, self.pin.oid, {'text': 'a'})
        b = Object.make(self.pin.body['identity'], 'request', 1, self.pin.oid, {'text': 'b'})
        self.assertNotEqual(a.oid, b.oid)
        self.assertEqual(a.slot, b.slot)


class IndependentProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pin = deployment('audit_session', 'synthetic/native/task')
        self.backend = LocalFSBackend(self.root / 'objects', create=True)
        self.cj = Journal.provision(self.root / 'controller', self.pin, 'controller')
        self.wj = Journal.provision(self.root / 'worker', self.pin, 'worker')
        self.controller = Controller(self.cj, self.backend)
        self.worker = Worker(self.wj, self.backend)
        self.controller.publish_deployment()
    def tearDown(self):
        self.tmp.cleanup()
    def test_crash_after_permit_never_returns_second_permit(self):
        self.controller.submit('one', 'key')
        permit = self.worker.start_next()
        self.assertIsNotNone(permit)
        resumed = Worker(Journal(self.root / 'worker', self.pin, 'worker'), self.backend)
        with self.assertRaisesRegex(ProtocolError, 'execution_outcome_unknown'):
            resumed.start_next()
    def test_missing_journal_refuses_resume(self):
        (self.root / 'worker' / 'state.json').unlink()
        with self.assertRaises((ProtocolError, FileNotFoundError)):
            Journal(self.root / 'worker', self.pin, 'worker')
    def test_identity_mismatched_journal_refuses_resume(self):
        another = deployment('audit_session', 'synthetic/native/task')
        with self.assertRaisesRegex(ProtocolError, 'journal_pin_mismatch'):
            Journal(self.root / 'worker', another, 'worker')
    def test_lost_execution_index_cannot_replay_started_object(self):
        self.controller.submit('one', 'key')
        first = self.worker.start_next()
        path = self.root / 'worker' / 'state.json'
        state = json.loads(path.read_text())
        state['executions'] = {}
        path.write_text(json.dumps(state))
        with self.assertRaises(ProtocolError):
            resumed = Worker(Journal(self.root / 'worker', self.pin, 'worker'), self.backend)
            second = resumed.start_next()
            self.assertEqual(first['dispatch_id'], second['dispatch_id'])
    def test_second_request_needs_explicit_delivery_receipt(self):
        request_id = self.controller.submit('one', 'key1')
        permit = self.worker.start_next()
        result_id = self.worker.complete(permit, 'answer')
        self.assertEqual(self.controller.result(request_id).oid, result_id)
        with self.assertRaisesRegex(ProtocolError, 'previous_delivery_unconfirmed'):
            self.controller.submit('two', 'key2')
        self.controller.record_delivery(request_id, result_id, 'observed synthetic local response')
        self.controller.submit('two', 'key2')
        self.assertNotEqual(permit['dispatch_id'], self.worker.start_next()['dispatch_id'])
    def test_remote_started_with_rolled_back_local_state_cannot_get_permit(self):
        path = self.root / 'worker' / 'state.json'
        before = path.read_bytes()
        self.controller.submit('one', 'key')
        self.assertIsNotNone(self.worker.start_next())
        path.write_bytes(before)
        with self.assertRaises(ProtocolError):
            resumed = Worker(Journal(self.root / 'worker', self.pin, 'worker'), self.backend)
            resumed.start_next()
    def test_lost_started_upload_response_never_returns_permit_later(self):
        self.controller.submit('one', 'key')
        original = self.backend.publish
        def uncertain(obj, reservation=None):
            result = original(obj, reservation)
            if obj.body['kind'] == 'started':
                raise TimeoutError('committed started, response lost')
            return result
        with patch.object(self.backend, 'publish', side_effect=uncertain):
            with self.assertRaises(TimeoutError):
                self.worker.start_next()
        with self.assertRaisesRegex(ProtocolError, 'execution_outcome_unknown'):
            self.worker.start_next()
    def test_result_waits_until_started_and_claim_links_visible(self):
        request_id = self.controller.submit('one', 'key')
        permit = self.worker.start_next()
        result_id = self.worker.complete(permit, 'answer')
        original = self.backend.scan
        def incomplete(dep):
            return [o for o in original(dep) if o.body['kind'] not in {'started', 'claim'}]
        with patch.object(self.backend, 'scan', side_effect=incomplete):
            self.assertIsNone(self.controller.result(request_id))
            with self.assertRaisesRegex(ProtocolError, 'unverified_result'):
                self.controller.record_delivery(request_id, result_id, 'not actually complete')
        self.assertEqual(self.controller.result(request_id).oid, result_id)
    def test_later_listing_omission_does_not_erase_verified_history(self):
        request_id = self.controller.submit('one', 'key')
        permit = self.worker.start_next()
        result_id = self.worker.complete(permit, 'answer')
        self.assertEqual(self.controller.result(request_id).oid, result_id)
        with patch.object(self.backend, 'scan', return_value=[]):
            self.assertEqual(self.controller.result(request_id).oid, result_id)
    def test_conflicting_request_slot_permanently_blocks_session(self):
        self.controller.submit('one', 'key')
        bad = Object.make(self.pin.body['identity'], 'request', 1, self.pin.oid, {'text': 'different'})
        self.backend.publish(bad)
        with self.assertRaisesRegex(ProtocolError, 'semantic_slot_conflict'):
            self.worker.start_next()
        with self.assertRaisesRegex(ProtocolError, 'session_blocked'):
            self.worker.start_next()
    def test_new_remote_generation_never_selects_itself(self):
        ident = copy.deepcopy(self.pin.body['identity']); ident['generation'] += 1
        bad = Object.make(ident, 'request', 1, self.pin.oid, {'text': 'wrong generation'})
        self.backend.publish(bad)
        with self.assertRaisesRegex(ProtocolError, 'object_pin_mismatch'):
            self.worker.start_next()
    def test_duplicate_same_object_observation_does_not_duplicate_permit(self):
        self.controller.submit('one', 'key')
        original = self.backend.scan
        with patch.object(self.backend, 'scan', side_effect=lambda dep: original(dep) * 3):
            self.assertIsNotNone(self.worker.start_next())
            with self.assertRaisesRegex(ProtocolError, 'execution_outcome_unknown'):
                self.worker.start_next()
    def test_crash_after_local_result_save_only_recovers_publication(self):
        request_id = self.controller.submit('one', 'key')
        permit = self.worker.start_next()
        original = self.backend.publish
        def interrupted(obj, reservation=None):
            if obj.body['kind'] == 'result':
                raise TimeoutError('result upload outcome unknown')
            return original(obj, reservation)
        with patch.object(self.backend, 'publish', side_effect=interrupted):
            with self.assertRaises(TimeoutError):
                self.worker.complete(permit, 'answer')
        resumed = Worker(Journal(self.root / 'worker', self.pin, 'worker'), self.backend)
        self.assertIsNone(resumed.start_next())
        self.assertIsNone(self.controller.result(request_id))
        state = json.loads((self.root / 'worker' / 'state.json').read_text())
        result_id = state['executions']['1']['result']
        resumed.recover_publication(result_id)
        self.assertEqual(self.controller.result(request_id).oid, result_id)
    def test_wrong_native_task_completion_is_rejected(self):
        self.controller.submit('one', 'key')
        permit = self.worker.start_next(); permit['native_task_id'] = 'someone_else'
        with self.assertRaisesRegex(ProtocolError, 'native_identity_mismatch'):
            self.worker.complete(permit, 'answer')
    def test_same_journal_concurrent_workers_issue_one_permit(self):
        self.controller.submit('one', 'key')
        workers = [Worker(Journal(self.root / 'worker', self.pin, 'worker'), self.backend) for _ in range(6)]
        def attempt(worker):
            try:
                return worker.start_next()
            except ProtocolError as exc:
                return str(exc)
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(attempt, workers))
        permits = [value for value in outcomes if isinstance(value, dict)]
        self.assertEqual(len(permits), 1)
        self.assertTrue(all(isinstance(value, dict) or value == 'execution_outcome_unknown' for value in outcomes))
    def test_partial_scan_never_commits_first_page_objects(self):
        self.controller.submit('one', 'key')
        before = (self.root / 'worker' / 'state.json').read_bytes()
        with patch.object(self.backend, 'scan', side_effect=ProtocolError('incomplete_search')):
            with self.assertRaisesRegex(ProtocolError, 'incomplete_search'):
                self.worker.reconcile()
        self.assertEqual((self.root / 'worker' / 'state.json').read_bytes(), before)
    def test_idempotent_submit_does_not_retry_ambiguous_upload(self):
        original = self.backend.publish
        calls = []
        def interrupted(obj, reservation=None):
            calls.append(obj.oid)
            result = original(obj, reservation)
            if obj.body['kind'] == 'request':
                raise TimeoutError('request upload response lost')
            return result
        with patch.object(self.backend, 'publish', side_effect=interrupted):
            with self.assertRaises(TimeoutError):
                self.controller.submit('one', 'key')
            request_id = self.controller.submit('one', 'key')
        self.assertEqual(calls, [request_id])
        self.assertIsNotNone(self.worker.start_next())
    def test_result_change_after_completion_rejected(self):
        self.controller.submit('one', 'key')
        permit = self.worker.start_next()
        result = self.worker.complete(permit, 'first answer')
        self.assertEqual(self.worker.complete(permit, 'first answer'), result)
        with self.assertRaisesRegex(ProtocolError, 'semantic_slot_conflict|result_conflict'):
            self.worker.complete(permit, 'different answer')
    def test_controller_lost_request_index_cannot_republish_new_request(self):
        self.controller.submit('one', 'key')
        path = self.root / 'controller' / 'state.json'
        state = json.loads(path.read_text()); state['requests'] = {}
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ProtocolError, 'request_index_corrupt'):
            Journal(self.root / 'controller', self.pin, 'controller')
    def test_remote_request_two_waits_for_missing_request_one(self):
        fake_prev = '0' * 64
        request = Object.make(self.pin.body['identity'], 'request', 2, self.pin.oid,
                              {'text': 'later visible first'}, {'previous_receipt': fake_prev})
        self.backend.publish(request)
        self.assertIsNone(self.worker.start_next())
    def test_pinned_request_budget_is_enforced_by_worker(self):
        small = Object.make(self.pin.body['identity'], 'deployment', 0, None,
                            dict(self.pin.body['payload'], max_requests=1))
        cj = Journal.provision(self.root / 'small_controller', small, 'controller')
        wj = Journal.provision(self.root / 'small_worker', small, 'worker')
        store = LocalFSBackend(self.root / 'small_objects', create=True)
        controller, worker = Controller(cj, store), Worker(wj, store)
        controller.publish_deployment()
        first = controller.submit('only authorized ordinal', 'key1')
        permit = worker.start_next()
        result = worker.complete(permit, 'answer')
        receipt = controller.record_delivery(first, result, 'synthetic response delivered')
        extra = Object.make(small.body['identity'], 'request', 2, small.oid,
                            {'text': 'exceeds pinned budget'}, {'previous_receipt': receipt})
        store.publish(extra)
        with self.assertRaises(ProtocolError):
            worker.start_next()
    def test_idempotency_key_cannot_change_payload(self):
        oid = self.controller.submit('one', 'key')
        self.assertEqual(oid, self.controller.submit('one', 'key'))
        with self.assertRaisesRegex(ProtocolError, 'idempotency_payload_conflict'):
            self.controller.submit('changed', 'key')


if __name__ == '__main__':
    unittest.main(verbosity=2)
