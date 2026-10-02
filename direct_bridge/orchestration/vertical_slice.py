"""Actual HTTP + actual MCP stdio, with explicitly synthetic client and worker.

This executable fixture never invokes a native model or any Mac/local command.
Its predetermined Responses outputs test transport semantics, not inference.
"""
from __future__ import annotations
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

from facade.test_runtime import config, request, response, http_post


class StdioClient:
    def __init__(self, config_path, bearer, port):
        root = Path(__file__).resolve().parents[1]
        env = {**os.environ, 'DOTS_BRIDGE_HTTP_BEARER': bearer}
        self.process = subprocess.Popen([sys.executable, '-m', 'mcp_adapter', '--config',
            str(config_path), '--http-port', str(port)], cwd=root, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.inbox = queue.Queue()
        self.counter = 0
        self.tool_calls = []
        def reader():
            for line in self.process.stdout:
                try:
                    self.inbox.put(json.loads(line))
                except ValueError:
                    self.inbox.put({'invalid_stdout': True})
        self.reader = threading.Thread(target=reader, daemon=True)
        self.reader.start()

    def send(self, method, params, *, notification=False):
        self.counter += 1
        message = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if not notification:
            message['id'] = self.counter
        self.process.stdin.write(json.dumps(message).encode() + b'\n')
        self.process.stdin.flush()
        if notification:
            return None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            reply = self.inbox.get(timeout=max(.001, deadline - time.monotonic()))
            if reply.get('id') == self.counter:
                if 'error' in reply:
                    raise AssertionError(reply['error'])
                return reply['result']
            if 'invalid_stdout' in reply:
                raise AssertionError('non_json_mcp_stdout')
        raise AssertionError('mcp_reply_timeout')

    def call(self, name, args=None):
        start = time.monotonic_ns()
        result = self.send('tools/call', {'name': name, 'arguments': args or {}})
        self.tool_calls.append({'name': name, 'elapsed_ms': (time.monotonic_ns() - start)/1e6})
        if result.get('isError'):
            raise AssertionError(result)
        return result['structuredContent']

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        stderr = self.process.stderr.read().decode(errors='replace')
        self.process.stdout.close()
        self.process.stderr.close()
        return stderr


def run_vertical_slice():
    with tempfile.TemporaryDirectory(prefix='dots-direct-fixture-') as directory:
        cfg = config(directory)
        bearer = cfg.pop('http_bearer')
        cfg['trust_mode'] = 'single_owner_stdio'
        path = Path(directory) / 'config.json'
        path.write_text(json.dumps(cfg))
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        client = StdioClient(path, bearer, port)
        results, errors, kinds, delta_outputs, view_bytes = [], [], [], [], []
        initial = request()
        try:
            init = client.send('initialize', {'protocolVersion': '2025-06-18',
                'capabilities': {}, 'clientInfo': {'name': 'synthetic-offline-fixture', 'version': '1'}})
            client.send('notifications/initialized', {}, notification=True)
            available = client.send('tools/list', {})
            assert len(available['tools']) == 8
            assert client.call('get_request', {'wait_ms': 0})['status'] == 'pending'
            def synthetic_mac():
                full = copy.deepcopy(initial)
                try:
                    for i in range(4):
                        status, emitted = http_post(port, bearer, full)
                        assert status == 200, emitted
                        results.append(emitted)
                        if i < 3:
                            item = emitted['output'][0]
                            full['input'] += [item, {'type': 'function_call_output',
                                'call_id': item['call_id'], 'output': f'SYNTHETIC_STDIO_CALLBACK_{i+1}'}]
                except BaseException as exc:
                    errors.append(exc)
            mac = threading.Thread(target=synthetic_mac)
            mac.start()
            current = client.call('get_request', {'wait_ms': 5000})
            def observe(receipt):
                kinds.append(receipt['context']['kind'])
                view_bytes.append(len(json.dumps(receipt['context']).encode()))
                if receipt['context']['kind'] == 'delta':
                    assert 'history' not in receipt['context']
                    assert not receipt['context']['set_fields']
                    delta_outputs.append(receipt['context']['append'][-1]['output'])
            observe(current)
            lookup = client.call('discover_tools', {'request_id': current['request_id'],
                'context_token': current['context_token'], 'query': 'fixture_probe'})['matches'][0]
            schema = client.call('lookup_schema', {'request_id': current['request_id'],
                'context_token': current['context_token'], 'name': lookup['key'],
                'sha256': lookup['schema_sha256']})
            for i in range(1, 4):
                current = client.call('submit_action_and_wait_result', {
                    'request_id': current['request_id'], 'action_id': f'fixture-stdio-{i}',
                    'context_token': current['context_token'],
                    'schema_tokens': [schema['schema_token']] if i == 1 else [],
                    'response': response(i), 'wait_ms': 5000})
                assert current['status'] == 'ready', current
                observe(current)
            client.call('finish_request', {'request_id': current['request_id'],
                'action_id': 'fixture-stdio-final', 'context_token': current['context_token'],
                'schema_tokens': [], 'response': response(4, final=True)})
            mac.join(5)
            assert not mac.is_alive()
            if errors:
                raise errors[0]
            status = client.call('bridge_status')
            assert len(results) == 4
            assert kinds == ['full', 'delta', 'delta', 'delta']
            assert delta_outputs == [f'SYNTHETIC_STDIO_CALLBACK_{i}' for i in range(1,4)]
            assert status['diagnostics']['logical_actor_bindings'] == 1
            assert status['diagnostics']['response_commits'] == 4
            assert status['diagnostics']['http_response_emissions_started'] == 4
            assert sum(c['name'] == 'lookup_schema' for c in client.tool_calls) == 1
            return {'fixture': 'synthetic-native-and-Mac-driver', 'real_local_http': True,
                    'real_mcp_stdio_pipe': True, 'protocol_version': init['protocolVersion'],
                    'successive_callbacks': 3, 'native_platform_verified': False,
                    'actual_mac_tools_executed': False, 'external_model_api_used': False,
                    'context_kinds': kinds, 'model_visible_context_bytes': view_bytes,
                    'logical_actor_bindings': 1, 'exact_schema_lookups': 1,
                    'runtime_diagnostics': status['diagnostics'],
                    'tool_calls': client.tool_calls,
                    'timing_scope': 'local synthetic fixture only; excludes native inference, tunnel and Mac execution'}
        finally:
            stderr = client.close()
            assert bearer not in stderr
            assert 'SYNTHETIC_STDIO_CALLBACK_' not in stderr


if __name__ == '__main__':
    print(json.dumps(run_vertical_slice(), indent=2))
