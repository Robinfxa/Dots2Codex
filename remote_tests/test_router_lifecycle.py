"""Adversarial supervisor tests: isolated fake Docs and local child processes only."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from remote_transport import router_mac as r
from remote_transport import router_child
from remote_transport.cli import write_new
from remote_transport.control import block_for, initial_state, CASConflict
from remote_transport.model import deployment, canonical
from remote_transport.router_bootstrap import (initial_state as boot_initial, forward_probe_bytes,
    block_for as boot_block, worker_admitted, bundle_ready, snapshot_from_document)
from remote_transport.model import hash_bytes


def document(did, tab, rev, text):
    return {'documentId': did, 'revisionId': rev, 'suggestionsViewMode': 'SUGGESTIONS_INLINE',
            'tabs': [{'tabId': tab, 'parentTabId': None, 'body': {'content': [
                {'sectionBreak': {}}, {'paragraph': {'elements': [{'textRun': {'content': text}}]}}]}}]}


class FakeDocs:
    def __init__(self, text, did='control', tab='t.0'):
        self.text = text; self.did = did; self.tab = tab; self.revision = 1
        self.calls = 0; self.mode = 'success'; self.after_write = None
    def get_document(self, did):
        if self.mode == 'unreadable': raise RuntimeError('no read')
        return document(self.did, self.tab, 'r' + str(self.revision), self.text)
    def batch_update_document(self, did, requests, wc):
        self.calls += 1
        if self.mode == 'reject': raise CASConflict('rejected')
        if self.mode == 'lost_absent': raise RuntimeError('unknown')
        assert wc == {'requiredRevisionId': 'r' + str(self.revision)}
        replacement = requests[0]['replaceAllText']
        assert replacement['containsText']['text'] == self.text[:-1]
        self.text = replacement['replaceText'] + '\n'; self.revision += 1
        result = {'documentId': self.did, 'replies': [{'replaceAllText': {'occurrencesChanged': 1}}],
                  'writeControl': {'requiredRevisionId': 'r' + str(self.revision)}}
        if self.after_write: self.after_write(self)
        if self.mode == 'lost_applied': raise RuntimeError('lost')
        return result


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.runtime = self.root / 'router-session'; self.runtime.mkdir(mode=0o700)
        self.path = self.root / 'active.json'
        self.pin = deployment('router-session', '/root/native', seconds=600)
        write_new(self.runtime/'pin.json', self.pin.raw)
        self.active = {'contract': 'dots-router-active/2', 'session_id': 'router-session',
            'stage': 'CONSUMED', 'closed': False, 'runtime': str(self.runtime),
            'pin_file': str(self.runtime/'pin.json'), 'deployment': self.pin.oid,
            'control_document_id': 'control', 'control_tab_id': 't.0', 'control_id': 'cid',
            'facade_pid': None, 'facade_identity': None, 'control_initialization_attempted': True,
            'ready_file': str(self.runtime/'ready.json'), 'facade_log': str(self.runtime/'facade.log'),
            'bootstrap_root': None, 'pin_expires': self.pin.body['payload']['expires']}
        self.config = {'mac_writer_identity': 'mac'}
        r._private_json(self.path, self.active)
        self.docs = FakeDocs(block_for(initial_state(self.pin, 'cid')))
    def tearDown(self): self.temp.cleanup()

    def test_dead_facade_close_uses_authoritative_cas(self):
        outcome = r._cleanup(self.docs, self.path, self.active, self.config)
        self.assertTrue(outcome['closed']); self.assertEqual(self.docs.calls, 1)
        self.assertTrue(outcome['control_close']['authoritative'])
        self.assertFalse(outcome['worker_stop_confirmed'])
        self.assertTrue(json.loads(self.docs.text.split('\n')[1])['closed'])

    def test_dead_facade_does_not_falsely_close_unreadable_control(self):
        self.docs.mode = 'unreadable'
        outcome = r._cleanup(self.docs, self.path, self.active, self.config)
        self.assertFalse(outcome['closed']); self.assertEqual(outcome['stage'], 'RECOVERY_REQUIRED')
        self.assertFalse(r._load_private_json(self.path)['closed'])

    def test_unknown_close_applied_reconciles_exact_operation(self):
        self.docs.mode = 'lost_applied'
        outcome = r._close_control(self.docs, self.active, self.config)
        self.assertTrue(outcome['closed']); self.assertEqual(self.docs.calls, 1)
        again = r._close_control(self.docs, self.active, self.config)
        self.assertEqual(outcome['operation_id'], again['operation_id'])
        self.assertEqual(self.docs.calls, 1)

    def test_unknown_close_absence_never_replays(self):
        self.docs.mode = 'lost_absent'
        with self.assertRaisesRegex(RuntimeError, 'unverified_no_replay'):
            r._close_control(self.docs, self.active, self.config)
        with self.assertRaisesRegex(Exception, 'unknown_no_replay'):
            r._close_control(self.docs, self.active, self.config)
        self.assertEqual(self.docs.calls, 1)
        self.assertEqual(r._load_private_json(self.runtime/'control-close.json')['status'], 'unknown')

    def test_definitely_rejected_close_requires_separate_stop_call(self):
        self.docs.mode = 'reject'
        with self.assertRaisesRegex(RuntimeError, 'new_stop'):
            r._close_control(self.docs, self.active, self.config)
        self.assertEqual(self.docs.calls, 1)
        self.docs.mode = 'success'
        self.assertTrue(r._close_control(self.docs, self.active, self.config)['closed'])
        self.assertEqual(self.docs.calls, 2)

    def test_wrong_control_binding_cannot_close(self):
        other = deployment('router-session', '/root/other', seconds=600)
        self.docs.text = block_for(initial_state(other, 'cid'))
        with self.assertRaisesRegex(Exception, 'binding_mismatch'):
            r._close_control(self.docs, self.active, self.config)
        self.assertEqual(self.docs.calls, 0)

    def test_competing_start_rejected_and_stop_cancels_without_resurrection(self):
        acquired = threading.Event(); cancelled = threading.Event(); done = threading.Event()
        def startup():
            with r._lifecycle_lock(self.path):
                acquired.set()
                self.assertTrue(cancelled.wait(3))
                with self.assertRaisesRegex(Exception, 'cancelled'): r._check_cancelled(self.active)
            done.set()
        thread = threading.Thread(target=startup); thread.start(); self.assertTrue(acquired.wait(3))
        with self.assertRaisesRegex(RuntimeError, 'in_progress'):
            with r._lifecycle_lock(self.path): pass
        r._request_stop(self.active); cancelled.set()
        with r._lifecycle_lock(self.path, wait=True):
            self.assertTrue(r._cancelled(self.active))
        thread.join(3); self.assertTrue(done.is_set()); self.assertFalse(thread.is_alive())

    def test_closed_but_process_unverified_cannot_be_overwritten(self):
        active = {**self.active, 'closed': True, 'stage': 'RECOVERY_REQUIRED', 'process_stopped': False}
        r._private_json(self.path, active)
        with self.assertRaises(Exception): r._check_existing_active(self.path)

    def test_reused_pid_is_not_signalled(self):
        active = {**self.active, 'facade_pid': 12345, 'facade_identity': 'old'}
        with patch.object(r, '_pid_alive', return_value=True), patch.object(r, '_process_identity', return_value='new'), patch.object(r.os, 'kill') as kill:
            self.assertFalse(r._stop_process(active)); kill.assert_not_called()

    def test_startup_readiness_timeout_reaps_process(self):
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        active = {**self.active, 'facade_pid': proc.pid}
        try:
            with self.assertRaisesRegex(RuntimeError, 'ready_timeout'):
                r._wait_ready(proc, self.runtime/'ready.json', self.runtime/'facade.log', timeout=.03)
            self.assertTrue(r._stop_process(active, proc)); self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None: proc.kill(); proc.wait()

    def test_child_without_pid_grant_times_out_before_facade_import(self):
        with self.assertRaisesRegex(RuntimeError, 'grant_timeout'):
            router_child.await_grant(self.runtime, timeout=.02)

    def test_child_wrong_pid_and_stop_marker_fail_closed(self):
        write_new(self.runtime/'facade-launch.json', canonical({'pid': os.getpid()+1, 'session_id': self.runtime.name}))
        with self.assertRaisesRegex(Exception, 'grant_mismatch'): router_child.await_grant(self.runtime)
        r._request_stop(self.active)
        with self.assertRaisesRegex(Exception, 'cancelled'): router_child.await_grant(self.runtime)

    def test_no_log_content_echo_on_early_exit(self):
        secret = 'PRIVATE_LOG_PAYLOAD_SHOULD_NOT_LEAK'
        (self.runtime/'facade.log').write_text(secret)
        proc = types.SimpleNamespace(poll=lambda: 1)
        with self.assertRaises(RuntimeError) as cm:
            r._wait_ready(proc, self.runtime/'ready.json', self.runtime/'facade.log')
        self.assertNotIn(secret, str(cm.exception))

    def test_ready_loop_cancel_prevents_launch_success(self):
        r._request_stop(self.active)
        with self.assertRaisesRegex(Exception, 'cancelled'):
            r._wait_ready(types.SimpleNamespace(poll=lambda: None), self.runtime/'ready.json',
                          self.runtime/'facade.log', active=self.active)

    def test_launch_persists_pid_before_ready_failure_and_reaps_child(self):
        args=[sys.executable,'-m','remote_transport.cli','serve','--synthetic-invalid-option']
        with patch.object(r,'_facade_command',return_value=args):
            with self.assertRaisesRegex(RuntimeError,'before_ready'):
                r._launch_facade(self.path,self.active,{})
        saved=r._load_private_json(self.path)
        self.assertIsInstance(saved['facade_pid'],int)
        self.assertIsNotNone(saved['facade_identity'])
        self.assertTrue((self.runtime/'facade-launch.json').is_file())
        self.assertFalse(r._pid_alive(saved['facade_pid']))

    def test_stop_still_cleans_local_process_when_docs_client_unavailable(self):
        args=types.SimpleNamespace(active=str(self.path),config='unused')
        with patch.object(r,'_load_config',return_value=self.config),patch.object(r,'_set_google_env'), \
             patch('examples.google_clients.create_docs_client',side_effect=RuntimeError('unavailable')), \
             patch.object(r,'_stop_process',return_value=True) as stop:
            outcome=r.stop(args)
        stop.assert_called_once()
        self.assertFalse(outcome['closed']);self.assertTrue(outcome['process_stopped'])
        self.assertEqual(outcome['stage'],'RECOVERY_REQUIRED')

    def test_bootstrap_fast_peer_advance_is_exact_event_reconciled(self):
        code = 'a'*32; now = int(time.time()); boot = 'boot'
        fp = {'file_id': 'forward', 'name': 'dots2codex-router-forward-probe-boot.json',
              'nonce': 'n'*32, 'sha256': hash_bytes(forward_probe_bytes(boot, 'n'*32))}
        state = boot_initial(bootstrap_id=boot, session_id='router-session', created=now,
            expires=now+300, join_code=code, folder_id='folder', control_document_id='control',
            control_tab_id='t.0', control_id='cid', mac_writer_identity='mac', worker_writer_identity='worker',
            bootstrap_document_id='bootstrap', bootstrap_tab_id='t.0', forward_probe=fp)
        raw = canonical({'contract':'dots-router-probe/1','bootstrap_id':boot,'native_task_id':'/root/native'})
        admitted = worker_admitted(state, join_code=code, native_task_id='/root/native',
            probe={'file_id':'reverse','name':'dots2codex-router-probe-boot.json','sha256':hash_bytes(raw)})
        cfg = canonical({'document_id':'control','tab_id':'t.0','control_id':'cid','writer_identity':'worker','folder_id':'folder'})
        ready = bundle_ready(admitted, join_code=code, pin_raw=self.pin.raw, config_raw=cfg, deployment_hash=self.pin.oid)
        docs = FakeDocs(boot_block(state), did='bootstrap')
        def advance(d): d.text = boot_block(ready); d.revision += 1
        docs.after_write = advance
        snap = snapshot_from_document(docs.get_document('bootstrap'), 'bootstrap','t.0')
        result = r._bootstrap_cas(docs, snap, admitted, join_code=code, runtime=self.runtime)
        self.assertEqual(result['stage'],'BUNDLE_READY'); self.assertEqual(docs.calls, 1)
        with self.assertRaises(FileExistsError):
            r._bootstrap_cas(docs, snap, admitted, join_code=code, runtime=self.runtime)
        self.assertEqual(docs.calls, 1)


if __name__ == '__main__': unittest.main()
