"""Offline benchmark: JSON/HTTP over 127.0.0.1, SQLite FULL commits, no models.

The fixture HTTP handler intentionally uses fixed fake principals, NOT real
network authentication. It binds only loopback, contains no credentials/user
content, has a bounded lifetime, and MUST NOT be deployed or tunnelled.
"""
import argparse
from contextlib import contextmanager
from dataclasses import asdict
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import statistics
import tempfile
import threading
import time

from transport import Binding, Principal, Queue, QueueError, RouteAuthorization, ToolSurface
from transport.queue import canonical


@contextmanager
def fixture_server(surface, client, worker, *, lifetime=120):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                if self.path not in ('/fixture/client', '/fixture/native'):
                    raise QueueError('fixture_route_missing')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 2 * 1024 * 1024:
                    raise QueueError('invalid_body_length')
                self.connection.settimeout(5)
                message = json.loads(self.rfile.read(length))
                principal = client if self.path.endswith('/client') else worker
                result = surface.dispatch(principal, message['method'], message['arguments'])
                status = 200
            except (QueueError, KeyError, TypeError, ValueError) as exc:
                result = {'error': getattr(exc, 'code', 'invalid_fixture_request')}
                status = 400
            raw = canonical(result)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    worker_thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
    worker_thread.start()
    timer = threading.Timer(lifetime, server.shutdown)
    timer.daemon = True
    timer.start()
    try:
        yield server.server_address
    finally:
        timer.cancel()
        server.shutdown()
        server.server_close()
        worker_thread.join(5)
        if worker_thread.is_alive():
            raise RuntimeError('fixture_cleanup_failed')


class HttpLoopback:
    def __init__(self, address, actor):
        self.address, self.actor = address, actor
        self.transferred_bytes = 0

    def call(self, method, **arguments):
        request = canonical({'method': method, 'arguments': arguments})
        connection = http.client.HTTPConnection(*self.address, timeout=5)
        try:
            connection.request('POST', '/fixture/' + self.actor, request, {'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = response.read()
            self.transferred_bytes += len(request) + len(raw)
            body = json.loads(raw)
            if response.status != 200:
                raise QueueError(body['error'])
            return body
        finally:
            connection.close()


def quantiles(values):
    ordered = sorted(values)
    return {'min_ms': round(ordered[0], 3), 'median_ms': round(statistics.median(ordered), 3),
            'p95_ms': round(ordered[max(0, math.ceil(len(ordered) * .95) - 1)], 3),
            'max_ms': round(ordered[-1], 3)}


def run(turns=25):
    assert 1 <= turns <= 128
    output = {'scope': 'synthetic IPv4 loopback HTTP + JSON + SQLite FULL commits; no inference, TLS, tunnel or native model',
              'http_requests_per_roundtrip': 4, 'internal_execution_permits_per_roundtrip': 1,
              'database': 'SQLite rollback journal; synchronous=FULL; temporary local filesystem',
              'cases': []}
    with tempfile.TemporaryDirectory(prefix='direct-queue-bench-') as temp:
        for label, chars in [('small_delta', 1024), ('legacy_sized_request', 515 * 1024)]:
            queue = Queue(Path(temp) / (label + '.sqlite'))
            binding = Binding('fixture-grant', label, 'session', 'thread', 'gpt-6.1-sol', 'xhigh')
            client, native = Principal('fixture-client'), Principal('/root/fixture-native')
            queue.install_route(RouteAuthorization(binding, client.actor_id, native.actor_id, time.time() - 5,
                                                   time.time() + 120, 'fixture-offline-approval'))
            durations, stage_samples = [], {stage: [] for stage in ('enqueue', 'claim', 'permit', 'submit', 'get_result')}
            payload = {'text': 'x' * chars}
            with fixture_server(ToolSurface(queue, binding), client, native) as address:
                http_client, http_native = HttpLoopback(address, 'client'), HttpLoopback(address, 'native')
                ack = None
                for seq in range(1, turns + 1):
                    request_id = f'{label}-{seq}'
                    begin = time.perf_counter()
                    then = begin
                    http_client.call('enqueue_request', request_id=request_id, seq=seq, payload=payload, previous_result_id=ack)
                    stage_samples['enqueue'].append((time.perf_counter() - then) * 1000)
                    then = time.perf_counter()
                    request = http_native.call('claim_request', request_id=request_id)
                    stage_samples['claim'].append((time.perf_counter() - then) * 1000)
                    then = time.perf_counter()
                    assert queue.reserve_execution(native, binding, request_id)['execute']
                    stage_samples['permit'].append((time.perf_counter() - then) * 1000)
                    then = time.perf_counter()
                    saved = http_native.call('submit_result', request_id=request_id, result={'output': f'fixture-result-{seq}'})
                    stage_samples['submit'].append((time.perf_counter() - then) * 1000)
                    then = time.perf_counter()
                    result = http_client.call('get_result', request_id=request_id)
                    stage_samples['get_result'].append((time.perf_counter() - then) * 1000)
                    assert result['result'] == {'output': f'fixture-result-{seq}'}
                    ack = saved['result_id']
                    durations.append((time.perf_counter() - begin) * 1000)
                transferred = http_client.transferred_bytes + http_native.transferred_bytes
            output['cases'].append({'case': label, 'turns': turns, 'request_payload_bytes': len(canonical(payload)),
                                    'roundtrip': quantiles(durations),
                                    'stage_median_ms': {key: quantiles(values)['median_ms'] for key, values in stage_samples.items()},
                                    'http_json_body_bytes_total': transferred})
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--turns', type=int, default=25)
    parser.add_argument('--output')
    arguments = parser.parse_args()
    result = run(arguments.turns)
    text = json.dumps(result, indent=2) + '\n'
    if arguments.output:
        Path(arguments.output).write_text(text)
    print(text, end='')
