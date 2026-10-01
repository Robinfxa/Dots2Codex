"""Actual parent/child helper CLI adapter; synthetic Google/native evidence only."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from remote_tests import test_global_control as fixtures
from remote_transport import global_control as q
from remote_transport.global_gateway import private_write
from remote_transport.model import ProtocolError, canonical
from remote_transport.router_join import JoinLedger


class GlobalChildAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.GlobalControlTests('test_join_heartbeat_claim_and_signed_projection')
        self.f.setUp();self.addCleanup(self.f.tearDown)
        self.serial=0
        self.code=self.file(self.f.code.encode())
        self.f.bridge.bootstrap_seconds=60
        self.rid,_,_=self.f.demand()
        self.f.execute('claim',self.rid);self.f.execute('begin',self.rid)
        self.plan=self.file_path()
        out=self.parent('plan-native',route_id=self.rid,package_root=self.f.package,save=self.plan)
        self.arguments=out['arguments'];self.native='/root/offline/'+self.arguments['task_name']
        self.receipt_path=self.file_path()
        self.parent('record-native',plan_file=self.plan,actual_arguments=self.file(self.arguments),
                    native_result=self.file({'task_name':self.native}),save=self.receipt_path)
        self.receipt=json.loads(self.receipt_path.read_bytes())
        self.directory=self.f.ledger.child_state_dir(self.rid)
        self.bootstrap=self.f.bridge.read().state['logical']['demands'][self.rid]['child_bootstrap']
        self.child_code=self.file(q.child_code(self.f.code,self.f.gen,self.rid).encode())

    def file_path(self):
        self.serial+=1;return self.f.root/f'child-test-{self.serial}.json'

    def file(self,value):
        path=self.file_path();private_write(path,value if isinstance(value,bytes) else canonical(value));return path

    def cli(self,module,operation,common,*,success=True,**kwargs):
        args=[sys.executable,'-B','-m','remote_transport.'+module,operation,*common]
        for key,value in kwargs.items():args.extend(['--'+key.replace('_','-'),str(value)])
        env=dict(os.environ);env['PYTHONDONTWRITEBYTECODE']='1'
        result=subprocess.run(args,cwd=self.f.package,env=env,text=True,capture_output=True)
        self.assertEqual(result.returncode==0,success,(result.stdout,result.stderr))
        return json.loads(result.stdout)

    def parent(self,operation,**kwargs):
        common=['--snapshot',str(self.file(self.f.google.get_document(self.f.initial['document_id']))),
                '--document-id',self.f.initial['document_id'],'--tab-id','t.0','--join-code-file',str(self.code),
                '--state-dir',str(self.f.ledger.root),'--native-task-id',self.f.ledger.identity]
        return self.cli('global_native',operation,common,**kwargs)

    def child(self,operation,*,directory=None,**kwargs):
        common=['--snapshot',str(self.file(self.f.google.get_document(self.bootstrap['bootstrap_document_id']))),
                '--document-id',self.bootstrap['bootstrap_document_id'],'--tab-id','t.0',
                '--join-code-file',str(self.child_code),'--state-dir',str(directory or self.directory),
                '--native-task-id',self.native]
        return self.cli('router_join',operation,common,**kwargs)

    def admitted(self):
        path=self.file_path();out=self.parent('plan-admitted',route_id=self.rid,save=path)
        response=self.f.google.batch_update_document(**out['tool_arguments'])
        self.parent('verify',plan_file=path,response=self.file(response),
                    readback=self.file(self.f.google.get_document(self.f.initial['document_id'])))

    def import_admission(self,**kwargs):
        return self.parent('import-child-admission',route_id=self.rid,
                           admission_receipt=self.receipt_path,child_native_task_id=self.native,**kwargs)

    def plan_admit(self,*,directory=None,success=True):
        probe=self.child('prepare-probe',directory=directory,save=self.file_path())
        plan=self.file_path()
        result=self.child('plan-admit',directory=directory,writer_identity=self.bootstrap['control']['worker_writer_identity'],
                         probe_file_id='synthetic-probe',probe_name=probe['file_name'],probe_sha256=probe['sha256'],
                         admission_receipt=self.receipt_path,save=plan,success=success)
        return result,plan

    def test_two_distinct_ledgers_and_real_cli_plan_admit(self):
        self.admitted()
        # This was the live defect: a genuine parent receipt alone cannot pass
        # the child's existing selected-v3 admission fence.
        result,path=self.plan_admit(directory=self.f.root/'unimported-child',success=False)
        self.assertEqual(result['error'],'parent_native_admission_record_required');self.assertFalse(path.exists())
        result=self.import_admission();self.assertTrue(result['imported']);self.assertFalse(result['reconciled_existing'])
        self.assertEqual(Path(result['child_state_dir']),self.directory)
        parent=json.loads(self.f.ledger.path.read_bytes())
        ledger=JoinLedger(self.directory,self.bootstrap);child=json.loads(ledger.path.read_bytes())
        self.assertNotEqual(ledger.path,self.f.ledger.path)
        self.assertNotIn('spawns',child);self.assertNotIn('root',child)
        self.assertEqual(child['native_admission']['receipt'],self.receipt)
        self.assertEqual(parent['spawns'][self.rid]['child_admission_import']['status'],'imported')
        self.assertNotIn(self.f.code,ledger.path.read_text())
        result,path=self.plan_admit();self.assertEqual(result['stage'],'WORKER_ADMITTED')
        response=self.f.google.batch_update_document(**result['tool_arguments'])
        self.child('verify',plan_file=path,response=self.file(response),
                   readback=self.file(self.f.google.get_document(self.bootstrap['bootstrap_document_id'])))
        self.assertEqual(json.loads(ledger.path.read_bytes())['operations']['admit']['status'],'verified')
        self.assertFalse(self.receipt['underlying_model_verified'])
        self.assertIn(str(self.directory),self.arguments['message'])
        self.assertIn('import-child-admission',self.arguments['message'])

    def test_import_requires_verified_admitted_cas(self):
        out=self.import_admission(success=False)
        self.assertEqual(out['error'],'global_admitted_owned_child_required')
        path=self.file_path();out=self.parent('plan-admitted',route_id=self.rid,save=path)
        self.f.google.batch_update_document(**out['tool_arguments'])
        out=self.import_admission(success=False)
        self.assertEqual(out['error'],'global_unresolved_cas_readonly_reconciliation_required')
        self.assertEqual(list(self.directory.iterdir()),[])

    def test_exact_native_identity_and_receipt_path_required(self):
        self.admitted()
        out=self.parent('import-child-admission',route_id=self.rid,admission_receipt=self.receipt_path,
                        child_native_task_id='/root/other',success=False)
        self.assertEqual(out['error'],'global_child_actual_admission_mismatch')
        out=self.parent('import-child-admission',route_id=self.rid,admission_receipt=self.file(self.receipt),
                        child_native_task_id=self.native,success=False)
        self.assertEqual(out['error'],'global_child_actual_admission_mismatch')
        self.assertEqual(list(self.directory.iterdir()),[])

    def test_altered_receipt_and_signed_queue_rejected(self):
        self.admitted();changed=copy.deepcopy(self.receipt);changed['submitted_reasoning_effort']='max'
        private_write(self.receipt_path,canonical(changed))
        self.assertEqual(self.import_admission(success=False)['error'],'global_child_actual_admission_mismatch')
        private_write(self.receipt_path,canonical(self.receipt))
        source=self.f.bridge.read();state=source.state
        state['logical']['demands'][self.rid]['controller_epoch']='0'*32
        corrupted=q.Snapshot(source.document_id,source.tab_id,source.revision_id,q.block(state))
        with self.assertRaisesRegex(ProtocolError,'global_projection_mismatch'):
            self.f.ledger.import_child_admission(corrupted,self.rid,self.receipt_path,self.native)
        self.assertEqual(list(self.directory.iterdir()),[])

    def test_exact_repeat_is_readback_and_cannot_seed_replacement(self):
        self.admitted();self.import_admission()
        ledger=JoinLedger(self.directory,self.bootstrap);before=ledger.path.read_bytes()
        repeat=self.import_admission();self.assertTrue(repeat['reconciled_existing'])
        self.assertEqual(before,ledger.path.read_bytes())
        ledger.path.unlink()  # Simulate lost child evidence; never reconstruct it.
        out=self.import_admission(success=False)
        self.assertEqual(out['error'],'parent_child_import_outcome_unknown_no_replay')
        self.assertFalse(ledger.path.exists())
        out=self.parent('plan-native',route_id=self.rid,package_root=self.f.package,save=self.file_path(),success=False)
        self.assertEqual(out['error'],'global_native_attempt_already_reserved_no_replay')

    def test_interrupted_import_before_child_write_stays_burned(self):
        self.admitted()
        with patch.object(JoinLedger,'import_parent_admission',side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):
                self.f.ledger.import_child_admission(self.f.bridge.read(),self.rid,self.receipt_path,self.native)
        saved=json.loads(self.f.ledger.path.read_bytes())
        self.assertEqual(saved['spawns'][self.rid]['child_admission_import']['status'],'reserved_outcome_unknown')
        self.assertEqual(self.import_admission(success=False)['error'],'parent_child_import_outcome_unknown_no_replay')

    def test_existing_child_directory_rejected(self):
        self.admitted();self.child('inspect')
        self.assertEqual(self.import_admission(success=False)['error'],'fresh_parent_child_ledger_required')

    def test_symlink_destination_rejected(self):
        self.admitted();self.directory.rmdir();target=self.f.root/'other-private';target.mkdir(mode=0o700)
        self.directory.symlink_to(target,target_is_directory=True)
        self.assertEqual(self.import_admission(success=False)['error'],'symlink_path_rejected')
        self.assertEqual(list(target.iterdir()),[])

    def test_expired_bootstrap_with_live_controller_rejected(self):
        self.admitted();expires=self.bootstrap['expires']
        with patch('time.time',return_value=expires):
            with self.assertRaisesRegex(ProtocolError,'bootstrap_expired'):
                self.f.ledger.import_child_admission(self.f.bridge.read(),self.rid,self.receipt_path,self.native)

    def test_closed_queue_rejected(self):
        self.admitted();self.f.bridge.event('close',{'confirm':True})
        self.assertEqual(self.import_admission(success=False)['error'],'global_queue_closed')
        self.assertEqual(list(self.directory.iterdir()),[])

    def test_crash_after_child_save_only_reconciles_exact_readback(self):
        self.admitted();original=self.f.ledger.save
        def fail_final_save(saved):
            pending=saved['spawns'][self.rid].get('child_admission_import')
            if pending and pending['status']=='imported':raise OSError('synthetic final save failure')
            return original(saved)
        with patch.object(self.f.ledger,'save',side_effect=fail_final_save):
            with self.assertRaises(OSError):
                self.f.ledger.import_child_admission(self.f.bridge.read(),self.rid,self.receipt_path,self.native)
        ledger=JoinLedger(self.directory,self.bootstrap);before=ledger.path.read_bytes()
        saved=json.loads(self.f.ledger.path.read_bytes())
        self.assertEqual(saved['spawns'][self.rid]['child_admission_import']['status'],'reserved_outcome_unknown')
        self.assertTrue(self.import_admission()['reconciled_existing'])
        self.assertEqual(before,ledger.path.read_bytes())


if __name__=='__main__':unittest.main()
