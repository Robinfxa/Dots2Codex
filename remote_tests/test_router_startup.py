"""Whole supervisor startup with in-memory provider ports. Never reaches Google."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
from remote_transport import router_mac as r
from remote_transport.router_bootstrap import (decode_block, block_for, worker_admitted,
    worker_polling, snapshot_from_document)
from remote_transport.model import canonical, hash_bytes
from remote_tests.test_router_lifecycle import document


class World:
    def __init__(self):
        self.docs={};self.raw={};self.creates=0;self.writes=0;self.unknown_create=False
    def create_document_once(self,folder,title):
        self.creates+=1
        if self.unknown_create:raise RuntimeError('lost create')
        did='doc'+str(self.creates);self.docs[did]=['\n',1]
        return did
    def get_document(self,did):return document(did,'t.0','r'+str(self.docs[did][1]),self.docs[did][0])
    def batch_update_document(self,did,requests,wc):
        value=self.docs[did];assert wc=={'requiredRevisionId':'r'+str(value[1])};self.writes+=1
        if 'insertText' in requests[0]:
            assert value[0]=='\n';value[0]=requests[0]['insertText']['text']+'\n';reply={}
        else:
            x=requests[0]['replaceAllText'];assert x['containsText']['text']==value[0][:-1]
            value[0]=x['replaceText']+'\n';reply={'replaceAllText':{'occurrencesChanged':1}}
        value[1]+=1
        return {'documentId':did,'replies':[reply],'writeControl':{'requiredRevisionId':'r'+str(value[1])}}
    def generate_id(self):return 'forward'
    def create_bytes(self,folder,name,raw,fid):self.raw[fid]=(folder,name,raw);return fid
    def get_metadata(self,fid):
        folder,name,_=self.raw[fid];return {'id':fid,'parents':[folder],'name':name,'trashed':False}
    def get_bytes(self,fid,limit):return self.raw[fid][2]
    def wait_for(self,docs,active,code,target,**kw):
        state=decode_block(self.docs[active['bootstrap_document_id']][0])
        if target=='WORKER_ADMITTED':
            raw=canonical({'contract':'dots-router-probe/1','bootstrap_id':state['bootstrap_id'],
                           'native_task_id':'/root/native'})
            name='dots2codex-router-probe-'+state['bootstrap_id']+'.json'
            self.raw['reverse']=('folder',name,raw)
            state=worker_admitted(state,join_code=code,native_task_id='/root/native',
                probe={'file_id':'reverse','name':name,'sha256':hash_bytes(raw)})
        else:state=worker_polling(state,join_code=code,native_task_id='/root/native',runtime_hash='a'*64)
        self.docs[active['bootstrap_document_id']][0]=block_for(state)
        self.docs[active['bootstrap_document_id']][1]+=1
        return snapshot_from_document(self.get_document(active['bootstrap_document_id']),active['bootstrap_document_id'],'t.0')


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.path=self.root/'active.json'
        self.args=types.SimpleNamespace(config=str(self.root/'config.json'),active=str(self.path),launch_codex=False)
        self.config={'folder_id':'folder','authorized_user_file':str(self.root/'auth.json'),'workdir':str(self.root),
            'mac_writer_identity':'mac','worker_writer_identity':'worker','seconds':14400,'max_requests':128,
            'scope':'responses_tools','port':0,'deadline':1800,'poll_interval':5,'heartbeat_interval':15,
            'bootstrap_ttl':1800,'bootstrap_poll_interval':5,'codex':'codex','expected_codex_version':r.EXPECTED_CODEX}
        r._private_json(self.args.config,self.config);self.world=World()
    def tearDown(self):self.tmp.cleanup()
    @contextlib.contextmanager
    def environment(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(r,'_set_google_env'))
            stack.enter_context(patch.object(r,'_copy_clipboard',return_value=False))
            stack.enter_context(patch.object(r.subprocess,'run',return_value=types.SimpleNamespace(stdout=r.EXPECTED_CODEX)))
            stack.enter_context(patch('examples.google_clients.create_docs_client',return_value=self.world))
            stack.enter_context(patch('examples.google_clients.create_drive_client',return_value=self.world))
            stack.enter_context(patch.object(r,'_wait_for',side_effect=self.world.wait_for))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            yield
    def launch(self,path,active,config):
        self.assertEqual(active['stage'],'CONSUMED')
        r._check_cancelled(active)
        self.assertNotEqual(active['bootstrap_document_id'],active['control_document_id'])
        ready={'base_url':'http://127.0.0.1:12345/v1'}
        return r._write_active(path,active,stage='READY'),ready
    def test_complete_startup_and_stop_through_fake_ports(self):
        with self.environment(),patch.object(r,'_launch_facade',side_effect=self.launch):
            result=r.start(self.args)
            self.assertTrue(result['router_ready']);self.assertEqual(self.world.creates,2)
            active=r._load_private_json(self.path)
            self.assertEqual(active['stage'],'READY')
            self.assertEqual(decode_block(self.world.docs[active['bootstrap_document_id']][0])['stage'],'CONSUMED')
            result=r.stop(self.args)
            self.assertTrue(result['closed']);self.assertTrue(result['process_stopped'])
            self.assertEqual(result['bootstrap_cleanup']['stage'],'CLOSED')
    def test_facade_failure_closes_authority_without_false_ready(self):
        with self.environment(),patch.object(r,'_launch_facade',side_effect=RuntimeError('synthetic_facade_timeout')):
            with self.assertRaisesRegex(RuntimeError,'synthetic_facade_timeout'):r.start(self.args)
        active=r._load_private_json(self.path)
        self.assertTrue(active['closed']);self.assertEqual(active['stage'],'CLOSED')
        self.assertTrue(active['control_close']['authoritative'])
    def test_unknown_document_create_preserves_recovery_and_never_retries(self):
        self.world.unknown_create=True
        with self.environment():
            with self.assertRaisesRegex(RuntimeError,'create_unknown'):r.start(self.args)
            with self.assertRaisesRegex(RuntimeError,'stale_or_incomplete'):r.start(self.args)
        self.assertEqual(self.world.creates,1)
        active=r._load_private_json(self.path)
        self.assertFalse(active['closed']);self.assertEqual(active['stage'],'RECOVERY_REQUIRED')
        self.assertEqual(r._load_private_json(Path(active['runtime'])/'create-control.json')['status'],'unknown')
    def test_stop_before_active_publication_cancels_first_or_replacement_start(self):
        for previous in (False,True):
            with self.subTest(previous=previous):
                path=self.root/('replacement.json' if previous else 'first.json')
                args=types.SimpleNamespace(config=self.args.config,active=str(path),launch_codex=False)
                if previous:
                    old=self.root/'old';old.mkdir(mode=0o700)
                    r._private_json(path,{'session_id':'old','runtime':str(old),'closed':True,
                        'process_stopped':True,'stage':'CLOSED','facade_pid':None})
                checking=threading.Event();release=threading.Event();errors=[];results=[]
                def version(*a,**kw):
                    checking.set()
                    if not release.wait(4):raise RuntimeError('test_sync_timeout')
                    return types.SimpleNamespace(stdout=r.EXPECTED_CODEX)
                def start():
                    try:r.start(args)
                    except Exception as exc:errors.append(str(exc))
                def stop():
                    try:results.append(r.stop(args))
                    except Exception as exc:errors.append('STOP:'+str(exc))
                with self.environment(),patch.object(r.subprocess,'run',side_effect=version):
                    a=threading.Thread(target=start);a.start();self.assertTrue(checking.wait(3))
                    b=threading.Thread(target=stop);b.start()
                    marker=Path(str(path)+'.stop-intent.json');until=time.monotonic()+3
                    while not marker.exists() and time.monotonic()<until:time.sleep(.01)
                    self.assertTrue(marker.exists());release.set();a.join(4);b.join(4)
                self.assertFalse(a.is_alive());self.assertFalse(b.is_alive())
                self.assertEqual(errors,['router_start_cancelled'])
                self.assertEqual(results[0]['stage'],'ABORTED')
                self.assertFalse(results[0]['closed']);self.assertEqual(self.world.creates,0)
                self.assertEqual(r._load_private_json(path)['lifecycle_intent'],r._load_private_json(marker)['intent_id'])

    def test_stop_waits_through_lease_before_intent_publication(self):
        for previous in (False,True):
            with self.subTest(previous=previous):
                path=self.root/('gap-replacement.json' if previous else 'gap-first.json')
                args=types.SimpleNamespace(config=self.args.config,active=str(path),launch_codex=False)
                if previous:
                    old=self.root/'gap-old';old.mkdir(mode=0o700)
                    r._private_json(path,{'session_id':'gap-old','runtime':str(old),'closed':True,
                        'process_stopped':True,'stage':'CLOSED','facade_pid':None})
                    r._private_json(Path(str(path)+'.start-intent.json'),{'intent_id':'old'})
                before=threading.Event();release=threading.Event();waiting=threading.Event();errors=[];results=[]
                original=r._private_json;signal_stop=r._signal_stop_intent
                def save(target,value):
                    if str(target)==str(path)+'.start-intent.json':
                        before.set()
                        if not release.wait(4):raise RuntimeError('test_sync_timeout')
                    return original(target,value)
                def signal(target):waiting.set();return signal_stop(target)
                def start():
                    try:r.start(args)
                    except Exception as exc:errors.append(str(exc))
                def stop():
                    try:results.append(r.stop(args))
                    except Exception as exc:errors.append('STOP:'+str(exc))
                with self.environment(),patch.object(r,'_private_json',side_effect=save), \
                     patch.object(r,'_signal_stop_intent',side_effect=signal), \
                     patch.object(r,'_launch_facade',side_effect=self.launch):
                    a=threading.Thread(target=start);a.start();self.assertTrue(before.wait(3))
                    b=threading.Thread(target=stop);b.start();self.assertTrue(waiting.wait(3))
                    self.assertTrue(b.is_alive());release.set();a.join(4);b.join(4)
                self.assertFalse(a.is_alive());self.assertFalse(b.is_alive())
                self.assertFalse(any(e.startswith('STOP:') for e in errors),errors)
                self.assertEqual(len(results),1)
                self.assertIn(results[0]['stage'],{'ABORTED','CLOSED'})
                self.assertNotEqual(r._load_private_json(path)['stage'],'READY')

    def test_stop_before_launch_blocks_ready_then_fences_control(self):
        def cancelled(path,active,config):
            r._request_stop(active);r._check_cancelled(active)
        with self.environment(),patch.object(r,'_launch_facade',side_effect=cancelled):
            with self.assertRaisesRegex(Exception,'cancelled'):r.start(self.args)
        active=r._load_private_json(self.path)
        self.assertTrue(active['closed']);self.assertEqual(active['stage'],'CLOSED')


if __name__=='__main__':unittest.main()
