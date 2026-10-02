"""Offline, independent durable boundary fault harness. Never invokes a model."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
REPO = Path(os.environ["LITE_REPO"]) if "LITE_REPO" in os.environ else next(parent for parent in ROOT.parents if (parent / "dots_lite").is_dir())
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(ROOT))
from dots_lite import docs, protocol as p
from dots_lite.storage import private_write
from dots_lite.worker import ParentController, Worker
from test_docs_audit import raw_document
from test_wire_audit import request, response, function_call

KEY = bytes(range(32))


class StrictDocs:
    """Independent exact CAS/indexed mutation port with durable return-loss faults."""
    def __init__(self): self.documents = {}; self.reads = 0; self.writes = 0
    def add(self, name): self.documents[name] = raw_document('\n', document_id=name, revision='r0')
    def read(self, name): self.reads += 1; return copy.deepcopy(self.documents[name])
    def apply(self, plan, *, lost=False):
        self.writes += 1
        source = self.documents[plan['document_id']]
        assert plan['body']['writeControl'] == {'requiredRevisionId': source['revisionId']}
        text = ''.join(r['textRun']['content'] for para in source['tabs'][0]['documentTab']['body']['content'][1:]
                       for r in para['paragraph']['elements'])
        requests = plan['body']['requests']
        if text == '\n': assert len(requests) == 1
        else:
            assert len(requests) == 2
            assert requests[0] == {'deleteContentRange': {'range': {'startIndex': 1,
                'endIndex': len(text.encode('utf-16-le')) // 2, 'tabId': 't.fixture'}}}
        ins = requests[-1]['insertText']; assert ins['location'] == {'index': 1, 'tabId': 't.fixture'}
        assert not ins['text'].endswith('\n')
        revision = 'r' + str(int(source['revisionId'][1:]) + 1)
        self.documents[plan['document_id']] = raw_document(ins['text']+'\n', document_id=plan['document_id'], revision=revision)
        if lost: return {'isError': True, 'content': [{'type': 'text', 'text': 'synthetic lost success response'}]}
        return {'structuredContent': {'documentId': plan['document_id'], 'revisionId': revision,
                'replies': [{} for _ in requests], 'writeControl': {'requiredRevisionId': revision, 'targetRevisionId': None}}}


class Scenario:
    def __init__(self, directory, route_id='route-a', provider=None):
        self.root = Path(directory); self.state_dir = self.root / ('state-'+route_id)
        self.clock_value = [500., 100., 'synthetic-boot']; self.clock = lambda: tuple(self.clock_value)
        self.grant = {'protocol': p.PROTOCOL, 'activation_id': 'activation-a', 'folder_id': 'folder-a',
            'inbox_id': 'inbox-a', 'created_at': 400, 'expires_at': 1000,
            'allowed_pairs': [{'model': 'gpt-6.1-sol', 'reasoning_effort': 'xhigh'}],
            'limits': dict(p.DEFAULT_LIMITS), 'package_sha256': 'a'*64}
        self.route = {'route_id': route_id, 'identity_sha256': p.sha256(route_id.encode()), 'model': 'gpt-6.1-sol',
            'reasoning_effort': 'xhigh', 'outbox_id': 'outbox-'+route_id, 'request': None, 'stop': False}
        self.provider = provider or StrictDocs(); self.provider.add(self.route['outbox_id'])
        source = docs.snapshot(self.provider.read(self.route['outbox_id']), self.route['outbox_id'], max_bytes=p.OUTBOX_MAX_BYTES)
        self.parent = ParentController.create(self.state_dir, self.grant, KEY, self.route, '/root', source, self.clock)
        self.args = {'task_name': 'serve_'+route_id.replace('-','_'), 'message': 'Wait for trusted admission; no prompt here',
                     'fork_turns': 'none', 'model': 'gpt-6.1-sol', 'reasoning_effort': 'xhigh'}
        self.native_boundary_calls = 0; self.inference_boundary_calls = 0

    def reserve(self):
        plan = self.parent.reserve_spawn(self.args)
        permit = self.parent.accept_spawn_reserved_and_issue(self.provider.apply(plan))
        assert permit == self.args
        return permit

    def native_hook(self, *, interrupted=False):
        self.native_boundary_calls += 1
        if interrupted: raise ConnectionError('synthetic native boundary result lost')
        return {'task_name': '/root/'+self.args['task_name'], 'agent_id': 'synthetic-agent-id'}

    def admit(self, *, lost=False):
        self.reserve(); self.actual = self.native_hook()
        self.admission_plan = self.parent.record_actual_admission(self.args, self.actual)
        result = self.parent.accept_admission(self.provider.apply(self.admission_plan, lost=lost))
        if lost: return result
        self.handoff = result
        self.worker = Worker(self.state_dir, KEY, self.actual['task_name'], self.clock)
        self.worker.takeover(result)
        return self.worker

    def begin(self, *, seq=1, raw=None, previous_ack=None, lost=False):
        raw = p.canonical(request()) if raw is None else raw
        self.input_path = self.root / ('incoming-'+self.route['route_id']+'-'+str(seq)+'.json')
        private_write(self.input_path, raw)
        self.route['request'] = {'seq': seq, 'request_id': 'request-'+self.route['route_id']+'-'+str(seq),
            'request_sha256': p.sha256(raw), 'byte_length': len(raw), 'file_id': 'file-input-'+str(seq),
            'folder_id': 'folder-a', 'previous_result_ack': previous_ack, 'begin_before': 1000}
        self.inbox = p.make_inbox(self.grant, [self.route], KEY, 'inbox-op-'+str(seq))
        self.begin_plan = self.worker.prepare_begin(self.inbox, self.input_path)
        ack = self.provider.apply(self.begin_plan, lost=lost)
        value = self.worker.accept_begin_and_expose(ack)
        if not lost:self.exposure=value
        if not lost: self.inference_boundary_calls += 1
        return value

    def save(self):
        if self.worker.state['phase']=='INPUT_EXPOSED':
            binding=self.exposure['binding'];offset=0
            while True:
                chunk=self.worker.expose_tool_schema(binding['request_id'],binding['request_sha256'],
                    binding['package_sha256'],self.exposure['exposure_token'],'local','read_file',offset=offset)
                if chunk['complete']:break
                offset=chunk['next_offset']
        return self.worker.save_actual_result(self.route['request']['request_id'], response(function_call()))

    def publish(self, *, lost=False):
        self.artifact = self.save(); self.worker.record_upload_attempt()
        self.result_plan = self.worker.publish_result({'file_id': 'result-file-a', 'folder_id': 'folder-a',
                                                      'byte_length': self.artifact['byte_length']})
        return self.worker.accept_result(self.provider.apply(self.result_plan, lost=lost))


class WorkerAudit(unittest.TestCase):
    def setUp(self): self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
    def scenario(self, **kwargs): return Scenario(self.temp.name, **kwargs)

    def test_success_has_one_exposure_one_result_and_no_healthy_readbacks(self):
        s = self.scenario(); s.admit(); self.assertEqual(s.begin()['request']['input'], request()['input']); s.publish()
        self.assertEqual(s.worker.state['phase'], 'RESULT_COMMITTED')
        self.assertEqual((s.provider.reads, s.provider.writes), (1, 4))
        self.assertEqual((s.native_boundary_calls, s.inference_boundary_calls), (1, 1))
        with self.assertRaises(p.ProtocolError): s.worker.accept_begin_and_expose(readback=s.provider.read(s.route['outbox_id']))

    def test_committed_begin_ack_lost_reconciles_and_exposes_once(self):
        s = self.scenario(); s.admit(); self.assertEqual(s.begin(lost=True)['status'], 'unknown')
        self.assertEqual(s.worker.state['current']['exposure'], 'INPUT_NOT_EXPOSED')
        restarted = Worker(s.state_dir, KEY, s.actual['task_name'], s.clock)
        self.assertEqual(restarted.accept_begin_and_expose(readback=s.provider.read(s.route['outbox_id']))['request']['input'], request()['input'])
        with self.assertRaises(p.ProtocolError): restarted.accept_begin_and_expose(readback=s.provider.read(s.route['outbox_id']))
        self.assertEqual(s.provider.writes, 3)

    def test_unobserved_begin_and_revision_conflict_do_not_rewrite_or_expose(self):
        s = self.scenario(); s.admit()
        before = s.provider.read(s.route['outbox_id']); self.assertEqual(s.begin(lost=True)['status'], 'unknown')
        self.assertEqual(s.worker.accept_begin_and_expose(readback=before)['status'], 'unknown')
        with self.assertRaises(p.ProtocolError): s.worker.prepare_begin(s.inbox, s.input_path)
        self.assertEqual(s.worker.state['current']['exposure'], 'INPUT_NOT_EXPOSED')
        self.assertEqual(s.provider.writes, 3)

    def test_missing_journal_remote_begin_never_recreates_permit(self):
        s = self.scenario(); s.admit(); s.begin(lost=True)
        (s.state_dir/'journal.json').unlink()
        with self.assertRaisesRegex(p.ProtocolError, 'journal_missing'): Worker(s.state_dir, KEY, s.actual['task_name'], s.clock)
        with self.assertRaises(p.ProtocolError): ParentController.create(s.state_dir,s.grant,KEY,s.route,'/root',
            docs.snapshot(s.provider.read(s.route['outbox_id']),s.route['outbox_id'],max_bytes=p.OUTBOX_MAX_BYTES),s.clock)

    def test_exposure_persistence_failure_returns_no_input(self):
        s = self.scenario(); s.admit(); s.begin(lost=True)
        with patch.object(s.worker.journal, 'write', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError): s.worker.accept_begin_and_expose(readback=s.provider.read(s.route['outbox_id']))
        self.assertEqual(s.worker.state['current']['exposure'], 'INPUT_NOT_EXPOSED')

    def test_interrupted_native_hook_is_never_called_twice(self):
        s = self.scenario(); s.reserve()
        with self.assertRaises(ConnectionError): s.native_hook(interrupted=True)
        restarted = ParentController(s.state_dir, KEY, '/root', s.clock)
        with self.assertRaises(p.ProtocolError): restarted.reserve_spawn(s.args)
        with self.assertRaises(p.ProtocolError): restarted.accept_spawn_reserved_and_issue(readback=s.provider.read(s.route['outbox_id']))
        self.assertEqual(s.native_boundary_calls, 1)

    def test_ambiguous_admission_keeps_parent_ownership_until_exact_reconcile(self):
        s = self.scenario(); self.assertEqual(s.admit(lost=True)['status'], 'unknown')
        with self.assertRaises(p.ProtocolError): Worker(s.state_dir, KEY, s.actual['task_name'], s.clock)
        handoff = s.parent.accept_admission(readback=s.provider.read(s.route['outbox_id']))
        worker = Worker(s.state_dir, KEY, s.actual['task_name'], s.clock); worker.takeover(handoff)
        with self.assertRaises(p.ProtocolError): s.parent.reserve_spawn(s.args)
        with self.assertRaises(p.ProtocolError): s.parent.accept_admission(readback=s.provider.read(s.route['outbox_id']))
        self.assertEqual(s.native_boundary_calls, 1)

    def test_actual_argument_or_child_identity_mismatch_is_rejected(self):
        s = self.scenario(); s.reserve(); result = s.native_hook()
        bad = dict(s.args, reasoning_effort='low')
        with self.assertRaises(p.ProtocolError): s.parent.record_actual_admission(bad, result)
        with self.assertRaises(p.ProtocolError): s.parent.record_actual_admission(s.args, {'task_name': '/unrelated/'+s.args['task_name']})

    def test_error_shaped_actual_native_result_does_not_admit(self):
        s = self.scenario(); s.reserve()
        with self.assertRaises(p.ProtocolError):
            s.parent.record_actual_admission(s.args, {'task_name': '/root/'+s.args['task_name'], 'isError': True})

    def test_upload_retry_preserves_bytes_logical_identity_and_three_attempt_cap(self):
        s = self.scenario(); s.admit(); s.begin(); artifact=s.save()
        before=Path(artifact['path']).read_bytes()
        self.assertEqual(s.worker.record_upload_attempt(),artifact)
        for attempt in (2,3):
            s.worker.record_upload_failure({'category':'transport_unknown','code':'lite_transport_unknown'})
            self.assertEqual(s.worker.retry_upload(artifact['result_id'],artifact['result_sha256'],attempt,'folder-a',
                {'decision':'same_immutable_result_upload_after_raw_and_permission_review',
                 'controller_task_id':s.actual['task_name'],'permission_reference':'synthetic-permission',
                 'raw_result_reference':'synthetic-result-'+str(attempt-1),'prior_disposition':'transport_unknown'}),artifact)
        with self.assertRaises(p.ProtocolError): s.worker.record_upload_attempt()
        self.assertEqual(Path(artifact['path']).read_bytes(), before)
        self.assertEqual(s.save(), artifact)
        changed = response(function_call(), rid='different-response')
        with self.assertRaises(p.ProtocolError): s.worker.save_actual_result(s.route['request']['request_id'],changed)
        self.assertEqual(s.inference_boundary_calls, 1)

    def test_result_ack_lost_recovers_original_artifact_after_expiry(self):
        s = self.scenario(); s.admit(); s.begin(); self.assertEqual(s.publish(lost=True)['status'],'unknown')
        raw=Path(s.artifact['path']).read_bytes(); s.clock_value[:2]=[1200.,800.]
        self.assertEqual(s.worker.accept_result(readback=s.provider.read(s.route['outbox_id']))['status'],'applied')
        self.assertEqual(Path(s.artifact['path']).read_bytes(), raw)
        with self.assertRaises(p.ProtocolError): s.worker.record_upload_attempt()
        with self.assertRaises(p.ProtocolError): s.worker.prepare_begin(s.inbox,s.input_path)
        self.assertEqual(s.inference_boundary_calls,1)

    def test_one_route_unknown_does_not_block_independent_project(self):
        provider=StrictDocs(); a=self.scenario(provider=provider); b=self.scenario(route_id='route-b',provider=provider)
        a.admit(); b.admit(); a.begin(lost=True); self.assertEqual(b.begin()['request']['input'],request()['input']); b.publish()
        self.assertEqual(a.worker.state['phase'],'BEGIN_PREPARED')
        self.assertEqual(b.worker.state['phase'],'RESULT_COMMITTED')
        self.assertNotEqual(a.actual['task_name'],b.actual['task_name'])
        self.assertNotEqual(a.worker.state['record']['identity_sha256'],b.worker.state['record']['identity_sha256'])


if __name__ == '__main__': unittest.main(verbosity=2)
