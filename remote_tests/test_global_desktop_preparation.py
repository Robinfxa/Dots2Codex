"""Offline Desktop overlap and owner-controlled pause/resume/drain barriers."""
import copy
import threading
import unittest
from unittest.mock import patch

from examples.google_clients import DocsReadError
from remote_tests import test_global_preparation_fast as prep_fixture
from remote_transport.global_desktop import save
from remote_transport.model import ProtocolError


class DesktopPreparationTests(unittest.TestCase):
    setUp = prep_fixture.PreparationFastTests.setUp
    advance = prep_fixture.PreparationFastTests.advance
    plan = prep_fixture.PreparationFastTests.plan
    event = prep_fixture.PreparationFastTests.event
    join = prep_fixture.PreparationFastTests.join
    recovering_docs = prep_fixture.PreparationFastTests.recovering_docs
    route = prep_fixture.PreparationFastTests.route
    active = prep_fixture.PreparationFastTests.active
    reservation = prep_fixture.PreparationFastTests.reservation

    def assert_drained(self, proxy):
        self.assertIsNone(proxy.preparation)
        self.assertFalse(any(t.name.startswith('global-prepare-drive') for t in threading.enumerate()))

    def run_pause(self, *, on_pause=None, reconciliation_fault=False, unknown=False, bootstrap_seconds=None):
        """Fail owner GET while bootstrap POST is known to be in flight."""
        if bootstrap_seconds is not None: self.bridge.bootstrap_seconds = bootstrap_seconds
        rid = self.route()
        original_create = self.google.create_document_once
        original_get = self.docs.get_document
        original_upload = self.google.create_bytes
        original_batch = self.google.batch_update_document
        owner = threading.get_ident()
        bootstrap_inflight = threading.Event()
        created, uploads, observed, reads = [], [], [], []
        faulted = False; queue_faulted = False

        def paused(report):
            # Pause acknowledgement is after the in-flight result and its receipt
            # are durable. No queued upload has started and admission is fenced.
            self.assertTrue(self.store.status()['docs_read_paused'])
            self.assertFalse(uploads)
            self.assertEqual(self.active()['control_document_id'], created[0])
            if not unknown:
                self.assertEqual(self.active()['bootstrap_document_id'], created[1])
                self.assertEqual(self.reservation('create-bootstrap')['document_id'], created[1])
            observed.append(copy.deepcopy(report))
            if on_pause is not None: on_pause(proxy)

        proxy = self.recovering_docs(on_pause=paused)

        def create(folder, title):
            self.assertNotEqual(threading.get_ident(), owner)
            self.assertFalse(self.store.status()['docs_read_paused'])
            if ' Bootstrap ' in title:
                bootstrap_inflight.set()
                self.assertTrue(proxy.preparation.pause_requested.wait(3), 'owner never requested pause')
                if unknown: raise TimeoutError('synthetic ambiguous create')
            value = original_create(folder, title)
            created.append(value)
            return value

        def get(did, **kwargs):
            nonlocal faulted, queue_faulted
            self.assertEqual(threading.get_ident(), owner)
            reads.append(did)
            if did != self.initial['document_id'] and not faulted:
                faulted = True
                self.assertTrue(bootstrap_inflight.wait(3), 'Drive never overlapped owner read')
                raise DocsReadError(category='network', retryable=True, attempts=3)
            if reconciliation_fault and proxy.paused and did == self.initial['document_id'] and not queue_faulted:
                queue_faulted = True
                raise DocsReadError(category='network', retryable=True, attempts=3)
            return original_get(did, **kwargs)

        def upload(*args):
            self.assertFalse(self.store.status()['docs_read_paused'])
            self.assertEqual(self.resume_reports, [True])
            uploads.append(args)
            return original_upload(*args)

        def batch(*args, **kwargs):
            self.assertEqual(threading.get_ident(), owner)
            self.assertFalse(self.store.status()['docs_read_paused'])
            return original_batch(*args, **kwargs)

        try:
            with patch.object(self.google, 'create_document_once', side_effect=create), \
                 patch.object(self.docs, 'get_document', side_effect=get), \
                 patch.object(self.google, 'create_bytes', side_effect=upload), \
                 patch.object(self.google, 'batch_update_document', side_effect=batch):
                result = self.bridge.prepare_child(rid)
        except BaseException:
            self.assert_drained(proxy)
            raise
        self.assert_drained(proxy)
        return rid, proxy, result, created, uploads, observed, reads

    def test_pause_drains_bootstrap_then_authenticates_before_queued_write(self):
        rid, proxy, result, created, uploads, observed, reads = self.run_pause()
        self.assertEqual(len(created), 2)
        self.assertEqual(len(uploads), 1)
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.resume_reports, [True])
        self.assertFalse(proxy.paused)
        self.assertEqual(self.active()['stage'], 'WAITING_FOR_WORKER')
        self.assertIn(rid, result.state['logical']['demands'])
        self.assertEqual(self.store.route(rid)['state'], 'pending')
        self.assertEqual(len(self.google.docs), 3)
        self.assertEqual(self.reservation('create-forward-probe')['status'], 'verified')
        self.assertEqual(self.reservation('create-forward-probe')['returned_file_id'], uploads[0][3])

    def test_reconciliation_get_can_pause_again_without_releasing_worker(self):
        _, proxy, _, created, uploads, observed, _ = self.run_pause(reconciliation_fault=True)
        self.assertEqual(len(observed), 2)
        self.assertEqual(len(created), 2)
        self.assertEqual(len(uploads), 1)
        self.assertFalse(proxy.paused)
        self.assertEqual(self.resume_reports, [True])

    def test_tampered_reconciliation_cancels_waiter_and_keeps_returned_ids(self):
        def tamper(_):
            record = self.google.docs[self.initial['document_id']]
            record[0] = record[0].replace('"closed":false', '"closed":true')
            record[1] += 1
        with self.assertRaisesRegex(ProtocolError, 'projection'):
            self.run_pause(on_pause=tamper)
        self.assertTrue(self.store.status()['docs_read_paused'])
        self.assertEqual(self.resume_reports, [])
        self.assertEqual(self.active()['stage'], 'PREPARING')
        self.assertIsNotNone(self.active()['control_document_id'])
        self.assertIsNotNone(self.active()['bootstrap_document_id'])
        self.assertFalse(self.google.files)
        with self.assertRaises(ProtocolError): self.bridge.prepare_child(self.route_id)
        self.assertEqual(len(self.google.docs), 3)

    def test_explicit_stop_while_paused_cancels_waiter_without_new_write(self):
        def stop(proxy): save(proxy.runtime/'stop.json', {'stopped': True})
        with self.assertRaisesRegex(ProtocolError, 'stop_requested|cancelled'):
            self.run_pause(on_pause=stop)
        self.assertEqual(self.resume_reports, [])
        self.assertFalse(self.google.files)
        self.assertEqual(len(self.google.docs), 3)
        self.assertEqual(self.active()['stage'], 'PREPARING')

    def test_setup_deadline_while_paused_cannot_resume_or_advance(self):
        def expire(proxy):
            proxy.setup_expires = self.now
            proxy.setup_deadline = self.now
        with self.assertRaisesRegex(ProtocolError, 'setup_expired|cancelled'):
            self.run_pause(on_pause=expire)
        self.assertEqual(self.resume_reports, [])
        self.assertFalse(self.google.files)
        self.assertEqual(self.active()['stage'], 'PREPARING')

    def test_disabled_activation_while_paused_cannot_resume_or_advance(self):
        def disable(_): self.store.disable(self.generation)
        with self.assertRaisesRegex(ProtocolError, 'activation_disabled|cancelled'):
            self.run_pause(on_pause=disable)
        self.assertEqual(self.resume_reports, [])
        self.assertFalse(self.google.files)

    def test_stop_intent_change_while_paused_cancels_waiter(self):
        def stop(proxy):
            save(proxy.runtime.parent.parent/'stop-intent.json', {'request_id': 'new-stop'})
        with self.assertRaisesRegex(ProtocolError, 'cancelled_by_stop|cancelled'):
            self.run_pause(on_pause=stop)
        self.assertEqual(self.resume_reports, [])
        self.assertFalse(self.google.files)

    def test_activation_deadline_while_paused_cannot_resume(self):
        def expire(proxy): self.now = proxy.expires
        with self.assertRaisesRegex(ProtocolError, 'recovery_expired|preparation_expired|cancelled'):
            self.run_pause(on_pause=expire)
        self.assertEqual(self.resume_reports, [])
        self.assertFalse(self.google.files)

    def test_controller_freshness_expiry_while_paused_cannot_resume(self):
        def expire(_): self.now += 900
        with self.assertRaisesRegex(ProtocolError, 'not_active|preparation_expired|cancelled'):
            self.run_pause(on_pause=expire, bootstrap_seconds=1800)
        self.assertEqual(self.resume_reports, [])
        self.assertFalse(self.google.files)
        self.assertFalse(self.store.status()['controller_active'])

    def test_clock_rollback_while_paused_cannot_resume(self):
        def rollback(_): self.now -= 1
        with self.assertRaisesRegex(ProtocolError, 'clock_rollback|cancelled'):
            self.run_pause(on_pause=rollback)
        self.assertEqual(self.resume_reports, [])
        self.assertFalse(self.google.files)

    def test_permanent_child_get_failure_drains_inflight_without_replay(self):
        proxy = self.recovering_docs()
        prep_fixture.PreparationFastTests.test_docs_failure_drains_inflight_create_and_preserves_ids(self)
        self.assert_drained(proxy)
        self.assertEqual(self.resume_reports, [])

    def test_initial_source_get_recovers_before_starting_any_resource(self):
        rid = self.route(); proxy = self.recovering_docs()
        self.service.read_faults = [TimeoutError('synthetic') for _ in range(3)]
        result = self.bridge.prepare_child(rid)
        self.assert_drained(proxy)
        self.assertIn(rid, result.state['logical']['demands'])
        self.assertEqual(len(self.pause_reports), 1)
        self.assertEqual(self.resume_reports, [True])
        self.assertFalse(proxy.paused)
        self.assertEqual(len(self.google.docs), 3)
        self.assertEqual(len(self.google.files), 1)

    def test_unknown_inflight_create_is_burned_and_aborts_owner_recovery(self):
        with self.assertRaisesRegex(ProtocolError, 'cancelled'):
            self.run_pause(unknown=True)
        self.assertEqual(self.resume_reports, [])
        self.assertEqual(self.reservation('create-bootstrap')['status'], 'unknown')
        self.assertIsNone(self.active()['bootstrap_document_id'])
        self.assertIsNotNone(self.active()['control_document_id'])
        self.assertEqual(len(self.google.docs), 2)
        self.assertFalse(self.google.files)

    def test_pause_during_probe_upload_records_id_before_acknowledgment(self):
        rid = self.route(); owner = threading.get_ident()
        uploading = threading.Event(); original_upload = self.google.create_bytes
        original_get = self.docs.get_document; original_metadata = self.google.get_metadata
        faulted = False; metadata_calls = []
        def paused(_):
            self.assertFalse(metadata_calls)
            receipt = self.reservation('create-forward-probe')
            self.assertEqual(receipt['status'], 'returned')
            self.assertEqual(receipt['returned_file_id'], receipt['arguments']['file_id'])
            self.assertIn(receipt['returned_file_id'], self.google.files)
        proxy = self.recovering_docs(on_pause=paused)
        def upload(*args):
            self.assertNotEqual(threading.get_ident(), owner)
            uploading.set()
            self.assertTrue(proxy.preparation.pause_requested.wait(3))
            return original_upload(*args)
        def get(did, **kwargs):
            nonlocal faulted
            if did != self.initial['document_id'] and not faulted:
                faulted = True
                self.assertTrue(uploading.wait(3))
                raise DocsReadError(category='network', retryable=True, attempts=3)
            return original_get(did, **kwargs)
        def metadata(*args):
            self.assertFalse(self.store.status()['docs_read_paused'])
            self.assertEqual(self.resume_reports, [True])
            metadata_calls.append(args)
            return original_metadata(*args)
        with patch.object(self.google, 'create_bytes', side_effect=upload), \
             patch.object(self.docs, 'get_document', side_effect=get), \
             patch.object(self.google, 'get_metadata', side_effect=metadata):
            self.bridge.prepare_child(rid)
        self.assert_drained(proxy)
        self.assertEqual(len(metadata_calls), 1)
        self.assertEqual(self.reservation('create-forward-probe')['status'], 'verified')

    def test_pause_before_bootstrap_write_reconciles_before_that_write(self):
        rid = self.route(); original_get = self.docs.get_document
        original_batch = self.google.batch_update_document
        counts = {}; written = []
        proxy = self.recovering_docs()
        def get(did, **kwargs):
            counts[did] = counts.get(did, 0)+1
            if self.active().get('bootstrap_document_id') == did and counts[did] == 2:
                raise DocsReadError(category='network', retryable=True, attempts=3)
            return original_get(did, **kwargs)
        def batch(*args, **kwargs):
            self.assertFalse(self.store.status()['docs_read_paused'])
            written.append(args)
            return original_batch(*args, **kwargs)
        # Source GET precedes active.json creation.
        def safe_get(did, **kwargs):
            if not (self.runtime/'active.json').exists(): return original_get(did, **kwargs)
            return get(did, **kwargs)
        with patch.object(self.docs, 'get_document', side_effect=safe_get), \
             patch.object(self.google, 'batch_update_document', side_effect=batch):
            self.bridge.prepare_child(rid)
        self.assert_drained(proxy)
        self.assertEqual(len(written), 2)
        self.assertEqual(len(self.pause_reports), 1)
        self.assertEqual(self.resume_reports, [True])
        self.assertFalse(proxy.paused)

    def test_child_preparation_deadline_applies_to_late_blank_resource_read(self):
        rid = self.route(); original_get = self.docs.get_document
        before_writes = self.service.count('batch'); counts = {}
        def paused(_): self.now += self.bridge.bootstrap_seconds
        proxy = self.recovering_docs(on_pause=paused)
        def get(did, **kwargs):
            if (self.runtime/'active.json').exists():
                counts[did] = counts.get(did, 0)+1
                if self.active().get('bootstrap_document_id') == did and counts[did] == 1:
                    raise DocsReadError(category='network', retryable=True, attempts=3)
            return original_get(did, **kwargs)
        with patch.object(self.docs, 'get_document', side_effect=get):
            with self.assertRaisesRegex(ProtocolError, 'preparation_expired'):
                self.bridge.prepare_child(rid)
        self.assert_drained(proxy)
        self.assertEqual(self.service.count('batch'), before_writes)
        self.assertEqual(self.active()['stage'], 'PREPARING')
        self.assertEqual(self.resume_reports, [])
        self.assertEqual(len(self.google.docs), 3)
        self.assertLessEqual(len(self.google.files), 1)
        self.assertFalse((self.runtime/'initialize-bootstrap.json').exists())


    def test_expired_initialization_readback_burns_one_write_without_demand(self):
        rid = self.route(); original_get = self.docs.get_document
        before_writes = self.service.count('batch'); counts = {}
        def paused(_): self.now += self.bridge.bootstrap_seconds
        proxy = self.recovering_docs(on_pause=paused)
        def get(did, **kwargs):
            if (self.runtime/'active.json').exists():
                counts[did] = counts.get(did, 0)+1
                if self.active().get('bootstrap_document_id') == did and counts[did] == 2:
                    raise DocsReadError(category='network', retryable=True, attempts=3)
            return original_get(did, **kwargs)
        with patch.object(self.docs, 'get_document', side_effect=get):
            with self.assertRaisesRegex(RuntimeError, 'initialization_outcome_unknown'):
                self.bridge.prepare_child(rid)
        self.assert_drained(proxy)
        self.assertEqual(self.service.count('batch'), before_writes+1)
        self.assertEqual(self.reservation('initialize-bootstrap')['status'], 'unknown')
        self.assertEqual(self.active()['stage'], 'PREPARING')
        self.assertNotIn(rid, self.bridge.facades)
        self.assertEqual(self.resume_reports, [])

    def test_active_facade_keeps_warm_preparation_on_owner_serial_path(self):
        rid = self.route(); proxy = self.recovering_docs()
        owner = threading.get_ident(); original = self.google.create_document_once
        # A live facade may already access the same ports. No new worker is
        # introduced, and baseline serial behavior is kept for that warm route.
        from unittest.mock import Mock
        self.bridge.facades['existing-route'] = Mock()
        threads = []
        def create(*args):
            threads.append(threading.get_ident())
            self.assertIsNone(proxy.preparation)
            return original(*args)
        with patch.object(self.google, 'create_document_once', side_effect=create):
            self.bridge.prepare_child(rid)
        self.assertEqual(threads, [owner, owner])
        self.assert_drained(proxy)
        self.assertEqual(self.active()['stage'], 'WAITING_FOR_WORKER')

    def test_desktop_supervisor_explicitly_enables_independent_port_overlap(self):
        # Keep the explicit opt-in adjacent to independently constructed clients.
        import inspect
        from remote_transport.global_desktop import supervise
        source = inspect.getsource(supervise)
        self.assertIn('RecoveringDocs(create_docs_client()', source)
        self.assertIn("read_port, create_drive_client(), cfg['folder_id']", source)
        self.assertIn('parallel_preparation=True', source)


if __name__ == '__main__': unittest.main()
