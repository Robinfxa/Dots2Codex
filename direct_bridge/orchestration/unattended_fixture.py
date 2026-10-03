"""Cloud-only native-controller test harness, never a production scheduler.

A real native task may use ``call`` over a private file-mailbox test shim
(or localhost HTTP when the caller shares the server network namespace). The shim
forwards the eight unchanged tools to the real SDK MCP stdio subprocess. Codex
HTTP requests are synthetic; answers must be provided by the external native task
for native evidence. No model API, native spawn, live tunnel or Mac access exists
in this module. The separate regression test module is wholly synthetic and labels itself accordingly.
"""
from __future__ import annotations

import argparse
import copy
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_global_mcp import ProductionClient, free_port
from facade.test_global_runtime import config, http, request

PAIR = {'model': 'gpt-6-astra', 'reasoning_effort': 'xhigh'}


def private_json(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as file:
        json.dump(value, file, indent=2)
        file.write('\n')
    os.replace(tmp, path)


class FixtureMCPClient(ProductionClient):
    # The general short-running pipe test client defaults to four seconds,
    # shorter than Direct's supported five-second application wait. Keep this
    # fixture transport deadline above that wait and below the mailbox deadline.
    def recv(self, identifier, timeout=8):
        return super().recv(identifier, timeout=timeout)


class UnattendedFixture:
    """Own only fixture-created processes, ports and an isolated private DB."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or self.directory.stat().st_mode & 0o077:
            raise ValueError('fixture_directory_must_be_private')
        if any(self.directory.iterdir()):
            raise ValueError('fixture_directory_must_be_empty')
        self.cfg = config(str(self.directory))
        self.cfg.update(config_id='cloud-unattended-' + secrets.token_hex(8),
                        http_bearer=secrets.token_hex(32), http_wait_ms=300000)
        value = {k: v for k, v in self.cfg.items() if k != 'http_bearer'}
        value['trust_mode'] = 'single_owner_stdio'
        self.config_path = self.directory / 'config.json'
        private_json(self.config_path, value)
        self.token = secrets.token_hex(32)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.events = []
        self.turns = []
        self.failures = []
        self.history = []
        self.client = None
        self.server = None
        self.client_thread = None
        self.identity = {'session-id': 'cloud-native-test', 'thread-id': 'one-retained-context'}
        self.restart_count = 0
        self.stage = 'created'
        self.start_bridge()

    def record(self, event, **values):
        self.events.append({'event': event, 'monotonic': time.monotonic(), **values})

    def start_bridge(self):
        self.port = free_port()
        self.client = FixtureMCPClient(self.config_path, self.cfg['http_bearer'], self.port)
        self.client.initialize()
        self.record('mcp_stdio_started', port=self.port)

    def call(self, name, args):
        # The test MCP pipe client is single-reader and must stay serialized.
        with self.lock:
            result = self.client.tool(name, args)
            self.record('mcp_call', tool=name, status=result.get('status'),
                        seq=result.get('seq'), replayed=result.get('replayed'),
                        worker_id=result.get('logical_worker_id'))
            return result

    def restart_bridge(self):
        with self.lock:
            if self.stage != 'awaiting_restart':
                raise ValueError('restart_requires_two_settled_turns')
            self.client.close()
            self.restart_count += 1
            self.start_bridge()
            self.stage = 'restart_complete'
            self.record('bridge_restarted_same_database')
        return {'status': 'restarted', 'restart_count': self.restart_count}

    def post_turn(self, number, text, expected):
        value = request()
        value['instructions'] = 'Cloud-only native lifecycle test. No external or local tools are needed. ' \
            'Return the exact requested short string as your final answer.'
        value['tools'] = []
        value['input'] = copy.deepcopy(self.history) + [{'role': 'user', 'content': text}]
        self.record('synthetic_codex_post_started', turn=number)
        # Production http helper has a short test timeout; this native test must
        # permit actual model and platform scheduling latency.
        conn = HTTPConnection('127.0.0.1', self.port, timeout=310)
        headers = {'Authorization': 'Bearer ' + self.cfg['http_bearer'],
                   'Content-Type': 'application/json', **self.identity}
        conn.request('POST', '/v1/responses', json.dumps(value), headers)
        reply = conn.getresponse()
        status, raw = reply.status, reply.read()
        conn.close()
        if status != 200:
            raise AssertionError(('http_status', status, raw.decode()))
        event = next(block for block in raw.decode().split('\n\n')
                     if block.startswith('event: response.completed\n'))
        response = json.loads(event.split('data: ', 1)[1])['response']
        answer = ''.join(item.get('text', '') for message in response['output']
                         for item in message.get('content', []))
        if answer != expected:
            raise AssertionError(('unexpected_native_answer', number, answer, expected))
        self.history = value['input'] + response['output']
        self.turns.append({'turn': number, 'answer': answer, 'response_id': response['id'],
                           'request': value})
        self.record('synthetic_codex_response_received', turn=number, answer=answer)

    def begin(self):
        if self.stage != 'created':
            raise ValueError('scenario_already_started')
        self.stage = 'first_two_turns'
        def run():
            try:
                if self.stop.wait(2):
                    return
                self.post_turn(1, 'Remember this code in your context: HARBOUR-47. Return exactly HARBOUR-47.', 'HARBOUR-47')
                if self.stop.wait(2):
                    return
                self.post_turn(2, 'Using the original code, double its numeric suffix. Return only the resulting code.', 'HARBOUR-94')
                self.stage = 'awaiting_restart'
            except Exception as exc:
                self.failures.append(str(exc))
                self.stage = 'failed'
        self.client_thread = threading.Thread(target=run, daemon=True)
        self.client_thread.start()
        return {'status': 'scheduled', 'first_delay_seconds': 2, 'inter_turn_delay_seconds': 2}

    def continue_after_restart(self):
        if self.stage != 'restart_complete':
            raise ValueError('restart_required_before_third_turn')
        self.stage = 'third_turn'
        def run():
            try:
                if self.stop.wait(2):
                    return
                self.post_turn(3, 'Using the original code remembered before the restart, triple its numeric suffix. Return only the resulting code.', 'HARBOUR-141')
                self.stage = 'complete'
            except Exception as exc:
                self.failures.append(str(exc))
                self.stage = 'failed'
        self.client_thread = threading.Thread(target=run, daemon=True)
        self.client_thread.start()
        return {'status': 'scheduled', 'delay_seconds': 2}

    def replay_last_http(self):
        if not self.turns:
            raise ValueError('no_settled_turn')
        code, response = http(self.port, self.cfg['http_bearer'], self.identity, self.turns[-1]['request'])
        if code != 200 or response['id'] != self.turns[-1]['response_id']:
            raise AssertionError('text_replay_mismatch')
        self.record('duplicate_http_text_replayed_without_native_work')
        return {'status': 'replayed', 'response_id': response['id']}

    def report(self):
        status = self.call('bridge_status', {})
        return {'scope': 'Cloud test shim (private mailbox or loopback HTTP) -> real SDK MCP stdio -> production GlobalRuntime; synthetic Codex HTTP client',
                'native_worker_evidence': 'Only external native launch/resume receipts can establish actual native execution',
                'actual_native_platform_verified_by_bridge': False, 'live_mac_verified': False,
                'deployed_mcp_to_native_wake_verified': False, 'automatic_wake': False,
                'external_model_api_calls': 0, 'stage': self.stage, 'restart_count': self.restart_count,
                'turns': [{k: v for k, v in turn.items() if k != 'request'} for turn in self.turns],
                'failures': self.failures, 'status': status, 'events': self.events.copy()}

    def close(self):
        self.stop.set()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.client is not None:
            self.client.close()


def rpc(info_path, command, payload=None):
    info = json.loads(Path(info_path).read_text())
    if 'mailbox' in info:
        mailbox = Path(info['mailbox'])
        identifier = secrets.token_hex(16)
        incoming = mailbox / (identifier + '.request.json')
        outgoing = mailbox / (identifier + '.response.json')
        private_json(incoming, {'operation': command, 'payload': payload or {}})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if outgoing.exists():
                value = json.loads(outgoing.read_text())
                outgoing.unlink()
                if 'error' in value:
                    raise ValueError(value['error'])
                return value['result']
            time.sleep(.02)
        raise TimeoutError('fixture_mailbox_timeout_result_unknown_do_not_replay')
    conn = HTTPConnection('127.0.0.1', info['port'], timeout=12)
    conn.request('POST', '/' + command, json.dumps(payload or {}),
                 {'Authorization': 'Bearer ' + info['token'], 'Content-Type': 'application/json'})
    reply = conn.getresponse()
    value = json.loads(reply.read())
    conn.close()
    if reply.status != 200:
        raise ValueError(value['error'])
    return value


def serve(directory, ready_path, lifetime, transport='mailbox'):
    fixture = UnattendedFixture(directory)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            if not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + fixture.token):
                self.send_error(401)
                return
            try:
                size = int(self.headers.get('Content-Length', 0))
                if not 0 < size <= 1024 * 1024:
                    raise ValueError('bounded_body_required')
                body = json.loads(self.rfile.read(size))
                if self.path == '/call':
                    result = fixture.call(body['name'], body.get('arguments', {}))
                elif self.path == '/begin':
                    result = fixture.begin()
                elif self.path == '/restart':
                    result = fixture.restart_bridge()
                elif self.path == '/continue':
                    result = fixture.continue_after_restart()
                elif self.path == '/replay':
                    result = fixture.replay_last_http()
                elif self.path == '/report':
                    result = fixture.report()
                elif self.path == '/stop':
                    fixture.stop.set()
                    result = {'status': 'stopping'}
                else:
                    raise ValueError('unknown_fixture_command')
                code = 200
            except Exception as exc:
                result, code = {'error': str(exc)}, 400
            raw = json.dumps(result).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    if transport == 'mailbox':
        mailbox = Path(directory) / 'mailbox'
        mailbox.mkdir(mode=0o700)
        def pump():
            while not fixture.stop.wait(.01):
                for incoming in sorted(mailbox.glob('*.request.json')):
                    outgoing = incoming.with_name(incoming.name.replace('.request.json', '.response.json'))
                    try:
                        body = json.loads(incoming.read_text())
                        op, payload = body['operation'], body['payload']
                        operations = {'begin': fixture.begin, 'restart': fixture.restart_bridge,
                                      'continue': fixture.continue_after_restart,
                                      'replay': fixture.replay_last_http, 'report': fixture.report}
                        if op == 'call':
                            result = fixture.call(payload['name'], payload.get('arguments', {}))
                        elif op == 'stop':
                            result = {'status': 'stopping'}
                            fixture.stop.set()
                        elif op in operations:
                            result = operations[op]()
                        else:
                            raise ValueError('unknown_fixture_command')
                        value = {'result': result}
                    except Exception as exc:
                        value = {'error': str(exc)}
                    private_json(outgoing, value)
                    incoming.unlink()
        threading.Thread(target=pump, daemon=True).start()
        private_json(ready_path, {'mailbox': str(mailbox),
                                 'transport': 'private cloud-file mailbox test shim, not deployed MCP',
                                 'pair': PAIR})
    else:
        fixture.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=fixture.server.serve_forever, daemon=True).start()
        private_json(ready_path, {'port': fixture.server.server_port, 'token': fixture.token,
                                 'transport': 'private cloud-loopback test shim, not deployed MCP',
                                 'pair': PAIR})
    signal.signal(signal.SIGTERM, lambda *_: fixture.stop.set())
    signal.signal(signal.SIGINT, lambda *_: fixture.stop.set())
    print(json.dumps({'status': 'ready', 'ready_file': str(ready_path), 'max_lifetime_seconds': lifetime}), flush=True)
    try:
        fixture.stop.wait(lifetime)
        private_json(Path(directory) / 'final-report.json', fixture.report())
    finally:
        fixture.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    server = sub.add_parser('serve')
    server.add_argument('--state-dir', required=True)
    server.add_argument('--ready-file', required=True)
    server.add_argument('--transport', choices=['mailbox', 'http'], default='mailbox')
    server.add_argument('--lifetime-seconds', type=int, default=1200, choices=range(60, 3601), metavar='60..3600')
    client = sub.add_parser('rpc')
    client.add_argument('--ready-file', required=True)
    client.add_argument('operation', choices=['call', 'begin', 'restart', 'continue', 'replay', 'report', 'stop'])
    client.add_argument('--json', default='{}')
    args = parser.parse_args()
    if args.command == 'serve':
        serve(args.state_dir, args.ready_file, args.lifetime_seconds, args.transport)
    else:
        print(json.dumps(rpc(args.ready_file, args.operation, json.loads(args.json)), indent=2))


if __name__ == '__main__':
    main()
