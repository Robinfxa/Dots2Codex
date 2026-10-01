"""Bounded Global-child pairing phases. Offline; never upload, spawn, or infer.

Only already-imported immutable Global handoffs are accepted. Original Router
validators remain authoritative. The emitted native cell uses actual connector
raw bytes and returns at the first bundle wait; no timer or retry is installed.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import global_handoff as handoff
from . import router_join as join
from .backend import read_private_file
from .cli import write_new
from .connector_files import capture, materialize, packet_chunk
from .model import ProtocolError, canonical, hash_bytes, require
from .router_bootstrap import root_context, verify_context


class Pairing:
    def __init__(self, reference, native_task_id, receipt_path):
        self.reference = reference
        self.native_task_id = native_task_id
        self.receipt_path = str(handoff.exact_path(receipt_path))
        self.value = self.guard()
        self.root = handoff.exact_path(self.value['descriptor']['state_dir'])
        self.bootstrap = self.value['descriptor']['expected_bootstrap_root']
        self.code_path = self.root / 'pairing-join-code.txt'
        self.runtime = self.root / 'runtime'

    def guard(self):
        value = handoff.consume(self.reference, self.native_task_id, self.receipt_path)
        if hasattr(self, 'value'):
            require(value == self.value, 'router_pairing_handoff_changed')
        return value

    def context(self):
        self.guard()
        return {'cwd': self.value['package_root'], 'stateDir': str(self.root),
                'nativeTaskId': self.native_task_id,
                'documentId': self.bootstrap['bootstrap_document_id'],
                'tabId': self.bootstrap['bootstrap_tab_id'],
                'controlDocumentId': self.bootstrap['control']['document_id'],
                'expires': self.value['expires']}

    def snapshot(self, path):
        self.guard()
        snap = join._snapshot(path, self.bootstrap['bootstrap_document_id'], self.bootstrap['bootstrap_tab_id'])
        verify_context(snap.state, self.value['descriptor']['join_code'], snap.document_id, snap.tab_id,
                       expected_root=self.bootstrap)
        return snap

    def run(self, operation, snapshot=None, **kwargs):
        self.guard()
        raw = self.value['descriptor']['join_code'].encode('ascii')
        if self.code_path.exists():
            require(read_private_file(self.code_path, 256) == raw, 'router_pairing_code_file_mismatch')
        else:
            write_new(self.code_path, raw)
        argv = [operation, '--join-code-file', str(self.code_path), '--state-dir', str(self.root)]
        if snapshot is not None:
            self.snapshot(snapshot)
            argv += ['--snapshot', str(snapshot), '--document-id', self.bootstrap['bootstrap_document_id'],
                     '--tab-id', self.bootstrap['bootstrap_tab_id'], '--native-task-id', self.native_task_id]
        for key, value in kwargs.items():
            if value is not None:
                argv += ['--' + key.replace('_', '-'), str(value)]
        result = join.main(argv)
        self.guard()
        return result

    def prepare(self, snapshot):
        state = self.snapshot(snapshot).state
        require(state['stage'] == 'WAITING_FOR_WORKER', 'router_pairing_waiting_required')
        # prepare-probe itself performs inspect's HMAC/root/identity/expiry and
        # durable observation checks. A redundant inspect invocation is omitted.
        probe = self.run('prepare-probe', snapshot, save=self.root / 'pairing-probe.json')
        return {'probe': probe, 'forward_probe': state['forward_probe'], 'folder_id': state['folder_id'],
                'one_attempt_only': True, 'checked_at': time.time(),
                'execute_before': self.value['expires']}

    def plan_admit(self, snapshot, forward_metadata, forward_raw, upload_response, reverse_metadata, reverse_raw):
        state = self.snapshot(snapshot).state
        require(state['stage'] == 'WAITING_FOR_WORKER', 'router_pairing_waiting_required')
        self.run('verify-forward-probe', snapshot, probe_metadata=forward_metadata, probe_raw=forward_raw)
        ledger = join.JoinLedger(self.root, state)
        with ledger.locked() as saved:
            probe = saved['probe']; uploaded = join._provider(join._read(upload_response))
            metadata = join._provider(join._read(reverse_metadata))
            require(probe is not None and isinstance(uploaded, dict) and uploaded.get('success') is True
                    and isinstance(uploaded.get('id'), str) and uploaded['id'], 'router_pairing_upload_unknown')
            file_id = uploaded['id']
            require(isinstance(metadata, dict) and metadata.get('id') == file_id
                    and metadata.get('title') == probe['file_name'] and metadata.get('mime_type') == probe['mime_type']
                    and type(metadata.get('parent_ids')) is list
                    and all(isinstance(v, str) for v in metadata['parent_ids'])
                    and probe['folder_id'] in metadata['parent_ids']
                    and ('trashed' not in metadata or metadata['trashed'] is False),
                    'router_pairing_reverse_metadata_mismatch')
            raw = read_private_file(Path(reverse_raw), join.MAX)
            expected = read_private_file(Path(probe['probe_file']), join.MAX)
            require(raw == expected and hash_bytes(raw) == probe['sha256'], 'router_pairing_reverse_raw_mismatch')
            self.guard()
            saved['reverse_probe'] = {'file_id': file_id, 'sha256': hash_bytes(raw),
                'upload_response_sha256': hash_bytes(canonical(uploaded)),
                'metadata_sha256': hash_bytes(canonical(metadata)), 'trash_state_verified': False}
            ledger.save(saved)
        result = self.run('plan-admit', snapshot,
            writer_identity=state['control']['worker_writer_identity'], probe_file_id=file_id,
            probe_name=probe['file_name'], probe_sha256=probe['sha256'], admission_receipt=self.receipt_path,
            save=self.root / 'pairing-admit-plan.json')
        return {**result, 'plan_file': str(self.root / 'pairing-admit-plan.json')}

    def reserve_write(self, plan_file):
        """Last local boundary before CAS: source, clock, exact packet, one use."""
        self.guard()
        packet = join._read(plan_file)
        expected = packet['expected_state']
        verify_context(expected, self.value['descriptor']['join_code'], self.bootstrap['bootstrap_document_id'],
                       self.bootstrap['bootstrap_tab_id'], expected_root=self.bootstrap)
        ledger = join.JoinLedger(self.root, expected)
        with ledger.locked() as saved:
            matches = [record for record in saved['operations'].values()
                       if record['operation_id'] == packet['operation_id']]
            require(len(matches) == 1, 'router_operation_evidence_required')
            record = matches[0]
            require(record['path'] == str(Path(plan_file).absolute())
                    and record['plan_sha256'] == hash_bytes(canonical(packet))
                    and record['status'] == 'issued_outcome_unknown'
                    and not record.get('pairing_cell_dispatched'), 'router_pairing_write_already_reserved_no_replay')
            require(packet['tool_arguments']['write_control']['requiredRevisionId'] == saved['last_revision'],
                    'router_pairing_cas_revision_changed')
            self.guard()
            record['pairing_cell_dispatched'] = True
            ledger.save(saved)
        self.guard()
        return {'reserved': True, 'operation_id': packet['operation_id'], 'one_attempt_only': True,
                'checked_at': time.time(), 'execute_before': self.value['expires']}

    def verify(self, plan_file, readback, response=None):
        self.snapshot(readback)
        result = self.run('verify', plan_file=plan_file, response=response, readback=readback)
        return {**result, 'snapshot': str(readback)}

    def ready(self, snapshot, control_snapshot=None):
        state = self.snapshot(snapshot).state
        ledger = join.JoinLedger(self.root, state)
        with ledger.locked() as saved:
            admit = saved['operations'].get('admit')
            require(admit is not None and admit['status'] == 'verified' and admit['evidence'] is not None
                    and admit['evidence']['event'] in state['events']
                    and admit['operation_id'] == state['events'][0]['operation_id'],
                    'worker_admission_operation_unverified')
            require('ready' not in saved['operations'], 'bootstrap_operation_already_issued_no_replay')
        if state['stage'] == 'WORKER_ADMITTED':
            # No background read, timer, or implicit resubmission. The caller may
            # perform a later read-only readyOnce when the peer publishes bundle.
            self.run('inspect', snapshot)
            return {'action': 'wait_for_bundle', 'stage': state['stage'], 'retry_writes': False}
        require(state['stage'] == 'BUNDLE_READY' and control_snapshot,
                'worker_runtime_and_fresh_bundle_required')
        materialized = self.run('materialize', snapshot, root=self.runtime)
        result = self.run('plan-ready', snapshot, root=self.runtime, control_snapshot=control_snapshot,
                          save=self.root / 'pairing-ready-plan.json')
        return {**result, 'plan_file': str(self.root / 'pairing-ready-plan.json'),
                'worker_runtime': str(self.runtime), 'execution_mode': materialized['execution_mode']}


def emit_cell(pairing, phase, destination):
    require(phase in {'pair', 'ready'}, 'invalid_router_pairing_phase')
    config = {**pairing.context(), 'handoff': pairing.reference, 'admissionReceipt': pairing.receipt_path}
    release = Path(config['cwd'])
    source = '\n'.join((release / 'native_connector' / name).read_text()
                       for name in ('tool_adapter.js', 'router_pairing.js'))
    call = 'pairOnce' if phase == 'pair' else 'readyOnce'
    text = '// @exec: {"yield_time_ms": 1000, "max_output_tokens": 2000}\n' + source
    text += '\nconst pairingAdapter=createRouterPairingToolAdapter(tools,' + json.dumps(config) + ');\n'
    text += 'text(await createRouterPairingRunner(pairingAdapter.io).' + call + '());\n'
    pairing.guard()
    write_new(destination, text.encode())
    return {'cell_saved': str(destination), 'phase': phase, 'sha256': hash_bytes(text.encode()),
            'native_execution_required': True, 'connector_calls': 0, 'executes_on_emission': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('operation', choices=['emit-cell', 'context', 'prepare', 'plan-admit', 'reserve-write', 'verify', 'verify-ready', 'ready'])
    for key in ('handoff-file', 'sha256', 'native-task-id', 'admission-receipt'):
        parser.add_argument('--' + key, required=True)
    for key in ('snapshot', 'control-snapshot', 'forward-metadata', 'forward-raw', 'upload-response',
                'reverse-metadata', 'reverse-raw', 'forward-fetch', 'reverse-fetch',
                'forward-download', 'reverse-download', 'plan-file', 'response', 'readback', 'save', 'result-file'):
        parser.add_argument('--' + key)
    parser.add_argument('--phase', choices=['pair', 'ready'])
    parser.add_argument('--inline-evidence', action='store_true')
    parser.add_argument('--result-large-chunk', action='store_true')
    parser.add_argument('--reserve-inline', action='store_true')
    args = parser.parse_args(argv); os.umask(0o077)
    pairing = Pairing({'path': args.handoff_file, 'sha256': args.sha256}, args.native_task_id, args.admission_receipt)
    evidence_files = {}
    require(not args.reserve_inline or args.operation in {'plan-admit', 'ready', 'verify-ready'},
            'invalid_router_pairing_inline_reservation')
    require(not args.result_large_chunk or args.result_file, 'router_pairing_result_file_required')
    if args.inline_evidence:
        allowed = {'prepare': {'snapshot'}, 'plan-admit': {'snapshot', 'forward_metadata', 'forward_fetch', 'forward_download',
                                  'reverse_metadata', 'reverse_fetch', 'reverse_download', 'upload_response'},
                   'verify': {'response', 'readback'},
                   'verify-ready': {'response', 'readback', 'control_snapshot'}, 'ready': {'snapshot', 'control_snapshot'}}
        require(args.operation in allowed, 'invalid_router_pairing_inline_operation')
        raw = sys.stdin.buffer.read(60001)
        require(len(raw) <= 60000, 'router_pairing_inline_evidence_too_large')
        envelope = handoff.strict_json(raw)
        require(isinstance(envelope, dict) and 0 < len(envelope) <= len(allowed[args.operation])
                and set(envelope) <= allowed[args.operation], 'invalid_router_pairing_inline_evidence')
        for key, resource in envelope.items():
            require(getattr(args, key) is None and isinstance(resource, dict),
                    'invalid_router_pairing_inline_evidence')
        # One finite envelope, never caller-selected operations or executable text.
        # Keep complete original provider structures before protocol validation.
        for key, resource in envelope.items():
            evidence_files[key] = capture(pairing.root, canonical(resource, max_bytes=join.MAX))['path']
            setattr(args, key, evidence_files[key])
    if args.operation == 'emit-cell': result = emit_cell(pairing, args.phase, args.save)
    elif args.operation == 'context': result = pairing.context()
    elif args.operation == 'prepare': result = pairing.prepare(args.snapshot)
    elif args.operation == 'plan-admit':
        if args.forward_download is not None or args.reverse_download is not None:
            require(args.forward_download and args.reverse_download and args.forward_fetch and args.reverse_fetch
                    and args.forward_raw is None and args.reverse_raw is None,
                    'router_pairing_exact_download_evidence_required')
            # The native adapter obtains these exact returned paths from the
            # existing validated raw-reference/download primitive. Keep all
            # provider/download responses; copy actual bytes under private I/O
            # checks before either probe validator is allowed to accept them.
            for direction in ('forward', 'reverse'):
                download = join._read(getattr(args, direction + '_download'))
                fetched = join._provider(join._read(getattr(args, direction + '_fetch')))
                metadata = join._provider(join._read(getattr(args, direction + '_metadata')))
                require(isinstance(download, dict) and isinstance(download.get('path'), str)
                        and isinstance(fetched, dict) and isinstance(metadata, dict)
                        and fetched.get('id') == metadata.get('id'), 'router_pairing_exact_download_evidence_required')
                pairing.guard()
                saved = materialize(pairing.root, download['path'])
                setattr(args, direction + '_raw', saved['path'])
        result = pairing.plan_admit(args.snapshot, args.forward_metadata, args.forward_raw,
                                    args.upload_response, args.reverse_metadata, args.reverse_raw)
    elif args.operation == 'reserve-write': result = pairing.reserve_write(args.plan_file)
    elif args.operation == 'verify': result = pairing.verify(args.plan_file, args.readback, args.response)
    elif args.operation == 'verify-ready':
        verified = pairing.verify(args.plan_file, args.readback, args.response)
        require(verified['verified'] is True, 'router_pairing_admission_unverified')
        result = {**pairing.ready(args.readback, args.control_snapshot), 'admission_verified': True}
    else: result = pairing.ready(args.snapshot, args.control_snapshot)
    if args.reserve_inline and result.get('action') != 'wait_for_bundle':
        result = {**result, 'dispatch_check': pairing.reserve_write(result['plan_file'])}
    if evidence_files:
        result = {**result, 'evidence_files': evidence_files}
    if args.result_file:
        write_new(args.result_file, canonical(result, max_bytes=join.MAX))
        result = packet_chunk(args.result_file, max_chars=65536 if args.result_large_chunk else 16384,
                              large_output=args.result_large_chunk)
    return result


if __name__ == '__main__':
    try: print(json.dumps(main(), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error': str(exc) if isinstance(exc, ProtocolError) else type(exc).__name__}))
        raise SystemExit(1)
