"""Offline native connector batching faults. No Google or model calls."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from test_long_sessions import ConnectorWorkerTests, wire
from remote_transport.connector_batch import ConnectorBatch
from remote_transport.connector_files import capture, begin_capture, append_capture, seal_capture, input_chunk
from remote_transport.connector_worker import ConnectorWorker
from remote_transport.model import ProtocolError, canonical, Object


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.f=ConnectorWorkerTests('test_input_is_one_use_after_restart');self.f.setUp()
        self.b=ConnectorBatch(self.f.cw)
    def tearDown(self):self.f.tearDown()
    def ready(self):
        f=self.f
        f.controller.submit('test','key');f.cw.accept(*f.execute(f.begin()))
        f.cw.input(f.entries_for_all(),1);return f.cw.result(1,'synthetic answer')
    def upload(self,item,fid):
        return self.b.record_upload(self.batch['batch_id'],item['object_id'],{'structuredContent':{'success':True,'id':fid}})
    def verify(self,item,ref,**kwargs):
        meta={'id':ref['reference']['locator']['file_id'],'title':item['name'],
              'mime_type':'application/json','parent_ids':['folder']}
        meta.update(kwargs)
        return self.b.verify_upload(self.batch['batch_id'],item['object_id'],meta,item['path'])
    def test_reordered_concurrent_uploads_barrier_and_cas(self):
        self.ready();self.batch=self.b.start(1);objects=list(reversed(self.batch['objects']))
        def one(pair):
            n,item=pair;ref=self.upload(item,'parallel-'+str(n));self.verify(item,ref)
        with ThreadPoolExecutor(3) as pool:list(pool.map(one,enumerate(objects)))
        done=self.b.finalize(self.batch['batch_id'],self.f.entries)
        self.assertTrue(done['verified'])
        packet=self.f.cw.tick(self.f.resource(),done['entries'])
        self.assertEqual(packet['kind'],'result')
        self.assertTrue(self.b.reserve_write(packet['operation_id'])['reserved'])
        self.f.cw.accept(*self.f.execute(packet))
        self.assertEqual(self.f.cw.status()['records']['1']['phase'],'result_committed')
    def test_router_runtime_cannot_fall_back_to_serial_result_commit(self):
        self.ready()
        with self.f.cw.locked() as state:
            state['router_execution_mode']='router_parallel_cells_v1';self.f.cw.save(state)
        action=self.f.cw.tick(self.f.resource(),self.f.entries_for_all())
        self.assertEqual(action['reason'],'router_parallel_upload_batch_required')
        self.batch=self.b.start(1)
        for n,item in enumerate(self.batch['objects']):self.verify(item,self.upload(item,str(n)))
        done=self.b.finalize(self.batch['batch_id'],self.f.entries)
        self.assertEqual(self.f.cw.tick(self.f.resource(),done['entries'])['kind'],'result')

    def test_router_result_explicitly_directs_parallel_cell(self):
        with self.f.cw.locked() as state:
            state['router_execution_mode']='router_parallel_cells_v1';self.f.cw.save(state)
        packet=self.ready()
        self.assertEqual(packet['action'],'prepare_parallel_upload_cell')
        self.assertEqual(packet['phase'],'upload-commit')
        self.assertTrue(packet['upload_batch_required'])
        self.assertNotIn('objects',packet)

    def test_router_cell_rejects_mixed_release_sources(self):
        import subprocess,sys
        from remote_transport.router_join import _source_hashes
        w=self.f.cw
        with w.locked() as state:
            state['router_execution_mode']='router_parallel_cells_v1'
            state['router_source_hashes']=_source_hashes()
            state['router_source_hashes']['native_connector/runner.js']='0'*64
            w.save(state)
        manifest=w.root/'empty.json';manifest.write_text('[]');manifest.chmod(0o600)
        out=subprocess.run([sys.executable,'-B','-m','remote_transport.connector_cell','claim-begin',
            '--root',str(w.root),'--native-task-id','synthetic/native','--manifest',str(manifest),
            '--save',str(w.root/'cell.js')],capture_output=True,text=True)
        self.assertNotEqual(out.returncode,0)
        self.assertIn('router_parallel_runtime_source_changed',out.stderr)
        self.assertFalse((w.root/'cell.js').exists())

    def test_start_replay_after_restart_blocked(self):
        self.ready();self.batch=self.b.start(1)
        b=ConnectorBatch(ConnectorWorker(self.f.root/'connector','synthetic/native'))
        with self.assertRaisesRegex(ProtocolError,'already_attempted'):b.start(1)
    def test_partial_failure_never_crosses_barrier_or_tick(self):
        self.ready();self.batch=self.b.start(1);item=self.batch['objects'][0]
        self.verify(item,self.upload(item,'a'))
        with self.assertRaisesRegex(ProtocolError,'all_uploads'):self.b.finalize(self.batch['batch_id'],self.f.entries)
        self.assertEqual(self.f.cw.tick(self.f.resource(),self.f.entries)['action'],'reconcile_uploads')
    def test_duplicate_physical_id_and_conflicting_rebind_rejected(self):
        self.ready();self.batch=self.b.start(1);a,b,c=self.batch['objects']
        self.upload(a,'same')
        with self.assertRaisesRegex(ProtocolError,'duplicate_upload_file_id'):self.upload(b,'same')
        with self.assertRaisesRegex(ProtocolError,'already_recorded'):self.upload(a,'different')
        items=self.f.cw.status()['records']['1']['upload_batch']['items']
        self.assertEqual(items[a['object_id']]['reference']['locator']['file_id'],'same')
        self.assertEqual(items[b['object_id']]['status'],'response_saved')
    def test_unknown_malformed_response_is_preserved(self):
        self.ready();self.batch=self.b.start(1);item=self.batch['objects'][0]
        response={'structuredContent':{'success':False,'error':{'opaque':'private failure'}}}
        with self.assertRaisesRegex(ProtocolError,'upload_response_unknown'):
            self.b.record_upload(self.batch['batch_id'],item['object_id'],response)
        saved=self.f.cw.status()['records']['1']['upload_batch']['items'][item['object_id']]
        self.assertEqual(json.loads(Path(saved['response']).read_bytes()),response)
        with self.assertRaisesRegex(ProtocolError,'already_recorded'):self.upload(item,'new')
    def test_swapped_raw_wrong_metadata_and_mutated_evidence_fail(self):
        self.ready();self.batch=self.b.start(1);a,b,c=self.batch['objects'];ref=self.upload(a,'a')
        with self.assertRaisesRegex(ProtocolError,'metadata_scope'):self.verify(a,ref,id='wrong')
        with self.assertRaisesRegex(ProtocolError,'readback_mismatch'):
            self.b.verify_upload(self.batch['batch_id'],a['object_id'],{},b['path'])
        self.verify(a,ref)
        for n,item in enumerate((b,c)):self.verify(item,self.upload(item,str(n)))
        good=self.b.finalize(self.batch['batch_id'],self.f.entries)
        entry=[e for e in good['entries'] if e['reference']['object_id']==a['object_id']][0]
        Path(entry['file']).write_bytes(Path(b['path']).read_bytes())
        with self.assertRaises(ProtocolError):self.b.finalize(self.batch['batch_id'],self.f.entries)
    def test_duplicate_evidence_is_not_silently_overwritten(self):
        self.ready();self.batch=self.b.start(1)
        for n,item in enumerate(self.batch['objects']):self.verify(item,self.upload(item,str(n)))
        good=self.b.finalize(self.batch['batch_id'],self.f.entries)
        with self.assertRaisesRegex(ProtocolError,'duplicate_batch_evidence'):
            self.b.finalize(self.batch['batch_id'],good['entries'])
    def test_cas_marker_once_and_expired_plan(self):
        self.f.controller.submit('one','key')
        plan=self.f.cw.tick(self.f.resource(),self.f.entries_for_all())
        with patch('remote_transport.connector_batch.time.monotonic_ns',return_value=0):
            with self.assertRaisesRegex(ProtocolError,'plan_expired'):self.b.reserve_write(plan['operation_id'])
        self.assertTrue(self.b.reserve_write(plan['operation_id'])['reserved'])
        with self.assertRaisesRegex(ProtocolError,'already_attempted'):self.b.reserve_write(plan['operation_id'])
    def test_large_unicode_input_file_full_hash_and_one_use(self):
        f=self.f
        text='A\U0001f600\"\\\n'*12000
        f.controller.submit_request(wire(text),'key');f.cw.accept(*f.execute(f.begin()))
        packet=f.cw.input(f.entries_for_all(),1,expose_path=f.cw.root/'input.json')
        self.assertEqual(packet['action'],'native_input_file_once')
        self.assertNotIn('payload',packet)
        offset=0;pieces=[]
        while True:
            chunk=input_chunk(packet['path'],packet['sha256'],offset,8191)
            pieces.append(chunk['text']);offset=chunk['next_offset_chars']
            if chunk['eof']:break
        recovered=json.loads(''.join(pieces))
        self.assertEqual(recovered['payload']['responses_request']['input'][0]['content'][0]['text'],text)
        with self.assertRaisesRegex(ProtocolError,'one_use'):f.cw.input(f.entries,1)
        with self.assertRaisesRegex(ProtocolError,'hash_mismatch'):input_chunk(packet['path'],'0'*64)
    def test_failed_file_exposure_still_burns_permit(self):
        f=self.f;f.controller.submit('one','key');f.cw.accept(*f.execute(f.begin()))
        with self.assertRaisesRegex(ProtocolError,'input_file_must'):
            f.cw.input(f.entries_for_all(),1,expose_path=f.root/'outside.json')
        with self.assertRaisesRegex(ProtocolError,'one_use'):f.cw.input(f.entries,1)
    def test_json_capture_unicode_null_quotes_and_bounded_append(self):
        value={'a':None,'b':"'"*20000+'\U0001f600'*5000,'c':False}
        raw=json.dumps(value,ensure_ascii=False).encode()
        state=begin_capture(self.f.cw.root)
        for offset in range(0,len(raw),10000):
            state=append_capture(self.f.cw.root,state['path'],state['bytes'],raw[offset:offset+10000])
        saved=seal_capture(self.f.cw.root,state['path'])
        self.assertEqual(json.loads(Path(saved['path']).read_bytes()),value)
        with self.assertRaisesRegex(ProtocolError,'offset_mismatch'):
            append_capture(self.f.cw.root,state['path'],0,b'bad')


if __name__=='__main__':unittest.main()
