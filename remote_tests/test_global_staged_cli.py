"""Independent OFFLINE staged-global integration through both real Python CLIs.

Google documents, native-tool results, installed-client/version observations and
inference text are SYNTHETIC. The actual parent and child CLI parsers, durable
ledgers, signed CAS transitions, child materialization and loopback HTTP
facades are exercised. No collaboration tool, live Google, installed Codex,
credential, external network, or production/native-acceptance claim is involved.
"""
import concurrent.futures
import json
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from remote_tests.test_global_control import FakeGoogle
from remote_transport import global_control as queue, global_pilot as pilot
from remote_transport import global_desktop as desktop, global_config as config
from remote_transport import router_bootstrap as bootstrap
from remote_transport.backend import GoogleDriveBackend
from remote_transport.control import GoogleDocsCASControlStore, SessionCoordinator
from remote_transport.controlled import CASWorker
from remote_transport.global_fixture import unused_fixture_port
from remote_transport.global_gateway import Store, Gateway, private_dir, private_write
from remote_transport.global_google import GoogleQueueBridge
from remote_transport.model import Object, ProtocolError, canonical, hash_bytes
from remote_transport.router_join import _source_hashes
from remote_transport.selection import load_catalog, select
from remote_transport.session import Journal


class GlobalStagedCLIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.clock = float(int(time.time()))
        self.now = patch('time.time', side_effect=lambda: self.clock)
        self.now.start(); self.addCleanup(self.now.stop)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.package = Path(__file__).resolve().parents[1]
        self.serial = 0
        self.parent_dir = private_dir(self.root / 'parent-ledger', create=True)
        self.child_dir = private_dir(self.root / 'child-ledger', create=True)
        self.parent_id = '/root/synthetic_staged_controller'
        self.selection = select(load_catalog(), 'gpt-6.1-sol', 'high')
        self.store, self.generation = Store.initialize(
            self.root / 'gateway', self.selection, port=unused_fixture_port())
        self.gateway = Gateway(self.store).start(); self.addCleanup(self.gateway.close)
        self.google = FakeGoogle()
        self.code = secrets.token_hex(32)
        self.code_file = self.file(self.code.encode(), 'queue-code')
        did = self.google.create_document_once('synthetic-folder', 'synthetic queue')
        self.initial = queue.initial(activation_id=self.generation,
            queue_id=secrets.token_hex(16), folder_id='synthetic-folder', document_id=did,
            tab_id='t.0', join_code=self.code, created=int(self.clock),
            expires=int(self.clock)+1800, runtime_source_hashes=_source_hashes())
        self.bridge = GoogleQueueBridge(self.store, self.root / 'bridge', self.google,
            self.google, self.initial, self.code, bootstrap_seconds=1800)
        self.addCleanup(self.bridge.close)
        self.bridge.initialize_blank_queue()
        self.event('join', capacity=2, seconds=1500)
        self.clock += 1
        self.event('heartbeat'); self.bridge.sync_heartbeat()
        self.cli_binary = self.file(b'explicit synthetic executable', 'fixture-cli')
        self.desktop_binary = self.file(b'explicit synthetic executable', 'fixture-desktop')
        self.cli_binary.chmod(0o700); self.desktop_binary.chmod(0o700)
        parser = patch.object(pilot, '_parser_binding', return_value={
            'version': '0.13.3', 'source': 'explicit synthetic version fixture'})
        parser.start(); self.addCleanup(parser.stop)
        sources = patch.object(pilot, '_sources', return_value=pilot._sources())
        sources.start(); self.addCleanup(sources.stop)
        with patch.object(pilot.subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout=config.CODEX_VERSION+'\n')):
            self.versions = pilot.observe_versions(self.store, self.cli_binary, self.desktop_binary)

    def path(self, label):
        self.serial += 1
        return self.root / f'{self.serial:04d}-{label}.json'

    def file(self, value, label='evidence'):
        path = self.path(label)
        private_write(path, value if isinstance(value, bytes) else canonical(value))
        return path

    def run_cli(self, module, operation, *, error=None, **arguments):
        # A real new interpreter runs the module's __main__ and argument parser.
        # Freeze only wall time, so >180s setup is tested without slow sleeps.
        source = ('import runpy,time; time.time=lambda: '+repr(self.clock)+'; '
                  'runpy.run_module('+repr(module)+',run_name="__main__")')
        argv = [sys.executable, '-c', source, operation]
        for key, value in arguments.items():
            if value is not None:
                argv += ['--'+key.replace('_', '-'), str(value)]
        result = subprocess.run(argv, cwd=self.package, capture_output=True, text=True,
                                timeout=15, check=False)
        try: value = json.loads(result.stdout)
        except ValueError:
            self.fail(f'{module} {operation} did not return JSON: {result.stdout!r}; {result.stderr!r}')
        if error is None:
            self.assertEqual(result.returncode, 0, (operation, value, result.stderr))
            self.assertNotIn('error', value)
        else:
            self.assertNotEqual(result.returncode, 0, (operation, value))
            self.assertRegex(value.get('error', ''), error)
        return value

    def parent(self, operation, **arguments):
        defaults = dict(snapshot=self.file(self.google.get_document(self.initial['document_id']), 'queue'),
            document_id=self.initial['document_id'], tab_id='t.0',
            join_code_file=self.code_file, state_dir=self.parent_dir, native_task_id=self.parent_id)
        defaults.update(arguments)
        return self.run_cli('remote_transport.global_native', operation, **defaults)

    def event(self, kind, route_id=None, **arguments):
        plan = self.path(kind+'-plan')
        result = self.parent('plan-'+kind, route_id=route_id, save=plan, **arguments)
        # This helper commits actual events only. A read-only heartbeat handoff
        # must be asserted separately, never mistaken for a missing CAS file.
        self.assertNotEqual(result.get('action'), 'heartbeat_deferred_for_ready')
        self.parent('check-cas', plan_file=plan)
        response = self.google.batch_update_document(**result['tool_arguments'])
        check = self.parent('verify', plan_file=plan, response=self.file(response),
            readback=self.file(self.google.get_document(self.initial['document_id'])))
        self.assertTrue(check['verified'])
        return plan

    def reserve(self):
        self.reservation = pilot.reserve_preflight_route(self.store, version_evidence=self.versions)
        self.rid = self.reservation['route_id']
        self.assert_counts(routes=1, requests=0)
        self.assertFalse(list((self.store.root/'pilot').glob('plan-*.json')))
        self.bridge.prepare_child(self.rid)
        self.child_state = self.bridge.read().state['logical']['demands'][self.rid]['child_bootstrap']
        self.child_code_file = self.file(queue.child_code(self.code, self.generation, self.rid).encode(), 'child-code')
        self.assert_counts(routes=1, requests=0)
        return self.reservation

    def native(self, *, publish=True):
        self.event('claim', self.rid); self.event('begin', self.rid)
        self.spawn_path = self.path('spawn')
        out = self.parent('plan-native', route_id=self.rid, save=self.spawn_path, package_root=self.package)
        self.assertFalse(out['native_invoked_by_python'])
        self.parent('check-native', plan_file=self.spawn_path)
        self.actual_arguments = out['arguments']
        # The real submitted message now carries only the exact private artifact
        # reference. Do not parse/reconstruct an expanded inline descriptor.
        self.spawn_packet = json.loads(self.spawn_path.read_bytes())
        self.handoff_reference = self.spawn_packet['handoff']
        self.assertEqual(self.actual_arguments, self.spawn_packet['arguments'])
        self.assertIn(self.handoff_reference['path'], self.actual_arguments['message'])
        self.assertIn(self.handoff_reference['sha256'], self.actual_arguments['message'])
        self.assertNotIn(self.code, self.actual_arguments['message'])
        self.actual_native_result = {'task_name': '/root/synthetic_parent/'+out['arguments']['task_name']}
        self.child_id = self.actual_native_result['task_name']
        self.receipt_path = self.path('admission-receipt')
        self.parent('record-native', plan_file=self.spawn_path,
            actual_arguments=self.file(self.actual_arguments), native_result=self.file(self.actual_native_result),
            save=self.receipt_path)
        self.receipt = json.loads(self.receipt_path.read_bytes())
        if publish: self.admitted_path = self.event('admitted', self.rid)
        return self.receipt

    def child(self, operation, **arguments):
        did = self.child_state['bootstrap_document_id']
        defaults = dict(snapshot=self.file(self.google.get_document(did), 'bootstrap'),
            document_id=did, tab_id=self.child_state['bootstrap_tab_id'],
            join_code_file=self.child_code_file, state_dir=self.child_dir, native_task_id=self.child_id)
        defaults.update(arguments)
        return self.run_cli('remote_transport.router_join', operation, **defaults)

    def prepare_probe(self):
        self.probe_path = self.path('reverse-probe')
        self.probe = self.child('prepare-probe', save=self.probe_path)
        self.probe_id = self.google.generate_id()
        self.google.create_bytes(self.initial['folder_id'], self.probe['file_name'],
                                 self.probe_path.read_bytes(), self.probe_id)

    def admit_arguments(self):
        return dict(writer_identity=self.child_state['control']['worker_writer_identity'],
            probe_file_id=self.probe_id, probe_name=self.probe['file_name'],
            probe_sha256=self.probe['sha256'], admission_receipt=self.receipt_path)

    def child_commit(self, operation, **arguments):
        plan = self.path('child-'+operation)
        result = self.child(operation, save=plan, **arguments)
        response = self.google.batch_update_document(**result['tool_arguments'])
        checked = self.child('verify', plan_file=plan, response=self.file(response),
            readback=self.file(self.google.get_document(self.child_state['bootstrap_document_id'])))
        self.assertTrue(checked['verified'])
        return plan

    def import_parent(self, **arguments):
        # The verified parent CLI installs admission in its fixed separate child ledger.
        out = self.parent('import-child-admission', route_id=self.rid,
            admission_receipt=self.receipt_path, child_native_task_id=self.child_id, **arguments)
        self.assertTrue(out['imported'])
        # Exercise actual child consumption only after successful parent import.
        # It must verify the artifact and imported receipt before releasing JOIN.
        consumed = self.run_cli('remote_transport.global_handoff', 'consume',
            handoff_file=self.handoff_reference['path'], sha256=self.handoff_reference['sha256'],
            native_task_id=self.child_id, admission_receipt=self.receipt_path)
        self.assertTrue(consumed['verified']); self.assertTrue(consumed['read_only'])
        self.child_descriptor = consumed['descriptor']
        self.assertEqual(self.child_descriptor['selection'], self.selection)
        self.assertEqual(self.child_descriptor['route_id'], self.rid)
        self.assertEqual(self.child_descriptor['state_dir'], self.spawn_packet['child_state_dir'])
        self.assertEqual(self.child_descriptor['expected_bootstrap_root'], bootstrap.root_context(self.child_state))
        self.assertEqual(self.child_descriptor['join_code'], queue.child_code(self.code, self.generation, self.rid))
        self.assertEqual(out['child_state_dir'], self.child_descriptor['state_dir'])
        self.child_code_file = self.file(self.child_descriptor['join_code'].encode(), 'consumed-child-code')
        self.child_dir = Path(out['child_state_dir'])
        self.assertNotEqual(self.parent_dir, self.child_dir)
        return out

    def ready(self):
        self.import_parent(); self.prepare_probe()
        self.child_commit('plan-admit', **self.admit_arguments())
        self.assertEqual(self.bridge.advance_child(self.rid)['state'], 'await_child_polling')
        forward = self.child_state['forward_probe']
        metadata = {'id': forward['file_id'], 'title': forward['name'],
                    'mime_type': 'application/json', 'parent_ids': [self.initial['folder_id']]}
        self.child('verify-forward-probe', probe_metadata=self.file(metadata),
                   probe_raw=self.file(self.google.files[forward['file_id']]['raw']))
        self.runtime = self.root/'child-runtime'
        materialized = self.child('materialize', root=self.runtime)
        self.assertTrue(materialized['materialized'])
        control_id = self.child_state['control']['document_id']
        self.child_commit('plan-ready', root=self.runtime,
                          control_snapshot=self.file(self.google.get_document(control_id)))
        self.assertEqual(self.bridge.advance_child(self.rid)['state'], 'ready')
        self.assertEqual(self.store.route(self.rid)['native_task'], self.child_id)
        self.assert_counts(routes=1, requests=0)
        # Test-only fake inference uses the exact pin/config materialized by the
        # child CLI; admission/polling above never invokes worker_admitted.
        self.pin = Object.parse((self.runtime/'pin.json').read_bytes())
        cfg = json.loads((self.runtime/'config.json').read_bytes())
        backend = GoogleDriveBackend(self.google, self.initial['folder_id'], discovery='control_refs')
        control = GoogleDocsCASControlStore(self.google, cfg['document_id'], cfg['tab_id'],
            cfg['control_id'], self.rid, cfg['writer_identity'])
        self.worker = CASWorker(Journal.provision(self.root/'synthetic-inference', self.pin, 'worker'),
                                backend, SessionCoordinator(control, backend))

    def status(self):
        return pilot.preflight_route_status(self.store, self.reservation['reservation_id'],
            queue_state=self.bridge.read().state, join_code=self.code)

    def finish_plan(self):
        return pilot.finalize_preflight_route(self.store, self.reservation['reservation_id'],
            version_evidence=self.versions, queue_state=self.bridge.read().state, join_code=self.code)

    def assert_counts(self, *, routes, requests):
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM routes').fetchone()[0], routes)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], requests)

    def assert_error(self, pattern, fn, *args, **kwargs):
        with self.assertRaisesRegex(ProtocolError, pattern): fn(*args, **kwargs)

    def test_receipt_alone_cannot_bypass_separate_child_ledger(self):
        self.reserve(); self.native(); self.prepare_probe()
        path = self.path('unimported-admit')
        self.child('plan-admit', save=path, error='parent_native_admission_record_required',
                   **self.admit_arguments())
        self.assertFalse(path.exists())
        self.assert_counts(routes=1, requests=0)

    def test_staged_240_second_setup_real_child_cli_then_single_http_proof(self):
        self.reserve(); self.native()
        ledger = self.parent_dir/(queue.root_hash(self.initial)+'.json')
        before = json.loads(ledger.read_bytes())
        initial_controller = self.bridge.read().state['logical']['controller']
        handoff_deadline = None
        # The 240-second staged setup remains within the original 900-second
        # freshness budget. Yield this admitted->ready writer interval in paced
        # read-only calls instead of issuing the old competing heartbeat CASes.
        for _ in range(10):
            self.clock += 24
            pending_plan = self.path('deferred-heartbeat-plan')
            calls = len(self.google.calls)
            deferred = self.parent('plan-heartbeat', save=pending_plan)
            self.assertEqual(deferred['action'], 'heartbeat_deferred_for_ready')
            self.assertTrue(deferred['read_only'])
            self.assertFalse(deferred['write_attempted'])
            self.assertFalse(deferred['heartbeat_verified'])
            self.assertFalse(deferred['native_spawn_allowed'])
            self.assertEqual(deferred['pending_ready_count'], 1)
            self.assertEqual(deferred['heartbeat_at'], initial_controller['heartbeat_at'])
            self.assertLessEqual(deferred['recheck_after_seconds'], 25)
            self.assertGreater(deferred['recheck_after_seconds'], 0)
            if handoff_deadline is None: handoff_deadline = deferred['observe_before']
            self.assertEqual(deferred['observe_before'], handoff_deadline)
            self.assertLessEqual(handoff_deadline, initial_controller['heartbeat_at']+900)
            self.assertLessEqual(handoff_deadline, initial_controller['lease_expires'])
            self.assertNotIn('tool_arguments', deferred)
            self.assertNotIn('plan_file', deferred)
            self.assertFalse(pending_plan.exists())
            current = json.loads(ledger.read_bytes())
            self.assertEqual(current['operations'], before['operations'])
            self.assertEqual(current['spawns'], before['spawns'])
            self.assertEqual(len(self.google.calls), calls)
            self.bridge.sync_heartbeat()
            self.assertFalse(self.status()['ready'])
            self.assert_counts(routes=1, requests=0)
        self.ready()
        self.assertTrue(self.status()['ready'])
        ready_source = self.bridge.read()
        self.assertEqual(ready_source.state['logical']['demands'][self.rid]['state'], 'ready')
        heartbeat_plan = self.event('heartbeat')
        heartbeat_packet = json.loads(heartbeat_plan.read_bytes())
        self.assertEqual(heartbeat_packet['tool_arguments']['write_control'],
                         {'requiredRevisionId': ready_source.revision_id})
        self.assertLessEqual(heartbeat_packet['execute_before'], initial_controller['heartbeat_at']+900)
        self.assertLessEqual(heartbeat_packet['execute_before'], self.clock+120)
        after_heartbeat = self.bridge.read().state
        self.assertEqual(after_heartbeat['epoch'], ready_source.state['epoch']+1)
        self.assertEqual(after_heartbeat['events'][-1]['kind'], 'heartbeat')
        self.assertEqual(after_heartbeat['logical']['controller']['heartbeat_at'], int(self.clock))
        self.bridge.sync_heartbeat()
        plan = self.finish_plan()
        self.assertGreater(plan['expires'], self.clock)
        with patch.object(desktop, 'post_preflight', wraps=desktop.post_preflight) as post:
            with concurrent.futures.ThreadPoolExecutor() as pool:
                control = {'lock': threading.Lock(), 'cancelled': False, 'socket': None}
                result = pool.submit(desktop.post_preflight, self.store, plan, control)
                try:
                    permit = self.worker.poll(self.worker.start_next, attempts=8,
                                              initial_delay=.02, max_delay=.1)
                    self.assertIsNotNone(permit)
                    self.worker.complete(permit, plan['expected_nonce'])
                    self.assertEqual(result.result(timeout=10), {'http_status': 200})
                finally:
                    # A failed assertion must not leave the executor waiting for
                    # a 600-second HTTP timeout and hide the original failure.
                    with control['lock']:
                        control['cancelled'] = True
                        if control['socket'] is not None:
                            try: control['socket'].shutdown(socket.SHUT_RDWR)
                            except OSError: pass
            self.assertEqual(post.call_count, 1)
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            with self.store.transaction() as db:
                row = db.execute('SELECT state FROM requests WHERE route=?', (self.rid,)).fetchone()
            if row and row['state'] == 'text_complete': break
            time.sleep(.01)
        proof = pilot.verify_preflight(self.store, plan['plan_id'],
            queue_state=self.bridge.read().state, join_code=self.code)
        accepted = pilot.require_pilot(self.store.root, proof['proof_id'], config.CODEX_VERSION, config.CODEX_VERSION)
        self.assertTrue(accepted['pilot_ready']); self.assertFalse(accepted['production_ready'])
        self.assertEqual(proof['native_task_id'], self.child_id)
        self.assert_counts(routes=1, requests=1)
        self.assertEqual(sum(call[0] == 'create_doc' for call in self.google.calls), 3)
        parent_state = json.loads((self.parent_dir/(queue.root_hash(self.initial)+'.json')).read_bytes())
        self.assertEqual(list(parent_state['spawns']), [self.rid])
        self.assert_error('already_consumed', self.finish_plan)
        self.assert_error('already_reserved_no_replay', pilot.reserve_preflight_route,
                          self.store, version_evidence=self.versions)
        self.parent('plan-native', route_id=self.rid, save=self.path('duplicate-spawn'),
                    package_root=self.package, error='already_reserved_no_replay')
        self.assert_counts(routes=1, requests=1)

    def test_unknown_create_never_replayed_and_no_http_request(self):
        self.reservation = pilot.reserve_preflight_route(self.store, version_evidence=self.versions)
        self.rid = self.reservation['route_id']; self.google.fail_create = True
        with self.assertRaisesRegex(RuntimeError, 'create_unknown'): self.bridge.prepare_child(self.rid)
        before = len(self.google.calls)
        self.assert_error('runtime_exists', self.bridge.prepare_child, self.rid)
        self.assertEqual(len(self.google.calls), before)
        self.assertFalse(self.status()['ready'])
        self.assert_counts(routes=1, requests=0)

    def test_setup_expiry_never_mints_request_or_replacement(self):
        self.reserve()
        self.clock = self.reservation['expires']
        self.assert_error('setup_expired', self.status)
        self.assertFalse(list((self.store.root/'pilot').glob('plan-*.json')))
        self.assert_counts(routes=1, requests=0)

    def test_stop_before_child_ready_does_not_dispatch(self):
        self.reserve(); self.native()
        self.bridge.stop()
        self.assert_error('live_native_controller|queue_closed|controller', self.status)
        self.assert_error('live_native_controller|queue_closed|controller', self.finish_plan)
        self.assert_counts(routes=1, requests=0)

    def test_route_selection_or_identity_changes_reject_preflight(self):
        self.reserve()
        for field, value in [('selection', canonical(select(load_catalog(), 'gpt-6-astra', 'xhigh')).decode()),
                             ('identity', canonical({'session-id': 'changed', 'thread-id': 'changed'}).decode())]:
            with self.subTest(field=field):
                old = self.store.route(self.rid)[field]
                with self.store.transaction() as db:
                    db.execute(f'UPDATE routes SET {field}=? WHERE id=?', (value, self.rid))
                self.assert_error('route_binding_mismatch', self.status)
                with self.store.transaction() as db:
                    db.execute(f'UPDATE routes SET {field}=? WHERE id=?', (old, self.rid))
        self.assert_counts(routes=1, requests=0)

    def test_unresolved_admitted_write_blocks_import_and_spawn_replay(self):
        self.reserve(); self.native(publish=False)
        plan = self.path('unresolved-admitted')
        self.parent('plan-admitted', route_id=self.rid, save=plan)
        self.parent('verify', plan_file=plan,
            readback=self.file(self.google.get_document(self.initial['document_id'])), error='not_observed')
        self.parent('import-child-admission', route_id=self.rid, admission_receipt=self.receipt_path,
                    child_native_task_id=self.child_id, error='unresolved|verified|admitted')
        self.parent('plan-native', route_id=self.rid, save=self.path('blocked-spawn'),
                    package_root=self.package, error='unresolved|already_reserved')
        self.assert_counts(routes=1, requests=0)

    def test_import_requires_exact_recorded_receipt_path_and_actual_identity(self):
        self.reserve(); self.native()
        writes = len(self.google.calls)
        copied_receipt = self.file(self.receipt, 'copied-receipt')
        self.parent('import-child-admission', route_id=self.rid,
            admission_receipt=copied_receipt, child_native_task_id=self.child_id,
            error='actual_admission_mismatch')
        self.parent('import-child-admission', route_id=self.rid,
            admission_receipt=self.receipt_path, child_native_task_id='/root/synthetic_wrong_child',
            error='actual_admission_mismatch')
        imported = self.import_parent()
        self.assertFalse(imported['reconciled_existing'])
        reconciled = self.import_parent()
        self.assertTrue(reconciled['reconciled_existing'])
        self.assertEqual(len(self.google.calls), writes)
        self.prepare_probe(); self.child_commit('plan-admit', **self.admit_arguments())
        self.assert_counts(routes=1, requests=0)

    def test_lost_admitted_response_reconciles_exact_event_without_write_replay(self):
        self.reserve(); self.native(publish=False)
        plan = self.path('lost-admitted-response')
        out = self.parent('plan-admitted', route_id=self.rid, save=plan)
        self.parent('check-cas', plan_file=plan)
        self.google.lose = True
        with self.assertRaises(TimeoutError):
            self.google.batch_update_document(**out['tool_arguments'])
        writes = len(self.google.calls)
        self.parent('import-child-admission', route_id=self.rid,
            admission_receipt=self.receipt_path, child_native_task_id=self.child_id,
            error='unresolved')
        checked = self.parent('verify', plan_file=plan,
            readback=self.file(self.google.get_document(self.initial['document_id'])))
        self.assertTrue(checked['verified']); self.assertTrue(checked['reconciled_from_event'])
        self.import_parent()
        self.assertEqual(len(self.google.calls), writes)
        self.parent('plan-admitted', route_id=self.rid, save=self.path('reissued-admitted'),
                    error='already_issued_no_replay')
        self.assert_counts(routes=1, requests=0)

    def test_package_source_mismatch_does_not_expose_native_plan(self):
        self.reserve(); self.event('claim', self.rid); self.event('begin', self.rid)
        package = self.root/'tampered-package'; package.mkdir(mode=0o700)
        for relative in {**self.initial['runtime_source_hashes'], **self.initial['controller_source_hashes']}:
            dest = package/relative; dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.package/relative, dest)
        (package/'native_connector/runner.js').write_text('synthetic changed source')
        destination = self.path('blocked-source-spawn')
        self.parent('plan-native', route_id=self.rid, save=destination, package_root=package,
                    error='package_source_mismatch')
        self.assertFalse(destination.exists())
        self.assert_counts(routes=1, requests=0)


if __name__ == '__main__': unittest.main()
