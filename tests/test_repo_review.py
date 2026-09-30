import copy
import json
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import repo_fetch as fetch
import repo_review as rr
import probe
import broker_bootstrap as broker
from portable import init, Deployment, FileQueue, QueueError, protocol
from test_tool_probe import request

SHA = 'a' * 40

def intent(suffix):
    return dict(kind='function_call', name='exec_command', arguments=dict(cmd=rr.command(suffix), **rr.FIXED_ARGS))

def wrapper(body, code=0):
    return 'Wall time: 0.1 seconds\nProcess exited with code ' + str(code) + '\nOutput:\n' + json.dumps(body) + '\n'

class RepoReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name) / 'root'
        init(self.root, 'owner', seconds=60, scope='repo_review', max_requests=16)
        self.d = Deployment(self.root)
        probe.observe(self.d,'desktop');probe.offer(self.d);probe.observe(self.d,'broker');probe.answer(self.d);probe.verify(self.d)
        self.q = FileQueue(self.d.queue_root, create=True, owner='owner', session='session_' + self.d.m['run_id'], max_jobs=16)
        protocol.atomic_json(self.d.queue_root/'ready.json',dict(version=1,instance=uuid.uuid4().hex,deadline=time.time()+60))
        self.a = self.d.assign('owner','broker','b',native_attested=True)
        self.req = request('review')
    def tearDown(self):
        self.q.close();self.tmp.cleanup()
    def start(self):
        self.q.enqueue(self.req, owner='owner', session=self.q.meta['session'], timeout=30)
        t = broker.claim(self.d,self.a,0,10);broker.begin(self.d,self.a,t);return t
    def err(self, fn, *args, **kwargs):
        with self.assertRaises(QueueError): fn(*args, **kwargs)
    def step(self, suffix, ok=True):
        t=self.start(); result=intent(suffix);broker.complete(self.d,self.a,t,result)
        self.q.delivery(t['job']['id'],owner='owner',session=self.q.meta['session'],value='delivered')
        action=rr.parse_command(result['arguments'])
        body=dict(contract=fetch.CONTRACT,repo=fetch.REPO,ok=ok,request=action)
        if ok and suffix=='tree':body.update(commit=SHA,files=[dict(path='README.md',mode='100644')])
        elif ok:body.update(commit=SHA,path='README.md',text='actual fixture text')
        else:body.update(error='network_unavailable')
        call=protocol.validate_result(result,self.req,t['job']['id'])
        self.req['input'] += [call,dict(type='function_call_output',call_id=call['call_id'],output=wrapper(body,0 if ok else 1))]
        return t,result
    def test_multicall_then_final(self):
        self.step('tree');self.step('read '+SHA+' README.md 1 100');self.step('search '+SHA+' README.md bridge');self.step('read '+SHA+' README.md 101 100')
        t=self.start();r=dict(kind='message',text='中文总结，来源README.md')
        self.assertEqual(broker.complete(self.d,self.a,t,r)['state'],'completed')
        self.assertTrue(broker.complete(self.d,self.a,t,r)['idempotent'])
    def test_first_read_denied(self):
        t=self.start();self.err(broker.complete,self.d,self.a,t,intent('read '+SHA+' README.md 1 10'))
    def test_failure_allows_only_final(self):
        self.step('tree',ok=False);t=self.start()
        self.err(broker.complete,self.d,self.a,t,intent('tree'))
        self.assertEqual(broker.complete(self.d,self.a,t,dict(kind='message',text='网络受阻，无法读取仓库'))['state'],'completed')
    def test_pinned_commit_and_tree_path(self):
        self.step('tree');t=self.start()
        for suffix in ['read '+'b'*40+' README.md 1 10','read '+SHA+' missing.py 1 10','tree']:
            self.err(broker.complete,self.d,self.a,t,intent(suffix))
    def test_reject_changed_missing_duplicate_history(self):
        self.step('tree');original=copy.deepcopy(self.req)
        for change in [lambda x:x['input'].pop(),lambda x:x['input'].append(copy.deepcopy(x['input'][-1])),lambda x:x['input'][-1].update(call_id='wrong')]:
            self.req=copy.deepcopy(original);change(self.req)
            with self.d.locked() as s:reservations=s['repo_review']['calls']
            self.err(rr.history,self.d,self.req,reservations)
    def test_prior_receipt_is_immutable(self):
        self.step('tree');self.step('read '+SHA+' README.md 1 100')
        body=json.loads(self.req['input'][2]['output'].split('Output:\n',1)[1])
        body['commit']='b'*40
        self.req['input'][2]['output']=wrapper(body)
        t=self.start();self.err(broker.complete,self.d,self.a,t,intent('read '+SHA+' README.md 101 100'))
    def test_command_grammar(self):
        for suffix in ['tree; id','tree\n','read '+SHA+' ../secret 1 10','read '+SHA+' .env 1 10','read '+SHA+' README.md 0 10','read '+SHA+' README.md 1 151','search '+SHA+' README.md $(id)']:
            self.err(rr.parse_command,intent(suffix)['arguments'])
        for key,value in [('login',True),('sandbox_permissions','require_escalated'),('shell','bash'),('workdir','/tmp')]:
            args=intent('tree')['arguments'];args[key]=value;self.err(rr.parse_command,args)
    def test_crash_reservation_retry(self):
        t=self.start();r=intent('tree')
        with patch.object(FileQueue,'complete',side_effect=OSError('injected fixture failure')):
            with self.assertRaises(OSError):broker.complete(self.d,self.a,t,r)
        self.assertEqual(broker.complete(self.d,self.a,t,r)['state'],'completed')
        self.assertTrue(broker.complete(self.d,self.a,t,r)['idempotent'])
    def test_early_success_final_denied(self):
        self.step('tree');t=self.start();self.err(broker.complete,self.d,self.a,t,dict(kind='message',text='premature'))
    def test_limits_remain_scope_specific(self):
        self.err(init,Path(self.tmp.name)/'bad','owner',60,16,scope='text_only')
    def test_fetch_tree_filters(self):
        fixtures=[dict(full_name=fetch.REPO,private=False,default_branch='main'),dict(sha=SHA),dict(tree=[dict(path='README.md',type='blob',mode='100644',size=12),dict(path='secret',type='blob',mode='120000',size=10),dict(path='submodule',type='commit',mode='160000',size=1)])]
        with patch.object(fetch,'fetch',side_effect=[json.dumps(x).encode() for x in fixtures]):out=fetch.run(dict(action='tree'))
        self.assertEqual(out['files'],[dict(path='README.md',bytes=12,mode='100644')])
    def test_fetch_read_and_search(self):
        with patch.object(fetch,'fetch',return_value=b'first\nbridge actual\nthird\n'):
            out=fetch.run(fetch.parse(['search',SHA,'README.md','bridge']))
            self.assertEqual(out['text'],'2: bridge actual')
            out=fetch.run(fetch.parse(['read',SHA,'README.md','2','1']))
            self.assertEqual(out['text'],'2: bridge actual')
    def test_output_json_duplicates_and_exit_mismatch(self):
        self.err(rr.receipt,'running unknown',{'action':'tree'})
        body=dict(contract=fetch.CONTRACT,repo=fetch.REPO,ok=True,request={'action':'tree'},commit=SHA,files=[])
        self.err(rr.receipt,wrapper(body,1),{'action':'tree'})
        with self.assertRaises(fetch.FetchError):fetch.strict_json('{"a":1,"a":2}')
if __name__=='__main__':unittest.main()
