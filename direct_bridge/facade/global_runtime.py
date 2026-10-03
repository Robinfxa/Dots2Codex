"""Durable multi-conversation Responses-to-native-MCP handoff.

No inference, child spawning, client tool execution, browser, cloud storage, or
background wake mechanism lives here. A trusted single owner coordinates actual
native children. Claimed IDs are logical bindings, never platform attestation.
"""
from __future__ import annotations

import copy
from dataclasses import asdict
import fcntl
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time
from urllib.parse import urlsplit, parse_qs

from diagnostics import Diagnostics, duration_ms

from context.incremental import (ContextStore, NativeContinuity, Delivery,
                                 strict_loads, tool_key, catalog_key, digest)
from .wire import validate_request, validate_response, validate_history, has_tools, response_events, CLIENT_CALL_TYPES, request_tools
from .wire_support import canonical, require

MAX_BODY = 1024 * 1024
MAX_ROUTE_BYTES = 32 * 1024 * 1024
CONTRACT = 'dots-direct-global/1'
PAIR_KEYS = {'model', 'reasoning_effort'}
INTERNAL_TOOLS = frozenset({'bridge_status', 'get_request', 'discover_tools', 'lookup_schema',
    'submit_action_and_wait_result', 'await_result', 'finish_request', 'cancel_request',
    'prepare_hosted_call', 'record_hosted_result'})


# Explicit compile-time protocol labels only. Never forward arbitrary exception text.
SAFE_GLOBAL_ERRORS = frozenset({
    'action_binding_mismatch',
    'action_id_conflict',
    'action_id_required',
    'ambiguous_idempotency_key',
    'ambiguous_legacy_session_header',
    'ambiguous_reasoning_effort',
    'append_must_be_list',
    'arguments_string_required',
    'authorization_expired',
    'authorization_required',
    'base_reference_mismatch',
    'binding_mismatch',
    'bounded_content_length_required',
    'bridge_closed',
    'bridge_transport_tool_intent_forbidden',
    'cancel_pending_request_before_close',
    'cancelled_route_requires_new_conversation',
    'canonical_runtime_identity_required',
    'clock_rollback_new_work_paused',
    'close_route_or_request_required',
    'completed_response_required',
    'conflicting_field_changes',
    'context_receipt_mismatch',
    'context_receipt_required',
    'context_working_set_too_large',
    'cursor_ahead_of_route',
    'custom_input_string_required',
    'default_pair_not_authorized',
    'deleted_field_absent',
    'delivery_not_current',
    'delivery_outcome_unknown_no_reemission',
    'duplicate_json_key',
    'duplicate_or_invalid_item_id',
    'duplicate_or_invalid_response_call_id',
    'duplicate_or_invalid_tool_call',
    'duplicate_or_missing_call_id',
    'duplicate_tool',
    'exact_current_schema_not_delivered',
    'explicit_reasoning_effort_required',
    'forbidden_http_header',
    'full_history_array_required',
    'full_history_prefix_mismatch',
    'full_snapshot_required',
    'get_body_not_supported',
    'global_authority_mismatch',
    'global_runtime_already_running',
    'history_must_be_list',
    'history_or_tools_override_forbidden',
    'history_prefix_changed',
    'http_waiter_capacity_exhausted',
    'immutable_route_model_effort_mismatch',
    'incomplete_body',
    'initial_revision_must_be_one',
    'invalid_additional_tools',
    'invalid_after_seq',
    'invalid_assistant_message',
    'invalid_binding',
    'invalid_deleted_field',
    'invalid_discovery_limit',
    'invalid_field_changes',
    'invalid_history_item',
    'invalid_http_wait_limit',
    'invalid_idempotency_key',
    'invalid_input_item',
    'invalid_instructions',
    'invalid_json',
    'invalid_message_role',
    'invalid_or_oversize_response',
    'invalid_output',
    'invalid_persisted_route',
    'invalid_request',
    'invalid_request_capacity',
    'invalid_response_item',
    'invalid_revision',
    'invalid_route_capacity',
    'invalid_source_actor',
    'invalid_tool',
    'invalid_tool_call_name',
    'invalid_tool_call_payload',
    'invalid_tool_name',
    'invalid_tool_namespace',
    'invalid_tool_schema',
    'invalid_tools',
    'invalid_wait_limit',
    'invalid_wait_ms',
    'json_content_type_required',
    'local_http_bearer_required',
    'loopback_host_required',
    'loopback_listener_required',
    'model_required',
    'native_claim_fields_required',
    'native_claim_pair_mismatch',
    'native_route_not_claimed',
    'native_route_owner_immutable',
    'native_worker_already_bound',
    'nested_namespace_unsupported',
    'nonempty_discovery_query_required',
    'numeric_precision_loss',
    'object_arguments_required',
    'output_already_committed',
    'output_without_current_delivery',
    'pair_not_authorized',
    'pending_delivery_continuity_changed',
    'persisted_route_too_large',
    'previous_assistant_message_missing',
    'previous_delivery_unacknowledged',
    'previous_request_unsettled',
    'previous_response_id_unsupported_send_full_history',
    'previous_response_not_emitted',
    'private_owner_file_required',
    'private_state_directory_required',
    'private_state_file_required',
    'request_already_answered_or_stale',
    'request_cancelled',
    'request_cancelled_or_route_closed',
    'request_id_conflict',
    'required_tool_arguments_missing',
    'reserved_projection_field',
    'response_error_not_success',
    'response_id_required',
    'response_item_id_required',
    'response_model_mismatch',
    'response_output_required',
    'revision_already_delivered',
    'revision_not_next',
    'route_archive_capacity_exhausted',
    'route_capacity_exhausted',
    'route_claim_required',
    'route_closed',
    'route_closed_start_new_conversation',
    'route_request_capacity_exhausted',
    'route_state_capacity_exhausted',
    'schema_receipt_wrong_context',
    'schema_without_native_continuity',
    'stale_request',
    'stale_schema_reference',
    'stream_true_required',
    'symlink_state_path_rejected',
    'text_content_required',
    'text_response_required',
    'tool_arguments_object_required',
    'tool_arguments_schema_mismatch',
    'tool_history_does_not_match_delivered_calls',
    'tool_item_id_prefix_suffix_required',
    'tool_outcome_unknown',
    'tool_outcome_unknown_route_not_settled',
    'tool_output_kind_mismatch',
    'tool_schema_anchor_duplicate',
    'tool_schema_dialect_unsupported',
    'tool_schema_reference_invalid',
    'tool_schema_reference_unresolvable',
    'tool_schema_reference_unsupported',
    'tool_schema_required',
    'tool_schema_resource_duplicate',
    'tool_schema_too_complex',
    'tool_schema_too_deep',
    'tool_schema_validation_too_complex',
    'tool_schema_validator_unavailable',
    'tools_must_be_list',
    'unadvertised_tool',
    'uncorrelated_tool_output',
    'unknown_native_continuity',
    'unknown_or_duplicate_tool_output',
    'unknown_request',
    'unknown_route',
    'unknown_schema_receipt',
    'unknown_tool',
    'unknown_tool_arguments',
    'unknown_tool_or_arguments',
    'unsupported_catalog_query',
    'unsupported_custom_tool_format',
    'unsupported_encoding',
    'unsupported_endpoint',
    'unsupported_input_item',
    'unsupported_response_item',
    'unsupported_tool_type',
    'untrusted_context_source',
    'use_finish_for_message_or_submit_for_tools',
    'wire_request_too_large',
})

