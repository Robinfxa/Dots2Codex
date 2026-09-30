"""Independent process-boundary failure checks; never connects to Drive/native."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE=Path(__file__).resolve()
REPO=next(p for p in HERE.parents if (p/'remote_transport'/'session.py').is_file())


class IndependentCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.pin=self.root/'pin.json';self.journal=self.root/'worker';self.objects=self.root/'objects'
        self.call('new-deployment','--pin',self.pin,'--session','audit_cli','--native-task-id','synthetic/native/task')
        self.call('provision-journal','--pin',self.pin,'--journal',self.journal,'--role','worker')
        self.call('init-local-store','--object-root',self.objects)
    def tearDown(self):self.tmp.cleanup()
    def call(self,*args,ok=True):
        out=subprocess.run([sys.executable,'-m','remote_transport.cli',*map(str,args)],cwd=REPO,
                           capture_output=True,text=True,timeout=10,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(out.returncode,0 if ok else 1,out.stdout+out.stderr)
        return json.loads(out.stdout)
    def test_transport_is_explicit_and_never_falls_back(self):
        out=self.call('worker-next','--pin',self.pin,'--journal',self.journal,'--save',self.root/'permit.json',ok=False)
        self.assertEqual(out['error'],'explicit_transport_required')
        self.assertFalse((self.root/'permit.json').exists())
    def test_drive_requires_reviewed_factory_and_folder(self):
        out=self.call('worker-next','--pin',self.pin,'--journal',self.journal,'--transport','drive',
                      '--save',self.root/'permit.json',ok=False)
        self.assertEqual(out['error'],'drive_client_and_folder_required')
        self.assertFalse((self.root/'permit.json').exists())
    def test_pending_never_manufactures_permit(self):
        out=self.call('worker-next','--pin',self.pin,'--journal',self.journal,'--transport','localfs',
                      '--object-root',self.objects,'--wait','0','--save',self.root/'permit.json')
        self.assertEqual(out,{'status':'pending','proves_unstarted':False})
        self.assertFalse((self.root/'permit.json').exists())
    def test_existing_permit_file_is_never_overwritten(self):
        path=self.root/'permit.json';path.write_text('keep existing');path.chmod(0o600)
        out=self.call('worker-next','--pin',self.pin,'--journal',self.journal,'--transport','localfs',
                      '--object-root',self.objects,'--wait','0','--save',path,ok=False)
        self.assertEqual(out['error'],'new_permit_path_required')
        self.assertEqual(path.read_text(),'keep existing')
    def test_missing_journal_never_reprovisions(self):
        missing=self.root/'missing'
        out=self.call('worker-next','--pin',self.pin,'--journal',missing,'--transport','localfs',
                      '--object-root',self.objects,'--wait','0','--save',self.root/'permit.json',ok=False)
        self.assertEqual(out['error'],'FileNotFoundError');self.assertFalse(missing.exists())


if __name__=='__main__':unittest.main(verbosity=2)
