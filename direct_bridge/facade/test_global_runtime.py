"""Synthetic worker/client verification. Actual localhost HTTP, no model inference."""
import copy
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from facade.runtime import create_runtime


def config(directory):
    pair = {'model': 'gpt-6-astra', 'reasoning_effort': 'xhigh'}
    return {'mode': 'global', 'db_path': str(Path(directory) / 'global.sqlite3'),
            'config_id': 'global-fixture', 'grant_id': 'fixture-grant', 'client_actor': 'fixture-client',
            'approval_ref': 'explicit-local-fixture', 'not_before': time.time()-1,
            'expires_at': time.time()+3600, 'allowed_pairs': [pair], 'default_pair': pair,
            'max_routes': 2, 'http_wait_ms': 2000, 'http_bearer': 'ephemeral-local-fixture-bearer'}


def request(text='hello'):
    return {'model': 'gpt-6-astra', 'reasoning': {'effort': 'xhigh'}, 'stream': True,
            'instructions': 'Synthetic context only. ' * 100,
            'input': [{'role': 'user', 'content': text}],
            'tools': [{'type': 'function', 'name': 'fixture_probe',
                       'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}]}


def response(index=1, tool=False):
    item = ({'type': 'function_call', 'id': f'fc_{index}', 'call_id': f'call-{index}',
             'name': 'fixture_probe', 'arguments': '{}'} if tool else
            {'type': 'message', 'id': f'msg_{index}', 'role': 'assistant',
             'content': [{'type': 'output_text', 'text': f'answer {index}'}]})
    return {'id': f'resp_{index}', 'object': 'response', 'model': 'gpt-6-astra',
            'status': 'completed', 'output': [item]}


def http(port, bearer, identity=None, req=None, path='/v1/responses', key=None):
    conn = HTTPConnection('127.0.0.1', port, timeout=5)
    headers = {'Authorization': 'Bearer '+bearer}
    if identity:
        headers.update(identity)
    if req is not None:
        headers['Content-Type'] = 'application/json'
    if key:
        headers['Idempotency-Key'] = key
    conn.request('GET' if req is None else 'POST', path,
                 None if req is None else json.dumps(req), headers)
    reply = conn.getresponse()
    status, raw = reply.status, reply.read()
    conn.close()
    if status == 200 and req is not None:
        event = next(block for block in raw.decode().split('\n\n') if block.startswith('event: response.completed\n'))
        return status, json.loads(event.split('data: ',1)[1])['response']
    return status, json.loads(raw)


class GlobalRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = config(self.temp.name)
        self.runtime = create_runtime(self.cfg)
        self.runtime.start_http()
        self.port = self.runtime.http.server_port

    def tearDown(self):
        self.runtime.close()
        self.assertTrue(self.runtime.diagnostics.wait_idle())
        self.temp.cleanup()

    def ingress(self, text='hello', identity=None, key=None):
        return self.runtime.ingest(identity or {'session-id':'s1','thread-id':'t1'}, request(text), key)

    def claim(self, route_id, actor='native-fixture'):
        return self.runtime.call_tool('get_request', {'route_id':route_id,'worker_id':actor,
            'context_epoch':'epoch1','model':'gpt-6-astra','reasoning_effort':'xhigh','wait_ms':0})

    def scope(self, item):
        return {k:item[k] for k in ('route_id','claim_token','request_id','context_token')}

    def finish(self, item, index=1):
        args = {**self.scope(item),'action_id':f'finish-{index}','response':response(index),'schema_tokens':[]}
        return self.runtime.call_tool('finish_request', args)

    def restart(self):
        self.runtime.close()
        self.assertTrue(self.runtime.diagnostics.wait_idle())
        self.runtime = create_runtime(self.cfg)
        self.runtime.start_http()
        self.port = self.runtime.http.server_port

    def test_catalog_and_health_authenticated(self):
        code, catalog = http(self.port,self.cfg['http_bearer'],path='/models?client_version=0.159.2')
        self.assertEqual(code,200)
        self.assertEqual(catalog['models'][0]['slug'],'gpt-6-astra')
        self.assertEqual(catalog['models'][0]['default_reasoning_level'],'xhigh')
        code, health = http(self.port,self.cfg['http_bearer'],path='/health')
        self.assertEqual(code,200)
        self.assertFalse(health['automatic_wake'])
        self.assertFalse(health['actual_native_platform_verified'])
        self.assertEqual(http(self.port,'wrong',path='/health')[0],401)

    def test_missing_identity_and_unauthorized_pair_do_not_admit(self):
        self.assertEqual(http(self.port,self.cfg['http_bearer'],req=request())[0],409)
        bad=request();bad['model']='gpt-6-sol'
        self.assertEqual(http(self.port,self.cfg['http_bearer'],{'session-id':'s','thread-id':'t'},bad)[0],409)
        self.assertEqual(self.runtime.status()['active_routes'],0)

    def test_pending_route_claim_and_owner_immutable(self):
        route,rid=self.ingress()
        self.assertEqual(self.runtime.status()['pending_routes'][0]['route_id'],route)
        first=self.claim(route)
        self.assertEqual(first['request_id'],rid)
        self.assertEqual(first['context']['kind'],'full')
        self.assertTrue(self.claim(route)['replayed'])
        with self.assertRaisesRegex(ValueError,'owner_immutable'):
            self.claim(route,'another-child')
        with self.assertRaisesRegex(ValueError,'route_claim_required'):
            self.runtime.call_tool('cancel_request',{'route_id':route,'claim_token':'wrong','request_id':rid})

    def test_duplicate_request_ids_bound_to_route(self):
        a=self.ingress(key='same')
        self.assertEqual(a,self.ingress(key='same'))
        b=self.ingress(identity={'session-id':'s1','thread-id':'t2'},key='same')
        self.assertNotEqual(a,b)
        with self.assertRaisesRegex(ValueError,'request_id_conflict'):
            self.ingress(text='different',key='same')

    def test_cancel_restart_and_capacity_release(self):
        route,rid=self.ingress()
        first=self.claim(route)
        self.runtime.call_tool('cancel_request',{k:first[k] for k in ('route_id','claim_token','request_id')})
        self.restart()
        self.assertEqual(self.claim(route)['status'],'cancelled')
        self.runtime.call_tool('cancel_request',{'route_id':route,'claim_token':first['claim_token'],'close_route':True})
        self.assertEqual(self.runtime.status()['active_routes'],0)
        self.assertEqual(self.runtime.status()['closed_route_count'],1)
        with self.assertRaisesRegex(ValueError,'route_closed'):
            self.ingress('new turn')

    def test_request_and_action_receipts_survive_restart(self):
        route,rid=self.ingress(key='a')
        first=self.claim(route)
        self.finish(first)
        self.restart()
        self.assertEqual(self.ingress(key='a'),(route,rid))
        self.assertEqual(self.finish(first)['status'],'completed')
        self.assertEqual(self.runtime.status()['routes'][0]['metrics']['response_commits'],1)
        with self.assertRaisesRegex(ValueError,'action_id_conflict'):
            self.runtime.call_tool('finish_request',{**self.scope(first),'action_id':'finish-1',
                'response':response(2),'schema_tokens':[]})

    def test_concurrent_conversations_then_sequential_turn_after_restart(self):
        results={}; errors=[]
        def post(label, req, ident):
            try: results[label]=http(self.port,self.cfg['http_bearer'],ident,req)
            except Exception as exc: errors.append(exc)
        identities=[{'session-id':'shared-client','thread-id':f'thread-{i}'} for i in range(2)]
        threads=[threading.Thread(target=post,args=(i,request(str(i)),identities[i])) for i in range(2)]
        for t in threads:t.start()
        until=time.monotonic()+2
        while len(self.runtime.status()['pending_routes'])<2 and time.monotonic()<until:time.sleep(.005)
        firsts={}
        for route in self.runtime.status()['pending_routes']:
            route_id=route['route_id']; index=int(self.runtime.routes[route_id]['identity']['thread-id'][-1])
            firsts[index]=self.claim(route_id,f'worker-{index}');self.finish(firsts[index],index+1)
        for t in threads:t.join(3)
        self.assertFalse(errors)
        self.assertEqual([results[i][0] for i in range(2)],[200,200])
        self.restart()
        current=request('0')
        # Codex's common serialization omits the assistant item id/status/type.
        message=copy.deepcopy(results[0][1]['output'][0]);message.pop('id');message.pop('type')
        current['input'] += [message,{'role':'user','content':'next user turn'}]
        thread=threading.Thread(target=post,args=('next',current,identities[0]));thread.start()
        item=self.runtime.call_tool('get_request',{'route_id':firsts[0]['route_id'],
            'claim_token':firsts[0]['claim_token'],'after_seq':1,'wait_ms':1000})
        self.assertEqual(item['context']['kind'],'delta')
        self.assertEqual(len(item['context']['append']),2)
        self.finish(item,3);thread.join(3)
        self.assertEqual(results['next'][0],200)
        self.assertFalse(errors)
        self.assertEqual(self.runtime.status()['routes'][0]['metrics']['full_context_returns'],1)

    def test_http_text_replay_is_safe_and_does_not_recommit(self):
        route,rid=self.ingress()
        first=self.claim(route);self.finish(first)
        ident={'session-id':'s1','thread-id':'t1'}
        self.assertEqual(http(self.port,self.cfg['http_bearer'],ident,request())[0],200)
        self.restart()
        self.assertEqual(http(self.port,self.cfg['http_bearer'],ident,request())[0],200)
        self.assertEqual(self.runtime.status()['routes'][0]['metrics']['response_commits'],1)

    def test_tool_commit_and_lost_response_fence_survive_restart(self):
        route,rid=self.ingress()
        first=self.claim(route)
        found=self.runtime.call_tool('discover_tools',{**self.scope(first),'query':'fixture_probe'})['matches'][0]
        schema=self.runtime.call_tool('lookup_schema',{**self.scope(first),'name':found['key'],'sha256':found['schema_sha256']})
        args={**self.scope(first),'action_id':'tool-1','response':response(1,True),'schema_tokens':[schema['schema_token']],'wait_ms':0}
        self.assertEqual(self.runtime.call_tool('submit_action_and_wait_result',args)['status'],'pending')
        ident={'session-id':'s1','thread-id':'t1'}
        self.assertEqual(http(self.port,self.cfg['http_bearer'],ident,request())[0],200)
        self.restart()
        self.assertEqual(self.runtime.call_tool('submit_action_and_wait_result',args)['status'],'pending')
        code,error=http(self.port,self.cfg['http_bearer'],ident,request())
        self.assertEqual(code,409)
        self.assertEqual(error['error']['code'],'delivery_outcome_unknown_no_reemission')
        self.assertEqual(self.runtime.status()['routes'][0]['metrics']['response_commits'],1)
        current=request();call=response(1,True)['output'][0]
        current['input'] += [call,{'type':'function_call_output','call_id':call['call_id'],'output':'synthetic done'}]
        route2,rid2=self.runtime.ingest(ident,current)
        item=self.runtime.call_tool('await_result',{'route_id':route,'claim_token':first['claim_token'],
             'request_id':rid,'action_id':'tool-1','wait_ms':0})
        self.assertEqual(item['request_id'],rid2)
        self.assertEqual(item['context']['kind'],'delta')
        self.assertEqual(self.runtime.status()['routes'][0]['metrics']['full_context_returns'],1)

    def test_internal_bridge_tools_hidden_and_cannot_emit(self):
        req=request()
        req['tools'].append({'type':'namespace','name':'mcp__Dots2CodexDirect','tools':[
            {'type':'function','name':'finish_request','parameters':{'type':'object'}}]})
        route,_=self.runtime.ingest({'session-id':'s','thread-id':'t'},req)
        first=self.claim(route)
        found=self.runtime.call_tool('discover_tools',{**self.scope(first),'query':'finish_request'})
        self.assertEqual(found['total_matches'],0)
        bad=response(1,True);bad['output'][0].update(name='finish_request',namespace='mcp__Dots2CodexDirect')
        with self.assertRaisesRegex(ValueError,'bridge_transport_tool_intent_forbidden'):
            self.runtime.call_tool('submit_action_and_wait_result',{**self.scope(first),
                'action_id':'recursive','response':bad,'schema_tokens':[],'wait_ms':0})

    def test_exact_connected_transport_spelling_and_unrelated_homonyms(self):
        from facade.global_runtime import _internal_tool
        for name in ('bridge_status','get_request','submit_action_and_wait_result'):
            self.assertTrue(_internal_tool('mcp__codex_apps__dots2codex_direct_' + name))
        self.assertFalse(_internal_tool('get_request','Acme'))
        self.assertFalse(_internal_tool('get_request'))
        req=request()
        req['tools'] += [{'type':'function','name':'mcp__codex_apps__dots2codex_direct_get_request',
                          'parameters':{'type':'object'}},
                        {'type':'namespace','name':'Acme','tools':[{'type':'function','name':'get_request',
                                                                  'parameters':{'type':'object'}}]}]
        route,_=self.runtime.ingest({'session-id':'s','thread-id':'t'},req)
        first=self.claim(route)
        found=self.runtime.call_tool('discover_tools',{**self.scope(first),'query':'get_request'})
        self.assertEqual(found['total_matches'],1)
        self.assertEqual(found['matches'][0]['namespace'],'Acme')

    def test_capacity_is_explicit_and_closed_slots_reusable(self):
        self.ingress(identity={'session-id':'s','thread-id':'a'})
        self.ingress(identity={'session-id':'s','thread-id':'b'})
        with self.assertRaisesRegex(ValueError,'route_capacity_exhausted'):
            self.ingress(identity={'session-id':'s','thread-id':'c'})
        route=self.runtime.status()['pending_routes'][0]['route_id'];item=self.claim(route)
        self.finish(item)
        self.runtime.call_tool('cancel_request',{'route_id':route,'claim_token':item['claim_token'],'close_route':True})
        self.ingress(identity={'session-id':'s','thread-id':'c'})
        self.assertEqual(self.runtime.status()['active_routes'],2)

    def test_single_runtime_owner(self):
        with self.assertRaisesRegex(ValueError,'global_runtime_already_running'):
            create_runtime(self.cfg)


if __name__=='__main__':unittest.main()