from .hosted import WEB_ERRORS
SAFE_GLOBAL_ERRORS = SAFE_GLOBAL_ERRORS | WEB_ERRORS | frozenset({
    'unsupported_hosted_tool', 'unsupported_tool_search_execution', 'invalid_tool_search_declaration',
    'invalid_tool_search_call', 'invalid_tool_search_output', 'conflicting_namespace_metadata',
    'invalid_tool_search_execution', 'final_message_required', 'unsupported_response_format', 'response_format_mismatch',
    'unsupported_image_source', 'invalid_image_content', 'image_content_too_large',
    'image_dimensions_too_large', 'image_count_exceeded', 'unsupported_tool_option',
    'unsupported_background_response', 'unsupported_response_storage',
    'unsupported_request_truncation', 'unsupported_generation_option',
})

def _internal_tool(name, namespace=None):
    if not isinstance(name, str):
        return False
    # Match the observed connected spelling as well as explicit namespaces:
    # mcp__codex_apps__dots2codex_direct_get_request. Generic homonyms alone
    # are not positive transport identity and must remain ordinary client tools.
    candidates = [tool for tool in INTERNAL_TOOLS if name == tool or name.endswith('_' + tool)]
    if not candidates:
        return False
    tail = max(candidates, key=len)
    origin = re.sub(r'[^a-z0-9]', '', (str(namespace or '') + name[:-len(tail)]).lower())
    return any(marker in origin for marker in ('dots2codexdirect', 'directbridge', 'dotsdirect'))


def _context_request(request):
    """Hide this transport's own tools; it must never recursively call itself."""
    value = copy.deepcopy(request)
    def filtered(tools, namespace=None):
        result = []
        for tool in tools:
            if tool.get('type') == 'namespace':
                children = filtered(tool['tools'], tool['name'])
                if children:
                    result.append({**tool, 'tools': children})
            elif not _internal_tool(tool.get('name'), namespace):
                result.append(tool)
        return result
    value['tools'] = filtered(value.get('tools') or [])
    for item in value['input']:
        if item.get('type') in {'additional_tools', 'tool_search_output'}:
            item['tools'] = filtered(item['tools'])
    return value


def _adapt_pending_echo(context, request):
    # Codex can omit benign response-item id/status/type annotations for assistant
    # messages and output_text annotations/logprobs. Use the same binding as wire
    # history validation. Keep the original source snapshot exact; accept only
    # wire-verified echoes of newly emitted items, never edits to earlier history.
    if context._request is None or not context._pending_output:
        return
    from .wire import call_binding, message_binding
    start = len(context._request['input'])
    echoed = request['input'][start:start + len(context._pending_output)]
    if len(echoed) != len(context._pending_output):
        return
    for expected, actual in zip(context._pending_output, echoed):
        if expected.get('type') == 'message':
            if message_binding(actual) != message_binding(expected):
                return
            if set(actual) - {'id', 'status', 'type', 'role', 'content'}:
                return
        elif call_binding(expected) != call_binding(actual):
            return
    context._pending_output = copy.deepcopy(echoed)


def catalog_for_pairs(pairs, default_pair=None):
    """Shared reviewed model catalog used by runtime and config transactions."""
    import sys
    parent = str(Path(__file__).resolve().parents[2])
    if parent not in sys.path:
        sys.path.insert(0, parent)
    from direct_bridge.global_config import catalog_for_pairs as build
    return build(pairs, default_pair)


def default_config(bridge_dir):
    """Nonsecret first-use proposal. Caller must obtain local setup consent."""
    config_id = secrets.token_hex(16)
    pair = {'model': 'gpt-6-astra', 'reasoning_effort': 'xhigh'}
    return {'mode': 'global', 'db_path': str(Path(bridge_dir).expanduser().absolute() / 'global.sqlite3'),
            'config_id': config_id, 'grant_id': 'global-' + config_id, 'client_actor': 'local-codex',
            'approval_ref': 'user-global-setup-' + config_id, 'not_before': time.time() - 1,
            'expires_at': None, 'allowed_pairs': [pair], 'default_pair': copy.deepcopy(pair),
            'max_routes': 8, 'max_requests_per_route': 128, 'max_wait_ms': 5000,
            'http_wait_ms': 300000, 'trust_mode': 'single_owner_stdio'}


def _delivery_dump(value):
    if value is None:
        return None
    return {'token': value.token, 'revision': value.revision,
            'continuity': asdict(value.continuity), 'payload': value.payload_bytes.decode('utf-8')}


def _delivery_load(value):
    if value is None:
        return None
    return Delivery(value['token'], value['revision'], NativeContinuity(**value['continuity']),
                    value['payload'].encode('utf-8'))


def _context_dump(context):
    # Closed explicit JSON format. Never pickle or evaluate persisted data.
    fields = ('_request', '_revision', '_pending_output', '_output_recorded',
              '_last_delta', '_delivered_snapshot')
    value = {field: copy.deepcopy(getattr(context, field)) for field in fields}
    value['_delivery'] = _delivery_dump(context._delivery)
    value['_acknowledged'] = _delivery_dump(context._acknowledged)
    value['_schema_delivery'] = [{'continuity': asdict(continuity), 'key': key, 'sha256': sha}
        for (continuity, key), sha in context._schema_delivery.items()]
    return value


