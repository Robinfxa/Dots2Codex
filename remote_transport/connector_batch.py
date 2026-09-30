"""Offline durable guards for active-native connector batching; no network/OAuth.

Each start burns all immutable upload intents before returning. Unknown writes are
never replayed. Native tools save full responses; this module verifies their exact
ID associations and acts as an all-verified barrier before a fresh result CAS.
"""
import argparse
import contextlib
import fcntl
import json
import os
import time
from pathlib import Path
from .backend import read_private_file
from .cli import write_new
from .connector_smoke import ConnectorEvidenceMessages, structured
from .connector_worker import ConnectorWorker
from .model import Object, canonical, hash_bytes, require, MAX_BYTES
from .session import MAX_STATE


class ConnectorBatch:
    def __init__(self, worker):
        self.worker = worker

    @contextlib.contextmanager
    def locked(self):
        # Independent callbacks may finish together. Serialize only small local
        # journal updates, never hold the lock over a network/tool operation.
        w = self.worker
        fd = os.open(w.root / 'lock', os.O_RDWR | os.O_NOFOLLOW)
        deadline = time.monotonic() + 5
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    require(time.monotonic() < deadline, 'batch_journal_lock_timeout')
                    time.sleep(.01)
            s = json.loads(read_private_file(w.root / 'worker.json', MAX_STATE))
            require(s['pin'] == w.pin.oid and s['native_task_id'] == w.pin.body['identity']['native_task_id'],
                    'worker_runtime_pin_mismatch')
            yield s
        finally:
            os.close(fd)

    def _batch(self, s, batch_id):
        matches = [r['upload_batch'] for r in s['records'].values()
                   if r.get('upload_batch', {}).get('batch_id') == batch_id]
        require(len(matches) == 1, 'unknown_upload_batch')
        return matches[0]

    def start(self, seq):
        w = self.worker
        with self.locked() as s:
            record = s['records'].get(str(seq))
            require(record and record['phase'] == 'result_saved', 'saved_result_required')
            require(s['pending'] is None and 'upload_batch' not in record, 'upload_batch_already_attempted')
            objects = [w.object(record[k]) for k in ('claim', 'started', 'result')]
            require(len({o.oid for o in objects}) == 3, 'duplicate_batch_object')
            packets = [w.save_object(o) for o in objects]
            batch_id = hash_bytes(canonical({'pin': w.pin.oid, 'result': record['result'], 'kind': 'upload_batch'}))
            record['upload_batch'] = {'batch_id': batch_id,
                'items': {o.oid: {'packet': p, 'status': 'attempted'} for o, p in zip(objects, packets)}}
            w.save(s)  # all three attempts durable BEFORE a single native write
            return {'action': 'upload_batch_once', 'batch_id': batch_id, 'objects': packets,
                    'no_automatic_retry': True}

    def record_upload(self, batch_id, object_id, response):
        w = self.worker
        with self.locked() as s:
            batch = self._batch(s, batch_id)
            require(object_id in batch['items'], 'unknown_batch_object')
            item = batch['items'][object_id]
            require(item['status'] == 'attempted', 'upload_response_already_recorded')
            path = w.path('upload-response-' + object_id + '.json')
            write_new(path, canonical(response, max_bytes=MAX_STATE))
            item.update(status='response_saved', response=str(path))
            w.save(s)  # retain even malformed/unknown success responses
            value = structured(response)
            require(isinstance(value, dict) and value.get('success') is True and
                    isinstance(value.get('id'), str) and value['id'], 'upload_response_unknown')
            reference = {'object_id': object_id, 'locator': {'backend': 'drive',
                'folder_id': w.config['folder_id'], 'file_id': value['id']}}
            from .backend import validate_reference
            validate_reference(reference)
            require(all(other.get('reference', {}).get('locator', {}).get('file_id') != value['id']
                        for oid, other in batch['items'].items() if oid != object_id), 'duplicate_upload_file_id')
            item.update(status='uploaded', reference=reference)
            w.save(s)
            return {'reference': reference, 'no_automatic_retry': True}

    def verify_upload(self, batch_id, object_id, metadata, raw_path):
        w = self.worker
        with self.locked() as s:
            batch = self._batch(s, batch_id)
            require(object_id in batch['items'], 'unknown_batch_object')
            item = batch['items'][object_id]
            require(item['status'] == 'uploaded', 'uploaded_exact_id_required')
            raw = read_private_file(Path(raw_path), MAX_BYTES)
            expected = w.object(object_id)
            require(raw == expected.raw, 'immutable_upload_readback_mismatch')
            raw_file = w.path('verified-' + object_id + '.json')
            meta_file = w.path('verified-metadata-' + object_id + '.json')
            # Validate before publishing verified evidence. The received files may
            # be saved already, but are never considered evidence until this check.
            meta = structured(metadata)
            ref = item['reference']; loc = ref['locator']
            require(isinstance(meta, dict) and meta.get('id') == loc['file_id'] and
                    meta.get('title') == item['packet']['name'] and meta.get('mime_type') == 'application/json' and
                    type(meta.get('parent_ids')) is list and all(isinstance(x,str) for x in meta['parent_ids']) and
                    w.config['folder_id'] in meta['parent_ids'], 'normalized_metadata_scope_mismatch')
            write_new(raw_file, raw)
            write_new(meta_file, canonical(metadata, max_bytes=MAX_STATE))
            entry = {'reference': ref, 'file': str(raw_file), 'metadata': str(meta_file)}
            ConnectorEvidenceMessages(w.config['folder_id'], [entry]).fetch(ref)
            item.update(status='verified', evidence=entry)
            w.save(s)
            return {'verified': True, 'reference': ref, 'trash_state_verified': False}

    def finalize(self, batch_id, existing_entries=()):
        w = self.worker
        with self.locked() as s:
            batch = self._batch(s, batch_id)
            require(len(batch['items']) == 3 and all(i['status'] == 'verified' for i in batch['items'].values()),
                    'all_uploads_must_be_verified')
            wanted = set(batch['items'])
            require(isinstance(existing_entries, (list, tuple)), 'invalid_evidence_manifest')
            entries = list(existing_entries)
            require(all(e['reference']['object_id'] not in wanted for e in entries), 'duplicate_batch_evidence')
            entries += [i['evidence'] for i in batch['items'].values()]
            keys = [canonical(e['reference']) for e in entries]
            require(len(keys) == len(set(keys)), 'duplicate_manifest_reference')
            evidence = ConnectorEvidenceMessages(w.config['folder_id'], entries)
            for item in batch['items'].values():
                require(evidence.fetch(item['reference']).raw == w.object(item['packet']['object_id']).raw,
                        'immutable_upload_readback_mismatch')
            return {'verified': True, 'entries': entries, 'native_retry': False,
                    'next': 'fresh_control_read_then_result_cas'}

    def reserve_write(self, operation_id, *, max_age_seconds=10):
        w = self.worker
        require(type(max_age_seconds) in (int,float) and 0 < max_age_seconds <= 10, 'invalid_cas_dispatch_age')
        with self.locked() as s:
            pending = s['pending']
            require(pending and pending['operation_id'] == operation_id, 'exact_pending_cas_required')
            require(not pending.get('write_attempted'), 'cas_write_already_attempted')
            age = (time.monotonic_ns() - pending.get('prepared_monotonic_ns', 0)) / 1e9
            wall_age = time.time() - pending.get('prepared_at', 0)
            require(0 <= age <= max_age_seconds and 0 <= wall_age <= max_age_seconds, 'cas_dispatch_plan_expired')
            pending['write_attempted'] = True
            w.save(s)
            return {'reserved': True, 'operation_id': operation_id, 'no_automatic_retry': True}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation', choices=['start','record-upload','verify-upload','finalize','reserve-write'])
    p.add_argument('--root', required=True); p.add_argument('--native-task-id', required=True)
    for name in ('batch-id','object-id','response','metadata','raw-file','manifest','operation-id','save'):
        p.add_argument('--' + name)
    p.add_argument('--seq', type=int)
    a = p.parse_args(); os.umask(0o077)
    b = ConnectorBatch(ConnectorWorker(a.root, a.native_task_id))
    def read(path): return json.loads(read_private_file(Path(path), MAX_STATE))
    if a.operation == 'start': value = b.start(a.seq)
    elif a.operation == 'record-upload': value = b.record_upload(a.batch_id,a.object_id,read(a.response))
    elif a.operation == 'verify-upload': value = b.verify_upload(a.batch_id,a.object_id,read(a.metadata),a.raw_file)
    elif a.operation == 'finalize': value = b.finalize(a.batch_id,read(a.manifest) if a.manifest else [])
    else: value = b.reserve_write(a.operation_id)
    if a.save:
        write_new(Path(a.save),canonical(value,max_bytes=MAX_STATE))
        return {'saved':a.save,'verified':value.get('verified'),'action':value.get('action')}
    return value


if __name__ == '__main__': print(json.dumps(main(),ensure_ascii=False))
