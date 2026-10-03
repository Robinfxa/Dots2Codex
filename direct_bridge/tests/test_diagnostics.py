"""Privacy, bounded storage, read-only CLI and synthetic transport diagnostics."""
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT.parent)]
import diagnostics as d
from direct_bridge import global_launcher as launcher
from facade.runtime import create_runtime
from facade.test_global_runtime import config, request, response, http


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.diag = d.Diagnostics(self.state)
    def tearDown(self):
        self.diag.close(); self.diag.wait_idle()
        self.temp.cleanup()
    def events(self):
        self.assertTrue(self.diag.wait_idle())
        return d.recent(self.state, 200)['events']

    def test_allowlists_drop_payloads_raw_ids_and_exception_text(self):
        secret = 'SYNTHETIC_SECRET_DO_NOT_LOG'
        self.diag.event('mcp', 'begin', method='get_request', ids={'route': secret, 'request': secret,
            'action': secret, 'claim_token': secret, 'context_token': secret}, wait_ms=5,
            query=secret, response=secret, payload=secret, token=secret, error_code=secret,
            reason=secret, duration_ms=secret, unused=secret)
        value = self.events()[0]
        self.assertNotIn(secret, json.dumps(value))
        self.assertEqual(value['wait_ms'], 5)
        self.assertEqual(set(value) - {'version', 'utc', 'monotonic_ms', 'process_ref', 'event_seq',
            'operation', 'stage', 'method', 'wait_ms', 'route_ref', 'request_ref', 'action_ref'}, set())
        for path in self.diag.path.iterdir():
            self.assertNotIn(secret.encode(), path.read_bytes())
        self.assertRegex(value['request_ref'], r'^[0-9a-f]{24}$')
        self.assertNotEqual(value['request_ref'], value['action_ref'])
        self.diag.event(secret, 'begin', payload=secret)
        self.assertEqual(len(self.events()), 1)

    def test_stable_correlation_across_processes_with_distinct_process_refs(self):
        self.diag.event('mcp', 'begin', ids={'route': 'same-route'})
        self.assertTrue(self.diag.wait_idle())
        env = {**os.environ, 'PYTHONPATH': str(ROOT), 'PYTHONDONTWRITEBYTECODE': '1'}
        subprocess.run([sys.executable, '-B', '-c',
            'from diagnostics import Diagnostics; import sys; d=Diagnostics(sys.argv[1]); d.event("mcp", "begin", ids={"route":"same-route"}); d.wait_idle(); d.close()',
            str(self.state)], env=env, check=True, capture_output=True)
        a, b = self.events()
        self.assertEqual(a['route_ref'], b['route_ref'])
        self.assertNotEqual(a['process_ref'], b['process_ref'])
        self.assertEqual((self.diag.path/'correlation-salt.bin').stat().st_size, 32)

    def test_files_private_rotation_and_tail_are_bounded(self):
        for index in range(2800):
            self.diag.event('mcp', 'begin', ids={'call': str(index)}, method='await_result', wait_ms=5000)
            if index % 100 == 99: self.assertTrue(self.diag.wait_idle())
        self.assertEqual(stat.S_IMODE(self.diag.path.stat().st_mode), 0o700)
        for path in self.diag.path.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue((self.diag.path/'events.jsonl.1').exists())
        self.assertLessEqual(sum((self.diag.path/n).stat().st_size for n in d.EVENT_FILES), 2*d.MAX_FILE_BYTES)
        events = d.recent(self.state, 7)['events']
        self.assertEqual(len(events), 7)
        self.assertEqual(events[-1]['event_seq'], 2800)
        self.assertGreater(events[0]['event_seq'], 1)
        self.assertEqual(d.recent(self.state, 201)['status'], 'invalid_line_limit')

    def test_concurrent_writers_produce_complete_records(self):
        def emit():
            for _ in range(25):
                self.diag.event('mcp', 'end', outcome='ok')
        threads=[threading.Thread(target=emit) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        events=self.events()
        self.assertGreater(len(events),0)
        self.assertLessEqual(len(events),100)  # Busy-lock records may be dropped by design.
        self.assertEqual(len({e['event_seq'] for e in events}),len(events))

    def test_reader_does_not_write_or_create_missing_state(self):
        missing = self.state/'missing'
        self.assertEqual(d.recent(missing)['status'], 'not_available')
        self.assertFalse(missing.exists())
        self.diag.event('service', 'started')
        self.assertTrue(self.diag.wait_idle())
        before={p.name:(p.stat().st_mtime_ns,p.read_bytes()) for p in self.diag.path.iterdir()}
        d.recent(self.state)
        self.assertEqual(before, {p.name:(p.stat().st_mtime_ns,p.read_bytes()) for p in self.diag.path.iterdir()})

    def test_reader_revalidates_and_bounds_corrupt_or_old_files(self):
        self.diag.event('service', 'started')
        path=self.diag.path/'events.jsonl'
        value=self.events()[0]
        value.update(query='PRIVATE_QUERY', reason='PRIVATE_REASON', route_ref='PRIVATE_TOKEN')
        with path.open('ab') as out:
            out.write(json.dumps(value).encode()+b'\n')
            out.write(b'PRIVATE_EXCEPTION\n')
            out.write(b'x'*(d.MAX_LINE_BYTES+1)+b'\n')
        result=d.recent(self.state)
        self.assertEqual(result['status'],'partial')
        self.assertNotIn('PRIVATE_',json.dumps(result))
        self.assertEqual(len(result['events']),2)
        path.write_bytes(b'x'*(d.MAX_FILE_BYTES+1))
        self.assertEqual(d.recent(self.state)['status'],'partial')
        self.assertEqual(d.recent(self.state)['events'],[])

    def test_symlink_hardlink_fifo_and_public_files_rejected(self):
        self.diag.event('service','started')
        self.assertTrue(self.diag.wait_idle())
        path=self.diag.path/'events.jsonl'
        outside=self.state/'outside';outside.write_bytes(b'KEEP_PRIVATE');outside.chmod(0o600)
        for kind in ('symlink','hardlink','fifo','public'):
            path.unlink()
            if kind=='symlink':path.symlink_to(outside)
            elif kind=='hardlink':os.link(outside,path)
            elif kind=='fifo':os.mkfifo(path,0o600)
            else:path.write_bytes(b'PRIVATE');path.chmod(0o644)
            self.diag.event('service','started')
            self.assertTrue(self.diag.wait_idle())
            result=d.recent(self.state)
            self.assertEqual(result['status'],'partial')
            self.assertEqual(result['events'],[])
            self.assertEqual(outside.read_bytes(),b'KEEP_PRIVATE')
        path.unlink()
        path.write_bytes(b'');path.chmod(0o600)
        (self.diag.path/'correlation-salt.bin').chmod(0o644)
        other=d.Diagnostics(self.state);other.event('service','started');other.wait_idle();other.close()
        self.assertEqual(path.read_bytes(),b'')

    def test_full_queue_drops_without_retaining_unknown_fields(self):
        blocked=threading.Event();release=threading.Event()
        original=self.diag._write
        def slow_write(*args):
            blocked.set();release.wait(2);return original(*args)
        with patch.object(self.diag,'_write',side_effect=slow_write):
            try:
                self.diag.event('mcp','begin')
                self.assertTrue(blocked.wait(1))
                huge={'PRIVATE_PAYLOAD':'x'*100000}
                for _ in range(1000):
                    self.diag.event('mcp','begin',method='get_request',wait_ms=huge,
                        query=huge,ids={'route':'x'*257,'request':'SAFE_LOGICAL_ID','claim_token':'SECRET'})
                self.assertEqual(self.diag._queue.qsize(),d.MAX_QUEUED_EVENTS)
                with self.diag._queue.mutex:
                    values=list(self.diag._queue.queue)
                for value,ids in values:
                    self.assertNotIn('wait_ms',value)
                    self.assertNotIn('query',value)
                    self.assertEqual(ids,{'request':'SAFE_LOGICAL_ID'})
            finally:
                release.set()
                self.assertTrue(self.diag.wait_idle())

    def test_busy_queue_lock_drops_instead_of_blocking_producer(self):
        self.diag._queue.mutex.acquire()
        try:
            self.diag.event('mcp','begin')
        finally:
            self.diag._queue.mutex.release()
        self.assertEqual(self.events(),[])

    def test_writer_start_failure_disables_only_diagnostics(self):
        with patch.object(d.threading.Thread,'start',side_effect=RuntimeError('PRIVATE_START_FAILURE')):
            diag=d.Diagnostics(self.state)
        self.assertTrue(diag._closed)
        diag.event('mcp','begin')
        self.assertFalse(diag.path.exists())

    def test_logging_failures_do_not_raise(self):
        with patch.object(d.os,'write',side_effect=OSError('PRIVATE_FAILURE')):
            self.diag.event('service','started')
            self.assertTrue(self.diag.wait_idle())
        self.assertNotIn('PRIVATE_FAILURE',json.dumps(d.recent(self.state)))


class CommandTests(unittest.TestCase):
    def test_initial_and_old_settings_work_without_dependencies_or_service_actions(self):
        with tempfile.TemporaryDirectory() as temp:
            state=Path(temp)/'absent'
            # -S prevents importing installed Pillow, MCP, tomlkit or jsonschema.
            def run():
                return subprocess.run([sys.executable,'-B','-S',str(ROOT/'global_launcher.py'),
                    'diagnostics','--state-dir',str(state),'--lines','5'],capture_output=True,text=True)
            result=run()
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('No diagnostic events available',result.stdout)
            self.assertFalse(state.exists())
            state.mkdir(mode=0o700)
            (state/'settings.json').write_text('CORRUPT_OLD_PRIVATE_SETTINGS')
            (state/'settings.json').chmod(0o600)
            before=(state/'settings.json').read_bytes()
            result=run()
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertNotIn('CORRUPT_OLD_PRIVATE_SETTINGS',result.stdout+result.stderr)
            self.assertEqual((state/'settings.json').read_bytes(),before)

    def test_command_never_loads_credentials_probes_http_or_mutates_status(self):
        with tempfile.TemporaryDirectory() as temp:
            state=Path(temp); (state/'bridge').mkdir(mode=0o700)
            diag=d.Diagnostics(state/'bridge');diag.event('service','started');diag.wait_idle();diag.close()
            with patch.object(launcher.Ports,'ready',side_effect=AssertionError('network')), \
                 patch.object(launcher.Launcher,'start',side_effect=AssertionError('start')):
                output=io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(launcher.main(['diagnostics','--state-dir',str(state)]),0)
            self.assertIn('service',output.getvalue())
            self.assertFalse((state/'current.json').exists())


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.cfg=config(self.temp.name)
        self.runtime=create_runtime(self.cfg);self.runtime.start_http()
        self.port=self.runtime.http.server_port
    def tearDown(self):
        self.runtime.close();self.runtime.diagnostics.wait_idle();self.temp.cleanup()
    def events(self):
        self.assertTrue(self.runtime.diagnostics.wait_idle())
        return d.recent(self.temp.name,200)['events']
    def claim(self, route):
        return self.runtime.call_tool('get_request',{'route_id':route,'worker_id':'private-worker',
            'context_epoch':'private-epoch','model':'gpt-6-astra','reasoning_effort':'xhigh','wait_ms':0})
    def finish(self,item):
        return self.runtime.call_tool('finish_request',{**{k:item[k] for k in
            ('route_id','request_id','claim_token','context_token')},'action_id':'PRIVATE_ACTION',
            'schema_tokens':[],'response':response()})
    def wait_route(self):
        until=time.monotonic()+3
        while time.monotonic()<until:
            with self.runtime.cv:
                if self.runtime.routes:return next(iter(self.runtime.routes))
            time.sleep(.01)
        self.fail('no synthetic request admitted')

    def test_http_timeout_then_late_commit_is_visible_without_payload(self):
        self.runtime.http_wait_ms=15
        code,_=http(self.port,self.cfg['http_bearer'],{'session-id':'PRIVATE_SESSION','thread-id':'PRIVATE_THREAD'},request('PRIVATE_PROMPT'))
        self.assertEqual(code,504)
        item=self.claim(self.wait_route());self.finish(item)
        events=self.events()
        timed=next(e for e in events if e['stage']=='timeout')
        committed=next(e for e in events if e['operation']=='response_commit')
        self.assertEqual(timed['error_code'],'outcome_pending')
        self.assertEqual(timed['request_ref'],committed['request_ref'])
        self.assertLess(timed['monotonic_ms'],committed['monotonic_ms'])
        self.assertNotIn('PRIVATE_',json.dumps(events))
        self.assertNotIn(item['claim_token'],json.dumps(events))
        self.assertNotIn(item['context_token'],json.dumps(events))
        self.assertNotIn(self.cfg['http_bearer'],json.dumps(events))
        self.assertFalse(any(e['stage']=='delivery_fenced' for e in events))

    def test_http_fence_and_socket_success_and_cooperative_wait(self):
        replies=[]
        thread=threading.Thread(target=lambda:replies.append(http(self.port,self.cfg['http_bearer'],
            {'session-id':'s','thread-id':'t'},request())))
        thread.start();item=self.claim(self.wait_route());self.finish(item);thread.join(3)
        self.assertEqual(replies[0][0],200)
        events=self.events()
        fence=next(e for e in events if e['stage']=='delivery_fenced')
        written=next(e for e in events if e['stage']=='socket_flushed')
        self.assertEqual(fence['request_ref'],written['request_ref'])
        self.assertEqual(written['http_status'],200)
        cancel=threading.Event();cancel.set()
        result=self.runtime.call_tool('get_request',{'route_id':item['route_id'],'claim_token':item['claim_token'],
            'after_seq':1,'wait_ms':100},cancel_event=cancel)
        self.assertEqual(result['status'],'pending')
        self.assertEqual(self.events()[-1]['reason'],'cooperative_cancel')

    def test_socket_disconnect_records_unknown_without_reemission(self):
        raw=json.dumps(request()).encode()
        sock=socket.create_connection(('127.0.0.1',self.port))
        sock.sendall((f'POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'
            f'Authorization: Bearer {self.cfg["http_bearer"]}\r\nsession-id: s\r\nthread-id: t\r\n'
            f'Content-Type: application/json\r\nContent-Length: {len(raw)}\r\n\r\n').encode()+raw)
        item=self.claim(self.wait_route())
        sock.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('ii',1,0));sock.close()
        self.finish(item)
        until=time.monotonic()+3
        while time.monotonic()<until and not any(e['stage']=='disconnect' for e in self.events()):time.sleep(.01)
        event=next(e for e in self.events() if e['stage']=='disconnect')
        self.assertEqual(event['outcome'],'unknown')
        self.assertTrue(self.runtime.routes[item['route_id']]['requests'][0]['delivery_started'])

    def test_unavailable_diagnostics_does_not_change_runtime_result(self):
        with patch.object(d.os,'write',side_effect=OSError('PRIVATE_DISK_FAILURE')):
            route,_=self.runtime.ingest({'session-id':'s','thread-id':'t'},request())
            item=self.claim(route)
            self.assertEqual(self.finish(item)['status'],'completed')
            self.assertTrue(self.runtime.diagnostics.wait_idle())


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from mcp_adapter.server import MCPServer
        self.temp=tempfile.TemporaryDirectory()
        class Runtime:
            mode='global'
            diagnostics=d.Diagnostics(self.temp.name)
            def call_tool(inner,name,args,*,cancel_event):return {'status':'pending','PRIVATE_RESPONSE':'PRIVATE_RESULT'}
            def public_error(inner,error):return 'runtime_error'
        self.runtime=Runtime();self.server=MCPServer(self.runtime,call_timeout=.025)
    async def asyncTearDown(self):
        self.server._pool.shutdown(wait=True)
        self.runtime.diagnostics.close();self.runtime.diagnostics.wait_idle()
        self.temp.cleanup()
    def events(self):
        self.assertTrue(self.runtime.diagnostics.wait_idle())
        return d.recent(self.temp.name,200)['events']
    async def test_mcp_begin_end_timings_and_no_arguments_or_results(self):
        result=await self.server._call_tool('get_request',{'route_id':'PRIVATE_ROUTE','wait_ms':5})
        self.assertFalse(result.isError)
        events=self.events()
        self.assertEqual([e['stage'] for e in events],['begin','queued','invoke_begin','invoke_end','end'])
        self.assertEqual(events[-1]['outcome'],'pending')
        self.assertEqual(events[-1]['wait_ms'],5)
        self.assertIn('duration_ms',events[-1])
        self.assertEqual(len({e['call_ref'] for e in events}),1)
        self.assertNotIn('PRIVATE_',json.dumps(events))
    async def test_timeout_and_worker_end_are_distinct(self):
        self.runtime.call_tool=lambda *args,**kwargs:(time.sleep(.08) or {'status':'pending'})
        result=await self.server._call_tool('bridge_status',{})
        self.assertTrue(result.isError)
        self.assertEqual(result.structuredContent['error'],'request_timeout')
        await asyncio.sleep(.10)
        events=self.events()
        self.assertTrue(any(e['stage']=='timeout' for e in events))
        self.assertEqual(next(e for e in events if e['stage']=='end')['outcome'],'timeout')
        self.assertEqual(events[-1]['stage'],'invoke_end')
    async def test_coroutine_cancellation_not_reported_as_user_action(self):
        self.runtime.call_tool=lambda *args,**kwargs:(time.sleep(.08) or {'status':'pending'})
        task=asyncio.create_task(self.server._call_tool('bridge_status',{}))
        await asyncio.sleep(.01);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        await asyncio.sleep(.10)
        event=next(e for e in self.events() if e['stage']=='end')
        self.assertEqual(event['outcome'],'cancelled')
        self.assertEqual(event['reason'],'coroutine_cancelled')
    async def test_stalled_disk_does_not_block_mcp_or_event_loop(self):
        blocked=threading.Event();release=threading.Event()
        original=self.runtime.diagnostics._write
        def blocked_write(*args):
            blocked.set();release.wait(2);return original(*args)
        with patch.object(self.runtime.diagnostics,'_write',side_effect=blocked_write):
            try:
                self.runtime.diagnostics.event('service','started')
                self.assertTrue(await asyncio.to_thread(blocked.wait,1))
                tick=asyncio.Event()
                asyncio.get_running_loop().call_later(.005,tick.set)
                started=time.monotonic()
                result=await asyncio.wait_for(self.server._call_tool('bridge_status',{}),.2)
                await asyncio.wait_for(tick.wait(),.2)
                self.assertFalse(result.isError)
                self.assertLess(time.monotonic()-started,.2)
                self.assertFalse(release.is_set())
            finally:
                release.set()
                self.assertTrue(self.runtime.diagnostics.wait_idle())

    async def test_unknown_tool_invalid_arguments_and_safe_error(self):
        result=await self.server._call_tool('PRIVATE_TOOL',{'PRIVATE_QUERY':'PRIVATE_VALUE'})
        self.assertEqual(result.structuredContent['error'],'unknown_tool')
        result=await self.server._call_tool('get_request',{'query':'PRIVATE_QUERY'})
        self.assertEqual(result.structuredContent['error'],'invalid_arguments')
        def fail(*args,**kwargs):raise ValueError('PRIVATE_EXCEPTION')
        self.runtime.call_tool=fail
        result=await self.server._call_tool('bridge_status',{})
        self.assertEqual(result.structuredContent['error'],'runtime_error')
        self.assertNotIn('PRIVATE_',json.dumps(self.events()))


if __name__=='__main__':unittest.main()
