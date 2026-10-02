"""Production stdio MCP + localhost HTTP vertical slice; synthetic workers only."""
import copy
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from test_mcp_stdio import PipeClient
from facade.test_global_runtime import config, request, response, http


class ProductionClient(PipeClient):
    def __init__(self, config_path, bearer, port):
        env = {k:v for k,v in os.environ.items() if not k.startswith('DOTS_DIRECT_') and k != 'CONTROL_PLANE_API_KEY'}
        env['DOTS_BRIDGE_HTTP_BEARER'] = bearer
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        self.proc = subprocess.Popen([sys.executable,'-m','mcp_adapter','--config',str(config_path),'--http-port',str(port)],
            cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
        self.messages=queue.Queue();self.raw=[];self.errors=bytearray()
        def read_output():
            for line in self.proc.stdout:
                self.raw.append(line)
                try:self.messages.put(json.loads(line))
                except Exception:self.messages.put({'NOT_JSON':line.decode(errors='replace')})
        def read_errors():self.errors.extend(self.proc.stderr.read())
        self.reader=threading.Thread(target=read_output,daemon=True)
        self.error_reader=threading.Thread(target=read_errors,daemon=True)
        self.reader.start();self.error_reader.start()
        self.counter=10;self.timings=[]

    def tool(self,name,arguments):
        self.counter+=1
        started=time.perf_counter()
        result=self.call(self.counter,name,arguments)['result']
        self.timings.append({'tool':name,'seconds':time.perf_counter()-started})
        if result['isError']:
            raise AssertionError(result['structuredContent'])
        return result['structuredContent']


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0))
        return s.getsockname()[1]


class GlobalMCPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.cfg=config(self.temp.name)
        value={k:v for k,v in self.cfg.items() if k!='http_bearer'}
        value['trust_mode']='single_owner_stdio';value['expires_at']=None
        self.cfg['expires_at']=None
        self.path=Path(self.temp.name)/'config.json';self.path.write_text(json.dumps(value));self.path.chmod(0o600)
        self.clients=[];self.start()

    def start(self):
        self.port=free_port();self.client=ProductionClient(self.path,self.cfg['http_bearer'],self.port)
        self.clients.append(self.client);self.client.initialize()

    def tearDown(self):
        for client in self.clients:client.close()
        self.temp.cleanup()

    def test_production_multi_route_tool_callback_restart_final_turn(self):
        started=time.perf_counter()
        c=self.client;c.request(2,'tools/list');tools=c.recv(2)['result']['tools']
        self.assertEqual(len(tools),8)
        get=next(t for t in tools if t['name']=='get_request')
        self.assertIn('route_id',get['inputSchema']['required'])
        results={};errors=[]
        def post(label,req,ident):
            try:results[label]=http(self.port,self.cfg['http_bearer'],ident,req)
            except Exception as exc:errors.append(exc)
        identities=[{'session-id':'codex-global','thread-id':f'project-{i}'} for i in range(2)]
        threads=[threading.Thread(target=post,args=(i,request(str(i)),identities[i])) for i in range(2)]
        for thread in threads:thread.start()
        until=time.monotonic()+3
        while time.monotonic()<until:
            status=c.tool('bridge_status',{})
            if len(status['pending_routes'])==2:break
            time.sleep(.01)
        self.assertEqual(len(status['pending_routes']),2)
        claimed=[]
        for index,route in enumerate(status['pending_routes']):
            item=c.tool('get_request',{'route_id':route['route_id'],'worker_id':f'synthetic-native-{index}',
                'context_epoch':'unchanged-native-epoch','model':route['model'],
                'reasoning_effort':route['reasoning_effort'],'wait_ms':0})
            self.assertEqual(item['context']['kind'],'full');claimed.append(item)
            scope={k:item[k] for k in ('route_id','claim_token','request_id','context_token')}
            # One route returns tool intent; the other ordinary text.
            if index==0:
                match=c.tool('discover_tools',{**scope,'query':'fixture_probe'})['matches'][0]
                schema=c.tool('lookup_schema',{**scope,'name':match['key'],'sha256':match['schema_sha256']})
                self.tool_args={**scope,'action_id':'action-tool','response':response(1,True),
                               'schema_tokens':[schema['schema_token']],'wait_ms':0}
                self.assertEqual(c.tool('submit_action_and_wait_result',self.tool_args)['status'],'pending')
            else:
                c.tool('finish_request',{**scope,'action_id':'action-final','response':response(2),'schema_tokens':[]})
        for thread in threads:thread.join(4)
        self.assertFalse(errors);self.assertEqual(len(results),2)
        self.assertTrue(all(value[0]==200 for value in results.values()))
        tool_index=next(index for index,result in results.items() if result[1]['output'][0]['type']=='function_call')
        tool_identity=identities[tool_index]
        # Gateway process restart keeps the SAME logical child and effects ledger.
        c.close();self.start();c=self.client
        self.assertEqual(c.tool('submit_action_and_wait_result',self.tool_args)['status'],'pending')
        code,blocked=http(self.port,self.cfg['http_bearer'],tool_identity,request(str(tool_index)))
        self.assertEqual(code,409);self.assertEqual(blocked['error']['code'],'delivery_outcome_unknown_no_reemission')
        current=request(str(tool_index));call=results[tool_index][1]['output'][0]
        current['input'] += [call,{'type':'function_call_output','call_id':call['call_id'],'output':'synthetic client tool output'}]
        next_thread=threading.Thread(target=post,args=('callback',current,tool_identity));next_thread.start()
        first=claimed[0]
        item=c.tool('await_result',{'route_id':first['route_id'],'claim_token':first['claim_token'],
            'request_id':first['request_id'],'action_id':'action-tool','wait_ms':1000})
        self.assertEqual(item['context']['kind'],'delta')
        self.assertEqual(item['context']['append'][-1]['output'],'synthetic client tool output')
        scope={k:item[k] for k in ('route_id','claim_token','request_id','context_token')}
        c.tool('finish_request',{**scope,'action_id':'callback-final','response':response(3),'schema_tokens':[]})
        next_thread.join(4);self.assertFalse(errors)
        self.assertEqual(results['callback'][0],200)
        # Ordinary later user turn in the same conversation reuses native context.
        current['input'] += results['callback'][1]['output']+[{'role':'user','content':'one more question'}]
        final_thread=threading.Thread(target=post,args=('user-next',current,tool_identity));final_thread.start()
        item=c.tool('get_request',{'route_id':first['route_id'],'claim_token':first['claim_token'],'after_seq':2,'wait_ms':1000})
        self.assertEqual(item['context']['kind'],'delta')
        scope={k:item[k] for k in ('route_id','claim_token','request_id','context_token')}
        c.tool('finish_request',{**scope,'action_id':'later-final','response':response(4),'schema_tokens':[]})
        final_thread.join(4);self.assertEqual(results['user-next'][0],200)
        status=c.tool('bridge_status',{})
        metrics=[r['metrics'] for r in status['routes']]
        self.assertEqual(sum(x['full_context_returns'] for x in metrics),2)
        self.assertEqual(sum(x['delta_context_returns'] for x in metrics),2)
        self.assertEqual(sum(x['response_commits'] for x in metrics),4)
        self.assertFalse(status['model_api_used']);self.assertFalse(status['actual_native_platform_verified'])
        for client in self.clients:
            self.assertNotIn(self.cfg['http_bearer'].encode(),b''.join(client.raw)+client.errors)
        self.evidence={'fixture':'production MCP stdio + actual localhost HTTP, synthetic actors',
            'live_mac_verified':False,'live_native_child_verified':False,'model_api_calls':0,
            'routes':len(status['routes']),'response_turns':len(results),
            'full_context_returns':sum(x['full_context_returns'] for x in metrics),
            'delta_context_returns':sum(x['delta_context_returns'] for x in metrics),
            'response_commits':sum(x['response_commits'] for x in metrics),
            'http_emissions':sum(x['http_emissions'] for x in metrics),
            'context_payload_bytes':[x['context_payload_bytes'] for x in metrics],
            'elapsed_seconds':time.perf_counter()-started,
            'mcp_calls':[timing for client in self.clients for timing in client.timings]}


if __name__=='__main__':unittest.main()
