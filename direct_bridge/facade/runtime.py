"""Single-owner experimental loopback facade. No model API or tool execution.

The stdio MCP ingress and configured logical actor are a trusted single-owner
boundary, not cryptographic native-platform identity. Real tunnel, native child,
and Mac acceptance require separate live tests. State after restart is fail-closed.
"""
from __future__ import annotations
from dataclasses import asdict
import copy
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

from context.incremental import ContextStore, NativeContinuity, strict_loads, tool_key, digest
from transport import Queue, Binding, Principal, RouteAuthorization
from .wire import validate_request, validate_response, validate_history, has_tools, response_events
from .wire_support import canonical, require

MAX_BODY = 1024 * 1024


class BridgeRuntime:
    def __init__(self, config):
        self.config = dict(config)
        self.binding = Binding(**config['binding'])
        self.client = Principal(config['client_actor'])
        self.worker = Principal(config['worker_actor'])
        self.continuity = NativeContinuity(self.worker.actor_id, config['context_epoch'])
        self.queue = Queue(config['db_path'])
        self.queue.install_route(RouteAuthorization(self.binding, self.client.actor_id,
            self.worker.actor_id, config['not_before'], config['expires_at'], config['approval_ref']))
        self.context = ContextStore(asdict(self.binding), source_actor=self.client.actor_id)
        self.cv = threading.Condition(threading.RLock())
        self.requests = []
        self.by_id = {}
        self.actions = {}
        self.context_tokens = {}
        self.schema_tokens = {}
        self.acknowledged_context = set()
        self.max_wait_ms = config.get('max_wait_ms', 5000)
        self.http_wait_ms = config.get('http_wait_ms', 30000)
        require(type(self.max_wait_ms) is int and 1 <= self.max_wait_ms <= 30000, 'invalid_wait_limit')
        require(type(self.http_wait_ms) is int and 1 <= self.http_wait_ms <= 300000, 'invalid_http_wait_limit')
        self.bearer = config['http_bearer']
        require(isinstance(self.bearer, str) and len(self.bearer) >= 16 and self.bearer.isascii(),
                'local_http_bearer_required')
        self.ledger = []
        self.http = None
        self.http_thread = None
        self.http_address = None
        self.closed = False
        self._record('logical_native_actor_bound', actor_id=self.worker.actor_id,
                     actual_platform_identity_verified=False)

    def _record(self, event, **fields):
        self.ledger.append({'event': event, 'monotonic_ns': time.monotonic_ns(), **fields})

    def _wait(self, predicate, wait_ms, cancel_event):
        require(type(wait_ms) is int and 0 <= wait_ms <= self.max_wait_ms, 'invalid_wait_ms')
        end = time.monotonic() + wait_ms / 1000
        while not predicate():
            if self.closed or (cancel_event and cancel_event.is_set()):
                return False
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            self.cv.wait(min(remaining, 0.1))
        return True

    def _lookup(self, request_id):
        require(request_id in self.by_id, 'unknown_request')
        return self.by_id[request_id]

    def ingest(self, request, request_id=None):
        """Called only by bearer-authenticated HTTP ingress; never by MCP data."""
        request = validate_request(request)
        require(request['model'] == self.binding.model and
                request['reasoning']['effort'] == self.binding.reasoning_effort,
                'immutable_model_effort_mismatch')
        rid = request_id or 'request-' + digest(request)
        with self.cv:
            if rid in self.by_id:
                existing = self.by_id[rid]
                require(canonical(existing['request']) == canonical(request), 'request_id_conflict')
                return existing
            previous = self.requests[-1] if self.requests else None
            if previous:
                require(previous.get('response') is not None, 'previous_request_unsettled')
                require(previous['delivery_started'], 'previous_response_not_emitted')
                validate_history(request, previous['request'], [previous['response']],
                    prior_call_hashes=self._prior_calls())
            else:
                validate_history(request, None, [])
            seq = len(self.requests) + 1
            # Validate full snapshot locally. The model receives only a projection.
            candidate_context = copy.deepcopy(self.context)
            candidate_context.ingest_full(actor=self.client.actor_id, binding=asdict(self.binding),
                                          revision=seq, request=request)
            payload = {'source_revision': seq, 'source_sha256': digest(request)}
            receipt = self.queue.enqueue_request(self.client, self.binding, rid, seq, payload,
                previous_result_id=previous['queue_result_id'] if previous else None)
            self.context = candidate_context
            record = {'id': rid, 'seq': seq, 'request': copy.deepcopy(request),
                      'receipt': receipt, 'response': None, 'delivery_started': False,
                      'context_token': None, 'queue_result_id': None}
            self.requests.append(record)
            self.by_id[rid] = record
            self._record('http_request_ingested', request_id=rid, seq=seq,
                         full_source_bytes=len(canonical(request)))
            self.cv.notify_all()
            return record

    def _prior_calls(self):
        from .wire import call_binding
        from .wire_support import sha256
        return {item['call_id']: sha256(canonical(call_binding(item)))
                for request in self.requests if request.get('response')
                for item in request['response']['output']
                if item.get('type') in {'function_call', 'custom_tool_call'}}

    def _deliver_request(self, record):
        # Same read receipt can be retried, without consuming another execution permit.
        self.queue.claim_request(self.worker, self.binding, record['id'])
        if record['context_token']:
            token = record['context_token']
            delivery = self.context_tokens[token]
            return {'status': 'ready', 'request_id': record['id'], 'seq': record['seq'],
                    'context_token': token, 'context': delivery.tool_result(), 'replayed': True}
        delivery = self.context.prepare_delivery(self.continuity)
        output = delivery.tool_result()
        permit = self.queue.reserve_execution(self.worker, self.binding, record['id'])
        require(permit['execute'], 'execution_admission_unknown_no_replay')
        record['context_token'] = delivery.token
        self.context_tokens[delivery.token] = delivery
        self._record('native_context_return_prepared', request_id=record['id'],
                     kind=output['kind'], payload_bytes=len(delivery.payload_bytes))
        return {'status': 'ready', 'request_id': record['id'], 'seq': record['seq'],
                'context_token': delivery.token, 'context': output, 'replayed': False}

    def _ack_context(self, record, token):
        require(token == record['context_token'] and token in self.context_tokens,
                'context_receipt_required')
        if token not in self.acknowledged_context:
            self.context.acknowledge_delivery(self.context_tokens[token])
            self.acknowledged_context.add(token)
            self._record('native_context_receipt_acknowledged', request_id=record['id'])

    def _next(self, seq, wait_ms, cancel_event):
        require(type(seq) is int and seq >= 0, 'invalid_after_seq')
        found = lambda: len(self.requests) > seq
        if not self._wait(found, wait_ms, cancel_event):
            self._record('native_wait_pending', after_seq=seq)
            return {'status': 'pending', 'after_seq': seq,
                    'effect': 'unknown', 'resubmit_action': False}
        # Never skip a request by choosing a future cursor.
        record = self.requests[seq]
        require(record['response'] is None, 'request_already_answered')
        return self._deliver_request(record)

    def _submit(self, args, final):
        record = self._lookup(args['request_id'])
        action_id = args['action_id']
        require(isinstance(action_id, str) and action_id, 'action_id_required')
        fingerprint = digest({'request_id': record['id'], 'response': args['response'],
                              'context_token': args['context_token'],
                              'schema_tokens': args.get('schema_tokens', [])})
        if action_id in self.actions:
            action = self.actions[action_id]
            require(action['fingerprint'] == fingerprint, 'action_id_conflict')
            require(action['final'] == final, 'action_mode_conflict')
            return record
        require(record is self.requests[-1] and record['response'] is None,
                'request_already_answered_or_stale')
        self._ack_context(record, args['context_token'])
        for token in args.get('schema_tokens', []):
            require(token in self.schema_tokens, 'unknown_schema_receipt')
            key, sha, context_token = self.schema_tokens[token]
            require(context_token == record['context_token'], 'schema_receipt_wrong_context')
            self.context.acknowledge_schema_delivery(continuity=self.continuity,
                                                     key=key, expected_digest=sha)
        response = validate_response(args['response'], record['request'])
        require(has_tools(response) != final, 'use_finish_for_message_or_submit_for_tools')
        for item in response['output']:
            if item.get('type') in {'function_call', 'custom_tool_call'}:
                self.context.require_schema(continuity=self.continuity,
                    key=tool_key(item.get('namespace'), item['name']))
        # Entire validation precedes immutable result publication.
        self.context.record_output(continuity=self.continuity, items=response['output'])
        receipt = self.queue.submit_result(self.worker, self.binding, record['id'], response)
        require(receipt['state'] == 'completed', 'request_cancelled')
        record['response'] = response
        record['queue_result_id'] = receipt['result_id']
        self.actions[action_id] = {'request_id': record['id'], 'fingerprint': fingerprint, 'final': final}
        self._record('native_response_committed', request_id=record['id'], action_id=action_id,
                     final=final)
        self.cv.notify_all()
        return record

    def call_tool(self, name, args, *, cancel_event=None):
        require(type(args) is dict, 'object_arguments_required')
        allowed = {
            'bridge_status': set(), 'get_request': {'after_seq', 'wait_ms'},
            'discover_tools': {'request_id', 'context_token', 'query', 'limit'},
            'lookup_schema': {'request_id', 'context_token', 'name', 'sha256'},
            'submit_action_and_wait_result': {'request_id', 'action_id', 'context_token', 'schema_tokens', 'response', 'wait_ms'},
            'finish_request': {'request_id', 'action_id', 'context_token', 'schema_tokens', 'response'},
            'await_result': {'request_id', 'action_id', 'wait_ms'},
            'cancel_request': {'request_id'},
        }
        require(name in allowed and not (set(args) - allowed[name]), 'unknown_tool_or_arguments')
        if 'wait_ms' in args:
            require(type(args['wait_ms']) is int and 0 <= args['wait_ms'] <= self.max_wait_ms,
                    'invalid_wait_ms')
        with self.cv:
            if name == 'bridge_status':
                return {'status': 'ready', 'binding': asdict(self.binding),
                        'logical_actor_id': self.worker.actor_id, 'requests': len(self.requests),
                        'http_address': self.http_address,
                        'actual_native_platform_verified': False, 'mac_execution_verified': False,
                        'model_api_used': False,
                        'diagnostics': {
                            'logical_actor_bindings': 1,
                            'full_context_returns': sum(e.get('kind') == 'full' for e in self.ledger),
                            'delta_context_returns': sum(e.get('kind') == 'delta' for e in self.ledger),
                            'response_commits': sum(e['event'] == 'native_response_committed' for e in self.ledger),
                            'http_response_emissions_started': sum(e['event'] == 'http_response_delivery_started' for e in self.ledger),
                            'context_payload_bytes': [e['payload_bytes'] for e in self.ledger if 'payload_bytes' in e]}}

            if name == 'get_request':
                return self._next(args.get('after_seq', 0), args.get('wait_ms', 1000), cancel_event)
            if name in {'discover_tools', 'lookup_schema'}:
                record = self._lookup(args['request_id'])
                require(record is self.requests[-1] and record['response'] is None, 'stale_request')
                self._ack_context(record, args['context_token'])
                if name == 'discover_tools':
                    return self.context.discover(args['query'], limit=args.get('limit', 8))
                exact = self.context.exact_schema(args['name'], args['sha256'])
                token = digest({'request_id': record['id'], 'context_token': record['context_token'],
                                'key': args['name'], 'sha': args['sha256']})
                self.schema_tokens[token] = (args['name'], args['sha256'], record['context_token'])
                return {**exact, 'schema_token': token}
            if name in {'submit_action_and_wait_result', 'finish_request'}:
                record = self._submit(args, name == 'finish_request')
                if name == 'finish_request':
                    return {'status': 'completed', 'request_id': record['id'],
                            'action_id': args['action_id'], 'http_delivery_confirmed': False}
                return self._next(record['seq'], args.get('wait_ms', 1000), cancel_event)
            if name == 'await_result':
                require(args['action_id'] in self.actions, 'unknown_action')
                action = self.actions[args['action_id']]
                require(action['request_id'] == args['request_id'] and not action['final'],
                        'action_binding_mismatch')
                return self._next(self._lookup(args['request_id'])['seq'],
                                  args.get('wait_ms', 1000), cancel_event)
            if name == 'cancel_request':
                record = self._lookup(args['request_id'])
                if record['response'] is not None:
                    return {'status': 'too_late_result_committed', 'request_id': record['id'],
                            'effect': 'unknown', 'resubmit_action': False}
                result = self.queue.cancel_request(self.client, self.binding, record['id'])
                self._record('cancellation_recorded', request_id=record['id'],
                             effect='unknown' if record['delivery_started'] else 'not_emitted')
                self.cv.notify_all()
                return result
            raise ValueError('unknown_tool')

    def start_http(self, host='127.0.0.1', port=0):
        require(host == '127.0.0.1', 'loopback_only')
        require(self.http is None, 'http_already_started')
        runtime = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *unused):
                pass
            def _json(self, status, value):
                body = canonical(value)
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True
            def do_POST(self):
                self.connection.settimeout(10)
                expected_host = f'127.0.0.1:{runtime.http.server_address[1]}'
                if self.headers.get('Host') != expected_host or self.headers.get('Origin'):
                    return self._json(403, {'error': 'loopback_origin_required'})
                auth = self.headers.get('Authorization', '')
                if not auth.isascii() or not hmac.compare_digest(auth, 'Bearer ' + runtime.bearer):
                    return self._json(401, {'error': 'authorization_required'})
                if self.path not in ('/responses', '/v1/responses'):
                    return self._json(404, {'error': 'not_found'})
                length = self.headers.get('Content-Length', '')
                if (len(self.headers.get_all('Authorization', [])) != 1
                        or len(self.headers.get_all('Content-Length', [])) != 1
                        or self.headers.get('Transfer-Encoding') is not None):
                    return self._json(400, {'error': 'ambiguous_http_framing'})
                if len(length) > 7 or not length.isascii() or not length.isdecimal() or not 0 < int(length) <= MAX_BODY:
                    return self._json(413, {'error': 'bounded_content_length_required'})
                try:
                    body = self.rfile.read(int(length))
                    require(len(body) == int(length), 'incomplete_body')
                    record = runtime.ingest(strict_loads(body), self.headers.get('X-Dots-Request-Id'))
                    end = time.monotonic() + runtime.http_wait_ms / 1000
                    with runtime.cv:
                        while record['response'] is None and not runtime.closed:
                            receipt = runtime.queue.get_result(runtime.client, runtime.binding, record['id'])
                            if receipt['state'] == 'cancelled' or receipt['cancel_requested']:
                                return self._json(409, {'error': 'request_cancelled', 'request_id': record['id']})
                            remaining = end - time.monotonic()
                            if remaining <= 0:
                                return self._json(504, {'error': 'outcome_pending', 'request_id': record['id'],
                                                       'resubmit_new_action': False})
                            runtime.cv.wait(min(remaining, 0.1))
                        require(record['response'] is not None, 'bridge_closed')
                        require(not record['delivery_started'], 'response_delivery_already_started')
                        # At-most-once local emission fence; a broken socket remains unknown.
                        record['delivery_started'] = True
                        runtime._record('http_response_delivery_started', request_id=record['id'])
                        payload = response_events(record['response'])
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(payload)))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(payload)
                    self.close_connection = True
                except (ValueError, KeyError, TypeError) as exc:
                    return self._json(409, {'error': str(exc) if isinstance(exc, ValueError) else 'invalid_arguments'})
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    # No re-emission; request outcome is uncertain to the client.
                    return
        class QuietServer(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                # Never send payload-bearing traceback/headers to inherited stderr.
                runtime._record('http_handler_failed')
        self.http = QuietServer((host, port), Handler)
        self.http.daemon_threads = True
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        self.http_address = f'http://127.0.0.1:{self.http.server_address[1]}/v1'
        return self.http_address

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()
        if self.http:
            self.http.shutdown()
            self.http.server_close()
        if self.http_thread:
            self.http_thread.join(timeout=2)


def create_runtime(config):
    return BridgeRuntime(config)
