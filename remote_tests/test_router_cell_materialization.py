"""Actual Router materialize -> generated-cell boundary; synthetic providers only.

Neither generated JavaScript nor any connector/native operation is executed.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from remote_transport.model import canonical, deployment
from remote_transport.router_bootstrap import initial_state, root_context, bundle_ready
from remote_transport.router_join import _source_hashes
import test_router_bootstrap as router_fixtures
from test_router_bootstrap import private_json
from test_model_selection import selected

RELEASE = Path(__file__).resolve().parents[1]
SIX_SOURCES = {'remote_transport/connector_cell.py', 'remote_transport/connector_worker.py',
               'native_connector/runner.js', 'native_connector/tool_adapter.js',
               'remote_transport/selection.py', 'remote_transport/native_capabilities.json'}


class MaterializedRouterCellTests(unittest.TestCase):
    def materialize(self, selected_mode):
        f = router_fixtures.RouterJoinDurabilityTests('test_materialization_reservation_blocks_second_root_and_supports_exact_resume')
        f.setUp(); self.addCleanup(f.doCleanups)
        if selected_mode:
            old=root_context(f.state);control=old['control'];selection=selected(effort='max')
            f.state=initial_state(bootstrap_id=old['bootstrap_id'],session_id=old['session_id'],
                created=old['created'],expires=old['expires'],join_code=f.code,folder_id=old['folder_id'],
                control_document_id=control['document_id'],control_tab_id=control['tab_id'],
                control_id=control['control_id'],mac_writer_identity=control['mac_writer_identity'],
                worker_writer_identity=control['worker_writer_identity'],bootstrap_document_id='bootDoc',
                bootstrap_tab_id='t.0',forward_probe=f.forward,required_selection=selection)
            def admission_plan():
                message=f.directory/'worker-message.txt';message.write_text('Synthetic native admission');message.chmod(0o600)
                plan=f.directory/'native-plan.json'
                arguments=f.call('plan-native',f.state,task_name=f.native.rsplit('/',1)[-1],
                    message_file=message,save=plan)['arguments']
                native_receipt=f.directory/'native-receipt.json'
                f.call('record-native',f.state,plan_file=plan,actual_arguments=f.file(arguments),
                    native_result=f.file({'task_name':f.native}),save=native_receipt)
                probe=f.call('prepare-probe',f.state,save=f.directory/'probe.json')
                packet=f.directory/'admit-plan.json'
                f.call('plan-admit',f.state,writer_identity=f.config['writer_identity'],
                    probe_file_id='probeFile',probe_name=probe['file_name'],probe_sha256=probe['sha256'],
                    admission_receipt=native_receipt,save=packet)
                return packet,json.loads(packet.read_bytes())['expected_state']
            def ready(admitted):
                pin=deployment('router-session',f.native,seconds=600,max_requests=128,scope='responses_tools',
                    now=f.now+2,inference={'selection':selection,'admission':admitted['worker']['admission']})
                state=bundle_ready(admitted,join_code=f.code,pin_raw=pin.raw,config_raw=canonical(f.config),
                    deployment_hash=pin.oid,now=max(f.now+3,admitted['worker']['admitted_at']))
                return state,pin
            f.admission_plan=admission_plan;f.ready=ready
        ready,pin=f.verified_bundle();runtime=f.directory/'runtime'
        output=f.call('materialize',ready,root=runtime)
        self.assertEqual(set(output['source_hashes']),SIX_SOURCES)
        self.assertEqual(output['source_hashes'],_source_hashes())
        self.assertEqual(json.loads((runtime/'worker.json').read_bytes())['router_source_hashes'],output['source_hashes'])
        self.assertEqual('inference' in pin.body['payload'],selected_mode)
        return f,runtime,output

    def generate(self,f,runtime,phase,*,release=RELEASE,filename='cell.js'):
        destination=runtime/filename
        argv=[sys.executable,'-B','-m','remote_transport.connector_cell',phase,
              '--root',str(runtime),'--native-task-id',f.native,
              '--manifest',str(runtime/'router-empty-manifest.json'),'--save',str(destination)]
        if phase=='upload-commit':argv.extend(['--seq','1'])
        env=dict(os.environ);env.pop('PYTHONPATH',None);env['PYTHONDONTWRITEBYTECODE']='1'
        result=subprocess.run(argv,cwd=release,env=env,text=True,capture_output=True)
        return result,destination

    def test_actual_legacy_materialization_generates_both_cells(self):
        self.check_valid(False)

    def test_actual_selected_materialization_generates_both_cells(self):
        self.check_valid(True)

    def check_valid(self,selected_mode):
        f,runtime,record=self.materialize(selected_mode)
        originals={name:(runtime/name).read_bytes() for name in ('worker.json','pin.json','router-materialization.json')}
        for phase in ('claim-begin','upload-commit'):
            with self.subTest(phase=phase):
                result,path=self.generate(f,runtime,phase,filename=phase+'.js')
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(json.loads(result.stdout)['connector_calls'],0)
                self.assertIn('createNativeConnectorRunner',path.read_text())
        for name,raw in originals.items():self.assertEqual((runtime/name).read_bytes(),raw)
        self.assertEqual(record['source_hashes'],_source_hashes())

    def test_each_of_six_recorded_digests_rejected_in_both_modes(self):
        for selected_mode in (False,True):
            f,runtime,_=self.materialize(selected_mode)
            original=json.loads((runtime/'worker.json').read_bytes())
            for name in sorted(SIX_SOURCES):
                for phase in ('claim-begin','upload-commit'):
                    with self.subTest(selected=selected_mode,source=name,phase=phase):
                        corrupt=copy.deepcopy(original);corrupt['router_source_hashes'][name]='0'*64
                        private_json(runtime/'worker.json',corrupt)
                        before=(runtime/'worker.json').read_bytes()
                        result,path=self.generate(f,runtime,phase)
                        self.assertNotEqual(result.returncode,0)
                        self.assertIn('router_parallel_runtime_source_changed',result.stderr)
                        self.assertFalse(path.exists());self.assertEqual((runtime/'worker.json').read_bytes(),before)
            private_json(runtime/'worker.json',original)

    def test_exact_source_key_set_cannot_drop_model_binding_or_add_paths(self):
        f,runtime,_=self.materialize(True);original=json.loads((runtime/'worker.json').read_bytes())
        changes=[]
        for name in SIX_SOURCES:
            bad=copy.deepcopy(original);del bad['router_source_hashes'][name];changes.append(bad)
        four=copy.deepcopy(original)
        del four['router_source_hashes']['remote_transport/selection.py']
        del four['router_source_hashes']['remote_transport/native_capabilities.json'];changes.append(four)
        extra=copy.deepcopy(original);extra['router_source_hashes']['../not-a-release-file']='0'*64;changes.append(extra)
        for bad in changes:
            private_json(runtime/'worker.json',bad)
            result,path=self.generate(f,runtime,'claim-begin')
            self.assertNotEqual(result.returncode,0);self.assertIn('router_parallel_runtime_source_changed',result.stderr)
            self.assertFalse(path.exists())

    def test_actual_source_bytes_of_every_bound_file_are_checked(self):
        f,runtime,_=self.materialize(True)
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        clone=Path(temp.name)/'release'
        shutil.copytree(RELEASE,clone,ignore=shutil.ignore_patterns('__pycache__','.git','*.pyc'))
        for name in sorted(SIX_SOURCES):
            with self.subTest(source=name):
                path=clone/name;original=path.read_bytes();path.write_bytes(original+b'\n')
                try:
                    result,destination=self.generate(f,runtime,'claim-begin',release=clone)
                    self.assertNotEqual(result.returncode,0)
                    self.assertIn('router_parallel_runtime_source_changed',result.stderr)
                    self.assertFalse(destination.exists())
                finally:path.write_bytes(original)

    def test_pre_fix_pinned_cell_digest_is_not_silently_migrated(self):
        f,runtime,_=self.materialize(True)
        state=json.loads((runtime/'worker.json').read_bytes())
        # Actual published pre-fix connector_cell.py digest, retained as a
        # portable migration fixture rather than depending on another checkout.
        digest='223ebc79ed7fa8dd8d83b2ac422e1e771f7711f016fce19559dd00d899a8cb57'
        self.assertNotEqual(digest,_source_hashes()['remote_transport/connector_cell.py'])
        state['router_source_hashes']['remote_transport/connector_cell.py']=digest
        private_json(runtime/'worker.json',state);before=(runtime/'worker.json').read_bytes()
        result,path=self.generate(f,runtime,'claim-begin')
        self.assertNotEqual(result.returncode,0);self.assertFalse(path.exists())
        self.assertIn('router_parallel_runtime_source_changed',result.stderr)
        self.assertEqual((runtime/'worker.json').read_bytes(),before)
