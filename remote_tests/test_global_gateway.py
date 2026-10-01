"""Offline only: no Google, real native admission, global config or client tools."""
import concurrent.futures
import copy
import http.client
import json
from pathlib import Path
import secrets
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from remote_transport.global_gateway import (Store, Gateway, control_call, global_catalog,
    private_dir, probe, route_key, endpoint, strict_json)
from remote_transport.global_fixture import OfflineController, unused_fixture_port, identity, request, post, events
from remote_transport.model import ProtocolError, canonical, deployment, hash_bytes
from remote_transport.selection import load_catalog, select, admission_receipt


class GlobalGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.selected=select(load_catalog(),'gpt-6.1-sol','high')
        self.store,self.generation=Store.initialize(self.root/'state',self.selected,port=unused_fixture_port())
        self.gateway=Gateway(self.store,request_deadline=5).start();self.controller=OfflineController(self.store)
    def tearDown(self):
        self.controller.close();self.gateway.close();self.tmp.cleanup()
    def new(self,body=None,client=None,generation=None):
        body=body or request('one');client=client or identity();generation=generation or self.generation
        status,raw=post(self.store,generation,client,body);self.assertEqual(status,409,raw)
        self.assertIn(b'admission_pending',raw);rid=self.controller.admit_next()
        return rid,body,client
    def error(self,pattern,fn,*args,**kwargs):
        with self.assertRaisesRegex(ProtocolError,pattern):fn(*args,**kwargs)
    def count(self,table):
        with self.store.transaction() as db:return db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]

    def test_catalog_all_25_pairs_without_hot_switch(self):
        rows=global_catalog(self.selected)['models'];self.assertEqual(len(rows),5)
        self.assertEqual(sum(len(r['supported_reasoning_levels']) for r in rows),25)
        self.assertTrue(all(r['supports_reasoning_effort_updates'] is False for r in rows))

    def test_only_bound_authenticated_probe_and_fixture_readiness(self):
        value=probe(self.store.root);self.assertTrue(value['bound']);self.assertTrue(value['controller_active'])
        self.assertFalse(value['ready_for_config']);self.assertFalse(value['production_ready'])
        self.error('native_controller_adapter_not_implemented',control_call,self.store.root,'join',
                   {'controller_id':secrets.token_hex(16),'mode':'native_google_v2','seconds':600,'capacity':2})
        conn=http.client.HTTPConnection('127.0.0.1',self.store.config()['port'])
        conn.request('POST','/control/v1/status',body=canonical({'challenge':secrets.token_hex(16)}),headers={'Content-Type':'application/json'})
        response=conn.getresponse();self.assertEqual(response.status,409);self.assertIn(b'authentication',response.read());conn.close()

    def test_private_state_permissions_and_reinitialization(self):
        self.assertEqual((self.store.root/'controller.key').stat().st_mode&0o777,0o600)
        self.error('already_exists',Store.initialize,self.store.root,self.selected,port=self.store.config()['port'])
        link=self.root/'link';link.symlink_to(self.store.root)
        self.error('symlink',Store,link)

    def test_listener_lease_and_unrelated_port_occupant(self):
        self.error('already_running',Gateway,self.store)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));sock.listen(1)
            other,_=Store.initialize(self.root/'other',self.selected,port=sock.getsockname()[1])
            with self.assertRaises(OSError):Gateway(other)

    def test_invalid_selection_never_binds_a_thread(self):
        for body in [request('x',effort='ultra'),request('x',model='arbitrary'),{**request('x'),'reasoning':{}}]:
            status,raw=post(self.store,self.generation,identity(),body);self.assertEqual(status,409,raw)
        self.assertEqual(self.count('routes'),0)

    def test_schema_failure_before_route_creation(self):
        body=request('x');body['input']=[{'type':'function_call_output','call_id':'forged','output':'x'}]
        status,raw=post(self.store,self.generation,identity(),body);self.assertEqual(status,409,raw)
        self.assertEqual(self.count('routes'),0)

    def test_origin_authorization_host_and_legacy_headers_rejected(self):
        for headers in [{'Origin':'http://127.0.0.1'},{'Authorization':'Bearer test'},
                        {'Host':'localhost'},{'session_id':'legacy'},{'Sec-Fetch-Site':'same-origin'},
                        {'Cookie':'test=test'},{'Content-Encoding':'gzip'}]:
            status,raw=post(self.store,self.generation,identity(),request('x'),headers)
            self.assertEqual(status,409,(headers,raw))
        self.assertEqual(self.count('routes'),0)

    def test_duplicate_headers_and_json_keys_fail_closed(self):
        port=self.store.config()['port'];conn=http.client.HTTPConnection('127.0.0.1',port)
        conn.putrequest('POST',f'/activations/{self.generation}/v1/responses')
        conn.putheader('Host',f'127.0.0.1:{port}');conn.putheader('Content-Length','2');conn.putheader('Content-Type','application/json')
        conn.endheaders(b'{}');response=conn.getresponse();self.assertEqual(response.status,409);response.read();conn.close()
        self.error('duplicate_json_key',strict_json,b'{"a":1,"a":2}')

    def test_no_controller_and_stale_controller_never_queue(self):
        with self.store.transaction() as db:db.execute('UPDATE controller SET heartbeat=?',(time.time()-60,))
        status,raw=post(self.store,self.generation,identity(),request('x'));self.assertEqual(status,503);self.assertIn(b'controller_not_ready',raw)
        self.assertEqual(self.count('routes'),0)
        self.error('controller_not_ready',self.controller.call,'heartbeat')

    def test_bounded_admission_queue(self):
        with self.store.transaction() as db:db.execute("UPDATE meta SET value='1' WHERE key='max_pending'")
        self.assertEqual(post(self.store,self.generation,identity(),request('a'))[0],409)
        status,raw=post(self.store,self.generation,identity(),request('b'));self.assertEqual(status,503);self.assertIn(b'queue_full',raw)
        self.assertEqual(self.count('routes'),1)

    def test_claim_cas_and_unknown_spawn_are_never_replayed(self):
        with self.store.transaction() as db:db.execute('UPDATE controller SET capacity=1')
        for text in ('a','b'):post(self.store,self.generation,identity(),request(text))
        claim=self.controller.claim();owned={k:claim[k] for k in ('route_id','claim','version')}
        begun=self.controller.call('begin',owned);self.assertFalse(begun['effect_performed_by_python'])
        self.error('cas_conflict',self.controller.call,'begin',owned)
        restarted=Store(self.store.root);self.assertEqual(restarted.route(claim['route_id'])['state'],'spawn_intent')
        self.controller.call('unknown',{**owned,'version':begun['version']})
        self.error('capacity_exhausted',self.controller.claim)
        self.assertEqual(self.controller.spawn_count,0)

    def test_admission_receipt_wrong_selection_rejected(self):
        post(self.store,self.generation,identity(),request('x'));claim=self.controller.claim()
        owned={k:claim[k] for k in ('route_id','claim','version')};begun=self.controller.call('begin',owned)
        other=select(load_catalog(),'gpt-6-astra','max');args={**begun['spawn_arguments'],'model':other['model'],'reasoning_effort':other['reasoning_effort']}
        task='/offline_fixture/'+args['task_name'];receipt=admission_receipt(other,args,task)
        pin=deployment(claim['route_id'],task,seconds=300,scope='responses_tools',inference={'selection':other,'admission':receipt})
        self.error('route_pin_binding_mismatch',self.controller.call,'admit',{**owned,'version':begun['version'],'pin':pin.value})
        self.assertEqual(self.store.route(claim['route_id'])['state'],'spawn_intent')

    def test_thread_pair_immutable_and_different_session_key(self):
        rid,body,client=self.new();status,raw=post(self.store,self.generation,client,request('change','gpt-6-astra','max'))
        self.assertEqual(status,409);self.assertIn(b'thread_selection_immutable',raw);self.assertEqual(self.count('routes'),1)
        other={**client,'session-id':'other-session'}
        self.assertNotEqual(route_key(self.generation,client),route_key(self.generation,other))
        self.assertEqual(post(self.store,self.generation,other,request('new'))[0],409);self.assertEqual(self.count('routes'),2)

    def test_two_concurrent_clients_different_pairs_and_no_cross_history(self):
        first=self.new(request('only A'));second=self.new(request('only B','gpt-6-astra','max'))
        with concurrent.futures.ThreadPoolExecutor() as pool:
            futures=[pool.submit(self.controller.turn,rid,body,text) for (rid,body,_),text in [(first,'A result'),(second,'B result')]]
            out=[f.result() for f in futures]
        self.assertNotEqual(out[0][1]['native_task_id'],out[1][1]['native_task_id'])
        self.assertEqual(out[0][1]['request']['responses_request']['input'][-1]['content'][0]['text'],'only A')
        self.assertEqual(out[1][1]['request']['responses_request']['input'][-1]['content'][0]['text'],'only B')
        self.assertEqual(self.controller.spawn_count,2)
        self.assertNotEqual(self.store.route(first[0])['pin'],self.store.route(second[0])['pin'])

    def test_completed_text_retry_same_digest_never_reexecutes(self):
        rid,body,client=self.new();original,_=self.controller.turn(rid,body,'answer')
        status,raw=post(self.store,self.generation,client,body);self.assertEqual(status,200);self.assertEqual(events(raw),original)
        with self.controller.children[rid]['worker'].journal.locked() as s:self.assertEqual(len(s['executions']),1)
        self.assertEqual(self.store.route(rid)['used'],1)

    def test_tool_full_loop_real_receipt_and_no_duplicate_side_effect(self):
        tool={'type':'function','name':'fixture_write','parameters':{'type':'object','properties':{'value':{'type':'string'}},'required':['value']}}
        rid,body,client=self.new(request('write',tools=[tool]))
        ev,_=self.controller.turn(rid,body,{'kind':'function_call','name':'fixture_write','arguments':{'value':'A'}})
        item=next(e['item'] for e in ev if e['type']=='response.output_item.done');side_effects=[item['call_id']]
        status,raw=post(self.store,self.generation,client,body);self.assertEqual(status,409);self.assertIn(b'no_replay',raw)
        self.assertEqual(len(side_effects),1)
        output={'type':'function_call_output','call_id':item['call_id'],'output':'synthetic completed'}
        follow=request('done',history=[item,output]);ev2,_=self.controller.turn(rid,follow,'done')
        self.assertEqual(ev2[-1]['type'],'response.completed')
        with self.controller.children[rid]['controller'].journal.locked() as s:self.assertEqual(len(s['deliveries']),1)

    def test_unknown_after_stream_does_not_resubmit_inference(self):
        rid,body,client=self.new()
        with patch.object(self.store,'request_finish',side_effect=ProtocolError('simulated_commit_unknown')):
            self.controller.turn(rid,body,'possibly observed')
        status,raw=post(self.store,self.generation,client,body);self.assertEqual(status,409);self.assertIn(b'outcome_unknown_no_replay',raw)
        with self.controller.children[rid]['worker'].journal.locked() as s:self.assertEqual(len(s['executions']),1)

    def test_downstream_disconnect_keeps_one_attempt_after_gateway_restart(self):
        rid,body,client=self.new();self.controller.children[rid]['facade'].close()
        status,raw=post(self.store,self.generation,client,body);self.assertEqual(status,503)
        self.gateway.close();self.gateway=Gateway(Store(self.store.root)).start()
        status,raw=post(self.store,self.generation,client,body);self.assertEqual(status,409);self.assertIn(b'no_replay',raw)
        self.assertEqual(self.store.route(rid)['used'],1)

    def test_changed_default_new_generation_does_not_rebind_old_route(self):
        rid,body,client=self.new();new=self.store.activate(select(load_catalog(),'gpt-6-astra','max'))
        newrid,_,_=self.new(request('other','gpt-6-astra','max'),client,new)
        self.assertNotEqual(rid,newrid);self.assertEqual(self.store.route(rid)['selection'],json.dumps(self.selected))
        self.assertEqual(self.store.route(rid)['generation'],self.generation)
        self.assertIn(new,probe(self.store.root)['base_url'])

    def test_disabled_activation_blocks_only_new_admissions(self):
        rid,body,client=self.new();self.store.disable(self.generation)
        status,raw=post(self.store,self.generation,identity(),request('x'));self.assertEqual(status,409);self.assertIn(b'activation_closed',raw)
        self.controller.turn(rid,body,'existing route completes')

    def test_quota_idle_expiry_and_controller_epoch_fence(self):
        rid,body,client=self.new()
        with self.store.transaction() as db:db.execute("UPDATE routes SET used=128 WHERE id=?",(rid,))
        status,raw=post(self.store,self.generation,client,body);self.assertIn(b'budget_exhausted',raw)
        with self.store.transaction() as db:db.execute("UPDATE routes SET used=0,last_used=? WHERE id=?",(time.time()-1900,rid))
        status,raw=post(self.store,self.generation,client,body);self.assertIn(b'idle_expired',raw)
        with self.store.transaction() as db:
            db.execute('UPDATE routes SET last_used=? WHERE id=?',(time.time(),rid));db.execute('UPDATE controller SET epoch=?',(secrets.token_hex(16),))
        status,raw=post(self.store,self.generation,client,body);self.assertIn(b'epoch_retired',raw)
        self.assertEqual(self.count('requests'),0)

    def test_downstream_endpoint_never_external_or_recursive(self):
        for url in ['http://localhost:123/v1','https://127.0.0.1:123/v1','http://127.0.0.1:123/v1?token=x',
                    'http://user@127.0.0.1:123/v1','http://127.0.0.2:123/v1','http://127.0.0.1/v1']:
            self.error('endpoint',endpoint,url)

    def test_bounded_first_request_wait_dispatches_once_without_user_retry(self):
        self.gateway.admission_wait=3;client=identity();body=request('first held privately')
        with concurrent.futures.ThreadPoolExecutor() as pool:
            pending=pool.submit(post,self.store,self.generation,client,body)
            until=time.monotonic()+2
            while self.count('routes')==0 and time.monotonic()<until:time.sleep(.01)
            rid=self.controller.admit_next();worker=self.controller.children[rid]['worker']
            permit=worker.poll(worker.start_next,attempts=8,initial_delay=.02,max_delay=.1);self.assertIsNotNone(permit)
            worker.complete(permit,'first result');status,raw=pending.result()
        self.assertEqual(status,200);self.assertIn(b'response.in_progress',raw)
        parsed=events(raw);self.assertEqual(sum(e['type']=='response.created' for e in parsed),1)
        self.assertEqual(len({e['response']['id'] for e in parsed if 'response' in e}),1)
        self.assertEqual(events(raw)[-1]['type'],'response.completed')
        with worker.journal.locked() as state:self.assertEqual(len(state['executions']),1)

    def test_immediate_followup_waits_for_completed_response_journal_flush(self):
        rid,body,client=self.new();worker=self.controller.children[rid]['worker'];original=self.store.request_finish
        def delayed_finish(*args):time.sleep(.3);return original(*args)
        def read_until_completed():
            conn=http.client.HTTPConnection('127.0.0.1',self.store.config()['port'],timeout=5)
            conn.request('POST',f'/activations/{self.generation}/v1/responses',body=canonical(body),headers={**client,'Content-Type':'application/json'})
            response=conn.getresponse();rows=[]
            while True:
                line=response.readline()
                if not line:break
                if line.startswith(b'data: '):
                    item=json.loads(line[6:]);rows.append(item)
                    if item['type']=='response.completed':break
            conn.close();return rows
        with patch.object(self.store,'request_finish',side_effect=delayed_finish),concurrent.futures.ThreadPoolExecutor() as pool:
            first=pool.submit(read_until_completed)
            permit=worker.poll(worker.start_next,attempts=8,initial_delay=.02,max_delay=.1);self.assertIsNotNone(permit)
            worker.complete(permit,'first');rows=first.result()
            item=next(e['item'] for e in rows if e['type']=='response.output_item.done')
            completed,_=self.controller.turn(rid,request('next',history=[item]),'second')
            self.assertEqual(completed[-1]['type'],'response.completed')
        with worker.journal.locked() as state:self.assertEqual(len(state['executions']),2)

    def test_connection_close_body_stall_obeys_absolute_dispatch_deadline(self):
        rid,body,client=self.new();self.gateway.request_deadline=.3
        def delayed(handler):
            time.sleep(.15);handler.send_response(200);handler.send_header('Content-Type','text/event-stream')
            handler.send_header('Connection','close');handler.end_headers()
            event={'type':'response.in_progress','response':{'id':'resp_fixture','status':'in_progress'}}
            handler.wfile.write(b'event: response.in_progress\ndata: '+canonical(event)+b'\n\n');handler.wfile.flush()
            time.sleep(.8);handler.close_connection=True
        facade=self.controller.children[rid]['facade']
        with patch.object(facade.server.RequestHandlerClass,'do_POST',delayed):
            start=time.monotonic();status,raw=post(self.store,self.generation,client,body);elapsed=time.monotonic()-start
        self.assertEqual(status,200);self.assertLess(elapsed,.43)
        self.assertIn(b'response.in_progress',raw)
        status,raw=post(self.store,self.generation,client,body);self.assertEqual(status,409);self.assertIn(b'no_replay',raw)

    def test_disconnect_during_admission_does_not_dispatch_later(self):
        self.gateway.admission_wait=3;client=identity();body=request('cancel before inference');raw=canonical(body)
        sock=socket.create_connection(('127.0.0.1',self.store.config()['port']))
        headers=('POST /activations/'+self.generation+'/v1/responses HTTP/1.1\r\nHost: 127.0.0.1:'+str(self.store.config()['port'])+
                 '\r\nContent-Type: application/json\r\nContent-Length: '+str(len(raw))+'\r\nsession-id: '+client['session-id']+
                 '\r\nthread-id: '+client['thread-id']+'\r\n\r\n').encode()
        sock.sendall(headers+raw);sock.recv(4096);sock.shutdown(socket.SHUT_RDWR);sock.close()
        rid=route_key(self.generation,client);lock=self.gateway.lock_for(rid)
        self.assertTrue(lock.acquire(timeout=2));lock.release()
        self.controller.admit_next();self.assertEqual(self.count('requests'),0)
        self.assertIsNone(self.controller.children[rid]['worker'].start_next())

    def test_admission_wait_timeout_has_no_request_dispatch(self):
        self.gateway.admission_wait=.1
        status,raw=post(self.store,self.generation,identity(),request('never dispatched'))
        self.assertEqual(status,200);self.assertEqual(events(raw)[-1]['type'],'response.failed')
        self.assertIn(b'no_inference_dispatched',raw);self.assertEqual(self.count('requests'),0)

    def test_status_does_not_disclose_prompts_identity_or_secret(self):
        _,body,_=self.new(request('private fixture prompt'));raw=canonical(probe(self.store.root))
        self.assertNotIn(b'private fixture prompt',raw);self.assertNotIn(self.store.key.encode(),raw)
        self.assertNotIn(b'thread-id',raw);self.assertFalse(probe(self.store.root)['automatic_wake'])

if __name__=='__main__':unittest.main()
