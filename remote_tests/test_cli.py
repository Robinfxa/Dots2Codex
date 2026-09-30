import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT=Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def test_separate_facade_and_worker_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def cmd(*args):
                out=subprocess.run([sys.executable,'-m','remote_transport.cli',*map(str,args)],
                    cwd=ROOT,text=True,capture_output=True,timeout=8)
                self.assertEqual(out.returncode,0,out.stdout+out.stderr)
                return json.loads(out.stdout)
            pin=root/'pin.json';store=root/'objects';cj=root/'controller';wj=root/'worker';ready=root/'ready.json'
            cmd('new-deployment','--pin',pin,'--session','cli-test','--native-task-id','synthetic/native','--seconds',60)
            cmd('init-local-store','--object-root',store)
            for role,path in [('controller',cj),('worker',wj)]:
                cmd('provision-journal','--pin',pin,'--journal',path,'--role',role)
            common=['--pin',pin,'--transport','localfs','--object-root',store]
            cmd('publish-deployment','--journal',cj,*common)
            server=subprocess.Popen([sys.executable,'-m','remote_transport.cli','serve','--journal',str(cj),
                '--ready',str(ready),'--deadline','6',*map(str,common)],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                end=time.monotonic()+5
                while not ready.exists() and server.poll() is None and time.monotonic()<end:time.sleep(.025)
                self.assertTrue(ready.exists(),'facade did not become ready')
                url=json.loads(ready.read_text())['base_url'];port=int(url.split(':')[2].split('/')[0])
                headers={'Content-Type':'application/json','session-id':'synthetic-client','thread-id':str(uuid.uuid4())}
                def post(body):
                    conn=http.client.HTTPConnection('127.0.0.1',port,timeout=8)
                    conn.request('POST','/v1/responses',json.dumps(body),headers)
                    response=conn.getresponse();raw=response.read();conn.close()
                    self.assertEqual(response.status,200,raw)
                    return json.loads(raw.split(b'data: ')[1].split(b'\n')[0])['item']
                prior=None;last=None
                for turn in range(2):
                    inputs=[] if prior is None else [prior]
                    inputs.append({'role':'user','content':[{'type':'input_text','text':'synthetic '+str(turn)}]})
                    body={'model':'native-subagent-bridge','stream':True,'input':inputs}
                    permit=root/('permit-'+str(turn)+'.json');result=root/('result-'+str(turn)+'.json')
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        call=pool.submit(post,body)
                        info=cmd('worker-next','--journal',wj,'--wait','4','--save',permit,*common)
                        self.assertFalse(info['native_invoked_by_python'])
                        last=info['request_id']
                        packet=json.loads(permit.read_text())
                        self.assertEqual(packet['native_task_id'],'synthetic/native')
                        result.write_text(json.dumps({'text':'synthetic answer '+str(turn)}));result.chmod(0o600)
                        cmd('worker-complete','--journal',wj,'--permit',permit,'--result',result,*common)
                        prior=call.result()
                    self.assertEqual(prior['content'][0]['text'],'synthetic answer '+str(turn))
                server.send_signal(signal.SIGINT);out,err=server.communicate(timeout=5)
                self.assertEqual(server.returncode,0,out+err)
                ack=cmd('ack-delivery','--journal',cj,'--request-id',last,'--evidence','synthetic HTTP client parsed both SSE results',*common)
                self.assertIn('receipt',ack)
            finally:
                if server.poll() is None:
                    server.terminate();server.communicate(timeout=5)
