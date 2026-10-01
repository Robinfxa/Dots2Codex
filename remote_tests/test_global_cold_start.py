"""Ordinary cold-route waiting: synthetic authority, real loopback transport."""
import concurrent.futures
import socket
import time
import unittest
from unittest.mock import patch

from remote_tests import test_global_gateway as fixture
from remote_transport.global_gateway import Gateway, route_key
from remote_transport.global_fixture import identity, request, post, events
from remote_transport.model import canonical, ProtocolError


class GlobalColdStartTests(unittest.TestCase):
    setUp=fixture.GlobalGatewayTests.setUp
    tearDown=fixture.GlobalGatewayTests.tearDown
    count=fixture.GlobalGatewayTests.count

    def waiting(self):
        deadline=time.monotonic()+2
        while self.count('routes')==0 and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(self.count('routes'),1)

    def test_new_thread_waits_over_180_seconds_then_dispatches_only_once(self):
        self.gateway.admission_wait=1800;client=identity();body=request('ordinary cold thread')
        real_time=time.time;real_monotonic=time.monotonic;offset={'value':0}
        with patch('time.time',side_effect=lambda:real_time()+offset['value']), \
             patch('time.monotonic',side_effect=lambda:real_monotonic()+offset['value']), \
             concurrent.futures.ThreadPoolExecutor() as pool:
            future=pool.submit(post,self.store,self.generation,client,body)
            try:
                self.waiting()
                # Atomic synthetic clock/heartbeat advancement represents an
                # independently healthy controller during seven minutes of setup.
                with self.store.transaction() as db:
                    db.execute('UPDATE controller SET heartbeat=?',(real_time()+420,))
                    offset['value']=420
                time.sleep(.08);self.assertFalse(future.done());self.assertEqual(self.count('requests'),0)
                rid=self.controller.admit_next();worker=self.controller.children[rid]['worker']
                permit=worker.poll(worker.start_next,attempts=8,initial_delay=.1,max_delay=.5)
                self.assertIsNotNone(permit);worker.complete(permit,'cold result')
                status,raw=future.result(timeout=5)
                self.assertEqual(status,200);self.assertEqual(events(raw)[-1]['type'],'response.completed')
                self.assertEqual(self.count('requests'),1);self.assertEqual(self.controller.spawn_count,1)
            except BaseException:
                self.gateway.close();self.controller.close()
                raise

    def test_stop_or_pause_during_long_wait_never_dispatches(self):
        for action in ('stop','pause'):
            with self.subTest(action=action):
                self.gateway.admission_wait=1800;client=identity();body=request(action)
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future=pool.submit(post,self.store,self.generation,client,body)
                    rid=route_key(self.generation,client);deadline=time.monotonic()+2
                    while time.monotonic()<deadline:
                        try:self.store.route(rid);break
                        except ProtocolError:time.sleep(.01)
                    if action=='stop':
                        self.store.disable(self.generation)
                    else:self.store.pause_docs_reads(self.generation,'f'*32)
                    status,raw=future.result(timeout=3)
                self.assertEqual(status,200);self.assertEqual(events(raw)[-1]['type'],'response.failed')
                self.assertEqual(self.count('requests'),0)
                if action=='stop':
                    with self.store.transaction() as db:db.execute('UPDATE activations SET enabled=1 WHERE id=?',(self.generation,))

    def test_reconnect_does_not_reset_original_setup_expiry(self):
        self.gateway.admission_wait=1800;client=identity();body=request('old pending')
        route=self.store.admission(self.generation,client,self.selected)
        with self.store.transaction() as db:db.execute('UPDATE routes SET created=? WHERE id=?',(time.time()-1801,route['id']))
        status,raw=post(self.store,self.generation,client,body)
        self.assertEqual(status,200);self.assertIn(b'expired_no_inference_dispatched',raw)
        self.assertEqual(self.count('requests'),0)

    def test_lease_expiry_and_stale_controller_do_not_keep_waiting(self):
        for field in ('expires','heartbeat'):
            with self.subTest(field=field):
                self.gateway.admission_wait=1800;client=identity();body=request('authority ends')
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future=pool.submit(post,self.store,self.generation,client,body)
                    rid=route_key(self.generation,client);deadline=time.monotonic()+2
                    while time.monotonic()<deadline:
                        try:self.store.route(rid);break
                        except ProtocolError:time.sleep(.01)
                    with self.store.transaction() as db:db.execute('UPDATE controller SET '+field+'=?',(time.time()-60,))
                    status,raw=future.result(timeout=3)
                self.assertEqual(status,200);self.assertEqual(events(raw)[-1]['type'],'response.failed')
                self.assertEqual(self.count('requests'),0)
                with self.store.transaction() as db:db.execute('UPDATE controller SET heartbeat=?,expires=?',(time.time(),time.time()+600))

    def test_final_dispatch_rechecks_wait_expiry_and_activation_stop(self):
        rid,body,client=fixture.GlobalGatewayTests.new(self)
        digest='f'*64
        with self.assertRaisesRegex(ProtocolError,'admission_expired'):
            self.store.request_start(rid,digest,admission_deadline=time.time()-1)
        self.store.disable(self.generation)
        with self.assertRaisesRegex(ProtocolError,'activation_closed'):
            self.store.request_start(rid,digest,admission_deadline=time.time()+30)
        self.assertEqual(self.count('requests'),0)

    def test_long_wait_disconnect_is_not_dispatched_later(self):
        # Existing regression uses actual socket shutdown and waits for handler
        # exit before making the same route ready; only the budget is extended.
        self.gateway.admission_wait=1800;client=identity();body=request('disconnect');raw=canonical(body)
        sock=socket.create_connection(('127.0.0.1',self.store.config()['port']))
        headers=('POST /activations/'+self.generation+'/v1/responses HTTP/1.1\r\nHost: 127.0.0.1:'+str(self.store.config()['port'])+
                 '\r\nContent-Type: application/json\r\nContent-Length: '+str(len(raw))+'\r\nsession-id: '+client['session-id']+
                 '\r\nthread-id: '+client['thread-id']+'\r\n\r\n').encode()
        sock.sendall(headers+raw);sock.recv(4096);sock.shutdown(socket.SHUT_RDWR);sock.close()
        lock=self.gateway.lock_for(route_key(self.generation,client));self.assertTrue(lock.acquire(timeout=2));lock.release()
        self.controller.admit_next();self.assertEqual(self.count('requests'),0)

    def test_invalid_wait_limit_and_generic_default_unchanged(self):
        with self.assertRaisesRegex(ProtocolError,'invalid_admission_wait'):Gateway(self.store,admission_wait=1801)
        self.gateway.close();self.gateway=Gateway(self.store)
        self.assertEqual(self.gateway.admission_wait,0);self.assertEqual(self.gateway.request_deadline,180)


if __name__=='__main__':unittest.main()