def _context_load(route, client_actor):
    from context.incremental import catalog
    context = ContextStore(route['binding'], source_actor=client_actor)
    state = route.get('context')
    if state is None:
        return context
    for field in ('_request', '_revision', '_pending_output', '_output_recorded',
                  '_last_delta', '_delivered_snapshot'):
        setattr(context, field, copy.deepcopy(state[field]))
    context._catalog = catalog(context._request) if context._request is not None else {}
    context._delivery = _delivery_load(state['_delivery'])
    context._acknowledged = _delivery_load(state['_acknowledged'])
    context._schema_delivery = {(NativeContinuity(**item['continuity']), item['key']): item['sha256']
                               for item in state['_schema_delivery']}
    return context


def _identity(headers):
    require(not headers.get_all('session_id') and not headers.get_all('thread_id'),
            'ambiguous_legacy_session_header')
    identity = {}
    for name in ('session-id', 'thread-id'):
        values = headers.get_all(name) or []
        require(len(values) == 1 and re.fullmatch(r'[!-~]{1,256}', values[0]),
                'canonical_runtime_identity_required')
        identity[name] = values[0]
    return identity


class GlobalRuntime:
    mode = 'global'

    def __init__(self, config):
        self.config = copy.deepcopy(config)
        self.config_id = config['config_id']
        self.instance_id = os.environ.get('DOTS_DIRECT_RUN_ID') or secrets.token_hex(16)
        self.max_routes = config.get('max_routes', 8)
        self.max_requests = config.get('max_requests_per_route', 128)
        self.max_wait_ms = config.get('max_wait_ms', 5000)
        self.http_wait_ms = config.get('http_wait_ms', 300000)
        self.bearer = config['http_bearer']
        require(isinstance(self.bearer, str) and len(self.bearer) >= 16 and self.bearer.isascii()
                and all(33 <= ord(c) <= 126 for c in self.bearer), 'local_http_bearer_required')
        require(type(self.max_routes) is int and 1 <= self.max_routes <= 32, 'invalid_route_capacity')
        require(type(self.max_requests) is int and 1 <= self.max_requests <= 1024, 'invalid_request_capacity')
        require(type(self.max_wait_ms) is int and 1 <= self.max_wait_ms <= 5000, 'invalid_wait_limit')
        require(type(self.http_wait_ms) is int and 1 <= self.http_wait_ms <= 300000, 'invalid_http_wait_limit')
        require(config['default_pair'] in config['allowed_pairs'], 'default_pair_not_authorized')
        self.catalog = catalog_for_pairs(config['allowed_pairs'], config['default_pair'])
        self.cv = threading.Condition(threading.RLock())
        self.routes = {}
        self.http = self.http_thread = self.http_address = None
        self.closed = False
        self._owner_fd = self.db = None
        path = Path(config['db_path']).expanduser().absolute()
        require(not any(p.is_symlink() for p in (path, *path.parents)), 'symlink_state_path_rejected')
        require(path.parent.is_dir() and path.parent.stat().st_uid == os.getuid()
                and not path.parent.stat().st_mode & 0o077, 'private_state_directory_required')
        if path.exists():
            require(path.is_file() and path.stat().st_uid == os.getuid()
                    and not path.stat().st_mode & 0o077, 'private_state_file_required')
        self.diagnostics = Diagnostics(path.parent)
        lock = str(path) + '.owner'
        self._owner_fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            require(stat.S_ISREG(os.fstat(self._owner_fd).st_mode) and os.fstat(self._owner_fd).st_uid == os.getuid()
                    and not os.fstat(self._owner_fd).st_mode & 0o077, 'private_owner_file_required')
            try:
                fcntl.flock(self._owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('global_runtime_already_running') from None
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('CREATE TABLE IF NOT EXISTS global_meta (key TEXT PRIMARY KEY, value BLOB NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS global_routes (route_id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, state BLOB NOT NULL)')
            self.db.commit()
            authority = {k: config[k] for k in ('config_id', 'grant_id', 'client_actor', 'approval_ref',
                                               'not_before', 'expires_at', 'allowed_pairs', 'default_pair')}
            raw = canonical(authority)
            old = self.db.execute("SELECT value FROM global_meta WHERE key='authority'").fetchone()
            if old:
                require(old[0] == raw, 'global_authority_mismatch')
            else:
                self.db.execute("INSERT INTO global_meta VALUES ('authority',?)", (raw,))
                self.db.commit()
            for route_id, state in self.db.execute('SELECT route_id,state FROM global_routes'):
                require(len(state) <= MAX_ROUTE_BYTES, 'persisted_route_too_large')
                route = strict_loads(state)
                require(route['route_id'] == route_id and route['contract'] == CONTRACT, 'invalid_persisted_route')
                self.routes[route_id] = route
        except BaseException:
            self.close()
            raise

        self.diagnostics.event('service', 'started', outcome='ok')

    def _active(self):
        require(not self.closed, 'bridge_closed')
        now = time.time()
        expires = self.config['expires_at']
        require(self.config['not_before'] <= now and (expires is None or now < expires), 'authorization_expired')
        old = self.db.execute("SELECT value FROM global_meta WHERE key='last_clock'").fetchone()
        last = float(old[0]) if old else self.config['not_before']
        require(now >= last - 1, 'clock_rollback_new_work_paused')
        self.db.execute("INSERT OR REPLACE INTO global_meta VALUES('last_clock',?)", (str(max(now, last)),))
        self.db.commit()

    def _save(self, route):
        raw = canonical(route)
        require(len(raw) <= MAX_ROUTE_BYTES, 'route_state_capacity_exhausted')
        # State, native action idempotence, and emission fence share one atomic row.
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO global_routes VALUES(?,?,?)',
                            (route['route_id'], canonical(route['identity']).decode(), raw))
        self.routes[route['route_id']] = route
        self.cv.notify_all()

    def _route(self, route_id):
        require(route_id in self.routes, 'unknown_route')
        return self.routes[route_id]

    def _owned(self, args):
        route = self._route(args.get('route_id'))
        claim = route['claim']
        require(claim is not None, 'native_route_not_claimed')
        token = args.get('claim_token')
        require(type(token) is str and token.isascii() and hmac.compare_digest(token, claim['claim_token']),
                'route_claim_required')
        return route

    def _record(self, route, request_id):
        matches = [r for r in route['requests'] if r['id'] == request_id]
        require(len(matches) == 1, 'unknown_request')
        return matches[0]

    def ingest(self, identity, request, request_key=None):
        require(type(identity) is dict and set(identity) == {'session-id', 'thread-id'}
                and all(type(v) is str and re.fullmatch(r'[!-~]{1,256}', v) for v in identity.values()),
                'canonical_runtime_identity_required')
        request = validate_request(request)
        pair = {'model': request['model'], 'reasoning_effort': request['reasoning']['effort']}
        require(pair in self.config['allowed_pairs'], 'pair_not_authorized')
        require(request_key is None or (type(request_key) is str and re.fullmatch(r'[!-~]{1,256}', request_key)),
                'invalid_idempotency_key')
        route_id = 'route-' + digest([self.config_id, identity])[:32]
        request_id = 'request-' + digest([route_id, request_key or digest(request)])
        with self.cv:
            self._active()
            if route_id in self.routes:
                route = copy.deepcopy(self.routes[route_id])
                require(route['pair'] == pair, 'immutable_route_model_effort_mismatch')
                existing = next((r for r in route['requests'] if r['id'] == request_id), None)
                if existing:
                    require(canonical(existing['request']) == canonical(request), 'request_id_conflict')
                    return route_id, request_id
                require(not route['closed'], 'route_closed_start_new_conversation')
            else:
                require(sum(not r['closed'] for r in self.routes.values()) < self.max_routes,
                        'route_capacity_exhausted')
                require(len(self.routes) < 1024, 'route_archive_capacity_exhausted')
                route = {'contract': CONTRACT, 'route_id': route_id, 'identity': copy.deepcopy(identity),
                    'pair': pair, 'binding': {'grant_id': self.config['grant_id'], 'route_id': route_id,
                        'session_id': identity['session-id'], 'thread_id': identity['thread-id'], **pair},
                    'claim': None, 'closed': False, 'requests': [], 'actions': {}, 'context': None,
                    'schema_tokens': {}, 'created_at': time.time(),
                    'metrics': {'full_context_returns': 0, 'delta_context_returns': 0,
                                'response_commits': 0, 'http_emissions': 0, 'context_payload_bytes': []}}
            previous = route['requests'][-1] if route['requests'] else None
            if previous:
                require(not previous['cancelled'], 'cancelled_route_requires_new_conversation')
                require(previous['response'] is not None, 'previous_request_unsettled')
                require(previous['delivery_started'], 'previous_response_not_emitted')
                from .wire import call_binding
                prior_calls = {x['call_id']: digest(call_binding(x)) for r in route['requests'] if r['response']
                               for x in r['response']['output'] if x.get('type') in CLIENT_CALL_TYPES}
                prior_hosted = {x['id']: digest(call_binding(x)) for r in route['requests'] if r['response']
                                for x in r['response']['output'] if x.get('type') == 'web_search_call'}
                validate_history(request, previous['request'], [previous['response']],
                                 prior_call_hashes=prior_calls, prior_hosted_hashes=prior_hosted)
            else:
                validate_history(request, None, [])
            require(len(route['requests']) < self.max_requests, 'route_request_capacity_exhausted')
            context = _context_load(route, self.config['client_actor'])
            seq = len(route['requests']) + 1
            projected = _context_request(request)
            _adapt_pending_echo(context, projected)
            context.ingest_full(actor=self.config['client_actor'], binding=route['binding'], revision=seq, request=projected)
            route['context'] = _context_dump(context)
            route['requests'].append({'id': request_id, 'seq': seq, 'request': request, 'response': None,
                'delivery_started': False, 'cancelled': False, 'context_token': None,
                'context_delivery': None, 'execution_reserved': False})
            self._save(route)
            return route_id, request_id

    def _claim(self, args):
        route = copy.deepcopy(self._route(args.get('route_id')))
        if args.get('claim_token'):
            self._owned(args)
            require(all(key not in args or args[key] == route['claim'][key]
                        for key in ('worker_id', 'context_epoch', 'model', 'reasoning_effort')),
                    'native_route_owner_immutable')
            return route
        self._active()
        require(not route['closed'], 'route_closed')
        for key in ('worker_id', 'context_epoch', 'model', 'reasoning_effort'):
            require(type(args.get(key)) is str and re.fullmatch(r'[!-~]{1,256}', args[key]), 'native_claim_fields_required')
        require({'model': args['model'], 'reasoning_effort': args['reasoning_effort']} == route['pair'],
                'native_claim_pair_mismatch')
        if route['claim']:
            require(all(route['claim'][key] == args[key] for key in ('worker_id', 'context_epoch', 'model', 'reasoning_effort')),
                    'native_route_owner_immutable')
            return route
        require(not any(r['claim'] and r['claim']['worker_id'] == args['worker_id'] for r in self.routes.values()),
                'native_worker_already_bound')
        route['claim'] = {key: args[key] for key in ('worker_id', 'context_epoch', 'model', 'reasoning_effort')}
        route['claim']['claim_token'] = secrets.token_hex(32)
        self._save(route)
        return route

    def _next(self, route_id, seq, wait_ms, cancel_event):
        started = time.monotonic()
        ids = {'route': route_id}
        self.diagnostics.event('runtime_wait', 'begin', ids=ids, wait_ms=wait_ms, after_seq=seq)
        try:
            result = self._next_impl(route_id, seq, wait_ms, cancel_event)
            reason = None
            if result.get('status') == 'pending':
                reason = ('service_closed' if self.closed else 'cooperative_cancel'
                          if cancel_event and cancel_event.is_set() else 'deadline')
            self.diagnostics.event('runtime_wait', 'end', ids={**ids, 'request': result.get('request_id')},
                wait_ms=wait_ms, after_seq=seq, outcome=result.get('status'), reason=reason,
                duration_ms=duration_ms(started))
            return result
        except BaseException:
            self.diagnostics.event('runtime_wait', 'end', ids=ids, outcome='error',
                duration_ms=duration_ms(started))
            raise

    def _next_impl(self, route_id, seq, wait_ms, cancel_event):
        require(type(seq) is int and 0 <= seq <= self.max_requests, 'invalid_after_seq')
        require(type(wait_ms) is int and 0 <= wait_ms <= self.max_wait_ms, 'invalid_wait_ms')
        end = time.monotonic() + wait_ms / 1000
        while True:
            route = self._route(route_id)
            require(seq <= len(route['requests']), 'cursor_ahead_of_route')
            claim = route['claim']
            common = {'route_id': route_id, 'claim_token': claim['claim_token'],
                      'logical_worker_id': claim['worker_id'], 'actual_native_platform_verified': False}
            if route['closed']:
                return {**common, 'status': 'closed'}
            if len(route['requests']) > seq:
                record = route['requests'][seq]
                if record['cancelled']:
                    return {**common, 'status': 'cancelled', 'request_id': record['id'], 'seq': record['seq'],
                            'effect': 'unknown' if record['delivery_started'] or record.get('hosted_operations') else 'not_emitted'}
                if record['response'] is not None:
                    return {**common, 'status': 'already_completed', 'request_id': record['id'],
                            'seq': record['seq'], 'next_after_seq': record['seq'], 'resubmit_action': False}
                self._active()
                if record['context_token']:
                    return {**common, 'status': 'ready', 'request_id': record['id'], 'seq': record['seq'],
                            'context_token': record['context_token'], 'context': record['context_delivery'],
                            'capabilities': self._capabilities(record['request']),
                            'replayed': True, 'execute_again': False}
                route = copy.deepcopy(route)
                record = route['requests'][seq]
                context = _context_load(route, self.config['client_actor'])
                continuity = NativeContinuity(claim['worker_id'], claim['context_epoch'])
                delivery = context.prepare_delivery(continuity)
                payload = delivery.tool_result(max_bytes=MAX_BODY)
                record['context_token'] = delivery.token
                record['context_delivery'] = payload
                record['execution_reserved'] = True
                route['context'] = _context_dump(context)
                route['metrics'][payload['kind'] + '_context_returns'] += 1
                route['metrics']['context_payload_bytes'].append(len(delivery.payload_bytes))
                self._save(route)
                return {**common, 'status': 'ready', 'request_id': record['id'], 'seq': record['seq'],
                        'context_token': delivery.token, 'context': payload, 'replayed': False,
                        'capabilities': self._capabilities(record['request'])}
            if self.closed or (cancel_event and cancel_event.is_set()) or time.monotonic() >= end:
                return {**common, 'status': 'pending', 'after_seq': seq, 'effect': 'unknown',
                        'resubmit_action': False, 'automatic_wake': False}
            self.cv.wait(min(.1, end - time.monotonic()))

    def _ack(self, route, record, args):
        require(args.get('context_token') == record['context_token'] and record['context_token'],
                'context_receipt_required')
        context = _context_load(route, self.config['client_actor'])
        if context._delivery is not None:
            require(context._delivery.token == record['context_token'], 'context_receipt_mismatch')
            context.acknowledge_delivery(context._delivery)
        return context

    @staticmethod
    def _capabilities(request):
        from .hosted import web_capability
        tools = request_tools(request)
        return {'tool_contract': 'dots-direct-tools/2',
                'client_execution': ['function', 'custom', 'namespace', 'tool_search'],
                'hosted': [web_capability(tool) for key, tool in tools.items() if key == ('web_search',)],
                'image_input': 'bounded_data_uri_mcp_image_blocks',
                'native_execution_attested': False}

    def _schemas(self, route, record, context, args):
        continuity = NativeContinuity(route['claim']['worker_id'], route['claim']['context_epoch'])
        for token in args.get('schema_tokens', []):
            require(token in route['schema_tokens'], 'unknown_schema_receipt')
            key, sha, context_token = route['schema_tokens'][token]
            require(context_token == record['context_token'], 'schema_receipt_wrong_context')
            context.acknowledge_schema_delivery(continuity=continuity, key=key, expected_digest=sha)
        return continuity

    def _hosted(self, name, route, record, args):
        from .hosted import prepare_web, verify_result, enforce_tool_policy
        self._active()
        require(not route['closed'] and not record['cancelled'], 'request_cancelled_or_route_closed')
        require(record is route['requests'][-1] and record['response'] is None, 'stale_request')
        context = self._ack(route, record, args)
        operations = record.setdefault('hosted_operations', {})
        operation_id = args.get('operation_id')
        require(type(operation_id) is str and re.fullmatch(r'[!-~]{1,256}', operation_id), 'action_id_required')
        if name == 'prepare_hosted_call':
            continuity = self._schemas(route, record, context, args)
            schema = context.require_schema(continuity=continuity, key=catalog_key('web_search'))
            tool = schema['component']['definition']
            prepared = prepare_web(tool, args['action'], args['item_id'])
            require(not any(item.get('id') == args['item_id'] for item in record['request']['input']),
                    'response_item_id_required')
            existing_items = [op['prepared']['item'] for key, op in operations.items() if key != operation_id]
            enforce_tool_policy(record['request'], existing_items + [prepared['item']])
            binding = {'route_id': route['route_id'], 'claim_token': route['claim']['claim_token'],
                       'request_id': record['id'], 'context_token': record['context_token'],
                       'schema_sha256': schema['schema_sha256'], 'operation_id': operation_id, 'prepared': prepared}
            fingerprint = digest(binding)
            if operation_id in operations:
                op = operations[operation_id]
                require(op['fingerprint'] == fingerprint, 'hosted_operation_conflict')
                return {'status': op['result']['status'] if op.get('result') else 'reserved',
                        'result_status': op['result']['status'] if op.get('result') else None,
                        'operation_token': op['token'], 'execute': False, 'replayed': True,
                        'effect': 'recorded' if op.get('result') else 'unknown',
                        'native_execution_attested': False}
            require(len(operations) < 32, 'hosted_operation_limit')
            require(not any(op.get('result') is None for op in operations.values()), 'hosted_operation_pending')
            require(not any(op['prepared']['item']['id'] == prepared['item']['id']
                            or op['prepared']['native_arguments'] == prepared['native_arguments'] for op in operations.values()),
                    'hosted_operation_conflict')
            op = {'fingerprint': fingerprint, 'token': secrets.token_hex(32), 'prepared': prepared,
                  'schema_sha256': schema['schema_sha256'], 'result': None}
            operations[operation_id] = op
            route['context'] = _context_dump(context)
            self._save(route)  # Durable before returning permission to the trusted native worker.
            return {'status': 'reserved', 'operation_token': op['token'], 'execute': True, 'replayed': False,
                    'native_tool': prepared['native_tool'], 'native_arguments': prepared['native_arguments'],
                    'native_execution_attested': False,
                    'instruction': 'Execute these exact arguments once using your actual native web tool. Preserve its complete result. Permission and web-tool safety rules still apply; a replay never authorizes execution.'}
        require(operation_id in operations, 'hosted_operation_unknown')
        op = operations[operation_id]
        require(args.get('operation_token') == op['token'], 'hosted_receipt_mismatch')
        result = verify_result(op['prepared'], args['result'])
        if op.get('result') is not None:
            require(op['result'] == result, 'hosted_result_conflict')
        else:
            op['result'] = result
            route['context'] = _context_dump(context)
            self._save(route)
        return {'status': result['status'], 'operation_token': op['token'], 'execute': False,
                'item': {**op['prepared']['item'], 'status': result['status']},
                'sources': result['sources'], 'native_execution_attested': False}

    def status(self):
        with self.cv:
            summaries = []
            for route in self.routes.values():
                latest = route['requests'][-1] if route['requests'] else None
                summaries.append({'route_id': route['route_id'], **route['pair'],
                    'state': 'closed' if route['closed'] else 'claimed' if route['claim'] else 'pending_native_child',
                    'worker_id': route['claim']['worker_id'] if route['claim'] else None,
                    'requests': len(route['requests']), 'last_seq': latest['seq'] if latest else 0,
                    'request_pending': bool(latest and not latest['response'] and not latest['cancelled']),
                    'last_local_stop_requested_at': route.get('last_local_stop_requested_at'),
                    'metrics': copy.deepcopy(route['metrics'])})
            current = [x for x in summaries if x['state'] != 'closed']
            return {'status': 'closed' if self.closed else 'ready', 'mode': 'global', 'contract': CONTRACT, 'config_id': self.config_id,
                'instance_id': self.instance_id, 'http_address': self.http_address,
                'max_routes': self.max_routes, 'active_routes': len(current),
                'pending_routes': [x for x in current if x['state'] == 'pending_native_child'],
                'routes': current, 'closed_route_count': len(summaries) - len(current),
                'allowed_pairs': self.config['allowed_pairs'], 'default_pair': self.config['default_pair'],
                'actual_native_platform_verified': False, 'mac_execution_verified': False,
                'model_api_used': False, 'automatic_wake': False,
                'tool_contract': 'dots-direct-tools/2', 'native_hosted_execution_attested': False,
                'native_children_stop_confirmed': False,
                'trust_boundary': 'trusted_single_owner_logical_route_claim',
                'controller_action': 'Create an actual native child for each pending route with its exact pair, then have that child claim using get_request. No automatic child wake is provided.'}

    def public_error(self, error):
        from context.incremental import NeedsWorkingSet
        if isinstance(error, NeedsWorkingSet):
            return 'context_working_set_too_large'
        code = str(error) if isinstance(error, ValueError) else ''
        return code if code in SAFE_GLOBAL_ERRORS else 'runtime_error'

    def call_tool(self, name, args, *, cancel_event=None):
        require(type(args) is dict, 'object_arguments_required')
        from mcp_adapter.tools import GLOBAL_BRIDGE_TOOLS
        definition = next((tool['inputSchema'] for tool in GLOBAL_BRIDGE_TOOLS if tool['name'] == name), None)
        require(definition is not None and not (set(args) - set(definition['properties'])), 'unknown_tool_or_arguments')
        require(set(definition['required']) <= set(args), 'required_tool_arguments_missing')
        if 'wait_ms' in args:
            require(type(args['wait_ms']) is int and 0 <= args['wait_ms'] <= self.max_wait_ms, 'invalid_wait_ms')
        with self.cv:
            require(not self.closed or name == 'bridge_status', 'bridge_closed')
            if name == 'bridge_status':
                require(not args, 'unknown_tool_arguments')
                return self.status()
            if name == 'get_request':
                route = self._claim(args)
                return self._next(route['route_id'], args.get('after_seq', 0), args.get('wait_ms', 1000), cancel_event)
            route = copy.deepcopy(self._owned(args))
            if name == 'cancel_request' and args.get('close_route'):
                require(not args.get('request_id'), 'close_route_or_request_required')
                require(not route['requests'] or route['requests'][-1]['response'] is not None
                        or route['requests'][-1]['cancelled'], 'cancel_pending_request_before_close')
                require(not route['requests'] or route['requests'][-1]['response'] is None
                        or not has_tools(route['requests'][-1]['response']), 'tool_outcome_unknown_route_not_settled')
                route['closed'] = True
                self._save(route)
                return {'status': 'closed', 'route_id': route['route_id'], 'worker_reassignment_allowed': False}
            record = self._record(route, args.get('request_id'))
            if name in ('prepare_hosted_call', 'record_hosted_result'):
                return self._hosted(name, route, record, args)
            if name in ('discover_tools', 'lookup_schema'):
                self._active()
                require(record is route['requests'][-1] and record['response'] is None and not record['cancelled'], 'stale_request')
                context = self._ack(route, record, args)
                if name == 'discover_tools':
                    result = context.discover(args['query'], limit=args.get('limit', 8))
                else:
                    result = context.exact_schema(args['name'], args['sha256'])
                    token = digest([route['route_id'], record['id'], record['context_token'], args['name'], args['sha256']])
                    route['schema_tokens'][token] = [args['name'], args['sha256'], record['context_token']]
                    result['schema_token'] = token
                route['context'] = _context_dump(context)
                self._save(route)
                return result
            if name in ('finish_request', 'submit_action_and_wait_result'):
                final = name == 'finish_request'
                action_id = args.get('action_id')
                require(type(action_id) is str and re.fullmatch(r'[!-~]{1,256}', action_id), 'action_id_required')
                fingerprint = digest({key: args.get(key) for key in ('request_id', 'context_token', 'schema_tokens', 'response')})
                if action_id in route['actions']:
                    action = route['actions'][action_id]
                    require(action['fingerprint'] == fingerprint and action['final'] == final, 'action_id_conflict')
                else:
                    self._active()
                    require(not route['closed'] and not record['cancelled'], 'request_cancelled_or_route_closed')
                    require(record is route['requests'][-1] and record['response'] is None, 'request_already_answered_or_stale')
                    context = self._ack(route, record, args)
                    continuity = self._schemas(route, record, context, args)
                    receipts = list(record.get('hosted_operations', {}).values())
                    require(not any(op.get('result') is None for op in receipts), 'hosted_operation_pending')
                    prior_sources = {source['url'] for previous in route['requests'] if previous is not record
                                     for op in previous.get('hosted_operations', {}).values()
                                     if op.get('result', {}).get('status') == 'completed'
                                     for source in op['result']['sources']}
                    response = validate_response(args['response'], record['request'], hosted_receipts=receipts,
                                                 prior_citation_sources=prior_sources)
                    require(not any(_internal_tool(item.get('name'), item.get('namespace')) for item in response['output']
                                    if item.get('type') in {'function_call', 'custom_tool_call'}), 'bridge_transport_tool_intent_forbidden')
                    require(has_tools(response) != final, 'use_finish_for_message_or_submit_for_tools')
                    for item in response['output']:
                        if item.get('type') == 'tool_search_call':
                            context.require_schema(continuity=continuity, key=catalog_key('tool_search'))
                        if item.get('type') in {'function_call', 'custom_tool_call'}:
                            context.require_schema(continuity=continuity, key=tool_key(item.get('namespace'), item['name']))
                    context.record_output(continuity=continuity, items=response['output'])
                    record['response'] = response
                    route['actions'][action_id] = {'request_id': record['id'], 'fingerprint': fingerprint, 'final': final}
                    route['context'] = _context_dump(context)
                    route['metrics']['response_commits'] += 1
                    self._save(route)
                    self.diagnostics.event('response_commit', 'end', method=name,
                        ids={'route': route['route_id'], 'request': record['id'], 'action': action_id},
                        outcome='message' if final else 'tools')
                if final:
                    return {'status': 'completed', 'route_id': route['route_id'], 'request_id': record['id'],
                            'action_id': action_id, 'http_delivery_confirmed': False}
                return self._next(route['route_id'], record['seq'], args.get('wait_ms', 1000), cancel_event)
            if name == 'await_result':
                action = route['actions'].get(args.get('action_id'))
                require(action is not None and action['request_id'] == record['id'] and not action['final'], 'action_binding_mismatch')
                return self._next(route['route_id'], record['seq'], args.get('wait_ms', 1000), cancel_event)
            if name == 'cancel_request':
                if record['response'] is not None:
                    return {'status': 'too_late_result_committed', 'effect': 'unknown', 'resubmit_action': False}
                record['cancelled'] = True
                self._save(route)
                return {'status': 'cancelled', 'request_id': record['id'],
                        'effect': 'unknown' if record.get('hosted_operations') else 'not_emitted',
                        'hosted_result_recorded': any(op.get('result') for op in record.get('hosted_operations', {}).values()),
                        'execute_again': False,
                        'route_requires_new_conversation': True}
            raise ValueError('unknown_tool')

    def start_http(self, host='127.0.0.1', port=0):
        require(host == '127.0.0.1' and self.http is None, 'loopback_listener_required')
        runtime = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *unused):
                pass
            def setup(self):
                super().setup()
                self.connection.settimeout(10)
            def _headers(self):
                require(self.headers.get_all('Host') == [f'127.0.0.1:{runtime.http.server_port}'], 'loopback_host_required')
                require(not any(self.headers.get_all(name) for name in ('Origin', 'Cookie', 'Proxy-Authorization', 'Sec-Fetch-Site', 'Transfer-Encoding', 'Upgrade')), 'forbidden_http_header')
                values = self.headers.get_all('Authorization') or []
                require(len(values) == 1 and values[0].isascii() and hmac.compare_digest(values[0], 'Bearer ' + runtime.bearer), 'authorization_required')
                require(not self.headers.get_all('Content-Encoding') or self.headers.get_all('Content-Encoding') == ['identity'], 'unsupported_encoding')
            def _diag(self, stage, **fields):
                if hasattr(self, 'diagnostic_ids'):
                    runtime.diagnostics.event('http', stage, method='responses', ids=self.diagnostic_ids,
                        duration_ms=duration_ms(self.diagnostic_started), **fields)
            def _json(self, status, value):
                raw = canonical(value)
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(raw)
                self._diag('socket_written', http_status=status, outcome='ok')
                self.close_connection = True
            def _error(self, error, request_id=None):
                # Exception codes here are fixed validators, never payload text.
                code = runtime.public_error(error)
                self._diag('error', outcome='error', error_code=code)
                details = getattr(error, 'details', {})
                safe_details = {}
                for key in ('tool_index', 'child_index'):
                    if type(details.get(key)) is int and 0 <= details[key] <= 100000:
                        safe_details[key] = details[key]
                from .wire import TOOL_DIAGNOSTIC_TYPES
                if details.get('tool_type') in TOOL_DIAGNOSTIC_TYPES:
                    safe_details['tool_type'] = details['tool_type']
                if type(details.get('name_present')) is bool:
                    safe_details['name_present'] = details['name_present']
                if details.get('item_type') in {'reasoning', 'input_audio', 'image_generation_call', 'computer_call', 'local_shell_call', 'agent_message', 'unknown'}:
                    safe_details['item_type'] = details['item_type']
                self._json(401 if code == 'authorization_required' else 409,
                    {'error': {'code': code, **({'details': safe_details} if safe_details else {})}, 'request_id': request_id,
                     'automatic_retry': False, 'resubmit_new_action': False, 'native_fallback': False})
            def do_GET(self):
                try:
                    self._headers()
                    require(not self.headers.get_all('Content-Length') or self.headers.get_all('Content-Length') == ['0'], 'get_body_not_supported')
                    parsed = urlsplit(self.path)
                    if parsed.path in ('/health', '/v1/health') and not parsed.query:
                        return self._json(200, {**runtime.status(), 'listener_ready': True})
                    if parsed.path in ('/v1/models', '/models'):
                        require(set(parse_qs(parsed.query)) <= {'client_version'}, 'unsupported_catalog_query')
                        return self._json(200, runtime.catalog)
                    if parsed.path.startswith('/v1/responses/') and not parsed.query:
                        identity = _identity(self.headers)
                        route_id = 'route-' + digest([runtime.config_id, identity])[:32]
                        with runtime.cv:
                            route = runtime._route(route_id)
                            record = runtime._record(route, parsed.path.rsplit('/', 1)[1])
                            # Inspection never re-emits execution-bearing response contents.
                            return self._json(200, {'route_id': route_id, 'request_id': record['id'],
                                'status': 'cancelled' if record['cancelled'] else 'completed' if record['response'] else 'pending',
                                'delivery_started': record['delivery_started'], 'automatic_retry': False,
                                'effect': 'unknown' if record['delivery_started'] or record.get('hosted_operations') else 'not_emitted'})
                    return self._json(404, {'error': {'code': 'not_found'}})
                except (ValueError, KeyError, TypeError) as exc:
                    self._error(exc)
                except OSError:
                    pass
            def do_POST(self):
                request_id = None
                acquired = False
                self.diagnostic_started = time.monotonic()
                self.diagnostic_ids = {'call': secrets.token_hex(12)}
                self._diag('begin')
                try:
                    self._headers()
                    require(self.path in ('/v1/responses', '/responses'), 'unsupported_endpoint')
                    lengths = self.headers.get_all('Content-Length') or []
                    require(len(lengths) == 1 and re.fullmatch(r'[0-9]{1,7}', lengths[0]) and 0 < int(lengths[0]) <= MAX_BODY,
                            'bounded_content_length_required')
                    require(self.headers.get_content_type() == 'application/json', 'json_content_type_required')
                    identity = _identity(self.headers)
                    keys = (self.headers.get_all('Idempotency-Key') or []) + (self.headers.get_all('X-Dots-Request-Id') or [])
                    require(len(keys) <= 1, 'ambiguous_idempotency_key')
                    acquired = runtime.http.waiters.acquire(blocking=False)
                    require(acquired, 'http_waiter_capacity_exhausted')
                    raw = self.rfile.read(int(lengths[0]))
                    require(len(raw) == int(lengths[0]), 'incomplete_body')
                    route_id, request_id = runtime.ingest(identity, strict_loads(raw), keys[0] if keys else None)
                    self.diagnostic_ids.update(route=route_id, request=request_id)
                    self._diag('ingested', wait_ms=runtime.http_wait_ms)
                    end = time.monotonic() + runtime.http_wait_ms / 1000
                    with runtime.cv:
                        while True:
                            route = runtime._route(route_id)
                            record = runtime._record(route, request_id)
                            require(not runtime.closed, 'bridge_closed')
                            require(not record['cancelled'], 'request_cancelled')
                            if record['response'] is not None:
                                require(not record['delivery_started'] or not has_tools(record['response']), 'delivery_outcome_unknown_no_reemission')
                                route = copy.deepcopy(route)
                                record = runtime._record(route, request_id)
                                first_emission = not record['delivery_started']
                                record['delivery_started'] = True
                                if first_emission:
                                    route['metrics']['http_emissions'] += 1
                                # Durable BEFORE socket emission. Broken sockets remain unknown.
                                runtime._save(route)
                                self._diag('delivery_fenced', outcome='tools' if has_tools(record['response']) else 'message')
                                payload = response_events(record['response'])
                                break
                            require(not runtime.closed, 'bridge_closed')
                            remaining = end - time.monotonic()
                            if remaining <= 0:
                                self._diag('timeout', http_status=504, outcome='pending', error_code='outcome_pending', reason='deadline')
                                return self._json(504, {'error': {'code': 'outcome_pending'}, 'request_id': request_id,
                                    'route_id': route_id, 'automatic_retry': False, 'resubmit_new_action': False})
                            runtime.cv.wait(min(.1, remaining))
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(payload)))
                    self.send_header('Cache-Control', 'no-cache')
                    self.send_header('X-Request-ID', request_id)
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(payload)
                    self.wfile.flush()
                    self._diag('socket_flushed', http_status=200, outcome='ok')
                    self.close_connection = True
                except (ValueError, KeyError, TypeError) as exc:
                    try:
                        self._error(exc, request_id)
                    except OSError:
                        self._diag('disconnect', outcome='unknown', reason='socket_error')
                except OSError:
                    self._diag('disconnect', outcome='unknown', reason='socket_error')
                finally:
                    self._diag('end')
                    if acquired:
                        runtime.http.waiters.release()
                    self.close_connection = True
        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True  # SO_REUSEADDR only: restart after TIME_WAIT; never SO_REUSEPORT.
            def handle_error(self, request, client_address):
                pass  # Never log source payload, credentials, or tracebacks.
        self.http = Server((host, port), Handler)
        self.http.waiters = threading.BoundedSemaphore(self.max_routes * 2)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        self.http_address = f'http://127.0.0.1:{self.http.server_port}/v1'
        return self.http_address

    def close(self):
        with self.cv:
            already_closed = self.closed
            self.closed = True  # Admission closes even if supplementary stop recording fails.
            if not already_closed:
                self.diagnostics.event('service', 'stopping', reason='shutdown')
            if not already_closed and self.db is not None:
                stopped_at = time.time()
                try:
                    for current in list(self.routes.values()):
                        if current['claim'] and not current['closed']:
                            route = copy.deepcopy(current)
                            route['last_local_stop_requested_at'] = stopped_at
                            self._save(route)
                except (sqlite3.Error, OSError, ValueError):
                    pass  # Never keep serving because a shutdown marker could not be written.
            self.cv.notify_all()
        if self.http:
            self.http.shutdown()
            self.http.server_close()
        if self.http_thread:
            self.http_thread.join(timeout=2)
        with self.cv:
            if self.db is not None:
                self.db.close()
                self.db = None
            if self._owner_fd is not None:
                fcntl.flock(self._owner_fd, fcntl.LOCK_UN)
                os.close(self._owner_fd)
                self._owner_fd = None

        if not already_closed:
            self.diagnostics.event('service', 'stopped', reason='shutdown')
            self.diagnostics.close()
