"""Protocol/receipt tests. Synthetic native result evidence is labelled as such."""
import asyncio
import base64
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from PIL import Image

from facade.test_global_runtime import config, request, response, http
from facade.runtime import create_runtime
from facade.wire import validate_request, validate_response, response_events, request_tools
from facade.hosted import prepare_web, web_capability
from facade.images import image_payload
from mcp_adapter.server import MCPServer

SEARCH = {'type': 'tool_search', 'execution': 'client', 'description': 'Find fixture tools',
          'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}, 'limit': {'type': 'number'}},
                         'required': ['query'], 'additionalProperties': False}}
WEB = {'type': 'web_search', 'external_web_access': True}


def search_call():
    return {'id': 'tsc_1', 'type': 'tool_search_call', 'call_id': 'call-search',
            'execution': 'client', 'arguments': {'query': 'fixture'}, 'status': 'completed'}


class ToolCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = config(self.tmp.name)
        self.rt = create_runtime(self.cfg)
        self.identity = {'session-id': 'fixture', 'thread-id': 'fixture'}

    def tearDown(self):
        self.rt.close(); self.tmp.cleanup()

    def acquire(self, req):
        route, _ = self.rt.ingest(self.identity, req)
        return self.rt.call_tool('get_request', {'route_id': route, 'worker_id': 'native-fixture',
            'context_epoch': 'epoch', 'model': req['model'], 'reasoning_effort': req['reasoning']['effort'], 'wait_ms': 0})

    def scope(self, item):
        return {key: item[key] for key in ('route_id', 'claim_token', 'request_id', 'context_token')}

    def schema(self, item, query):
        found = self.rt.call_tool('discover_tools', {**self.scope(item), 'query': query})['matches'][0]
        return self.rt.call_tool('lookup_schema', {**self.scope(item), 'name': found['key'], 'sha256': found['schema_sha256']})['schema_token']

    def prepare(self, first, op='web-1', item='ws_1', action=None):
        return self.rt.call_tool('prepare_hosted_call', {**self.scope(first), 'operation_id': op, 'item_id': item,
            'action': action or {'type': 'search', 'query': 'fixture search'},
            'schema_tokens': [self.schema(first, 'web_search')]})

    def result(self, first, prepared, op='web-1', sources=True, status='completed'):
        src = [{'url': 'https://example.com/source', 'title': 'Fixture', 'reference': 'fixture-ref'}] if sources else []
        result = {'native_tool': prepared['native_tool'], 'native_arguments': prepared['native_arguments'],
                  'native_result': {'synthetic_fixture': True, 'sources': src, 'text': 'Fixture result'},
                  'sources': src, 'status': status}
        return self.rt.call_tool('record_hosted_result', {**self.scope(first), 'operation_id': op,
            'operation_token': prepared['operation_token'], 'result': result})

    def finish(self, first, output, action='finish'):
        res = response(); res['output'] = output
        return self.rt.call_tool('finish_request', {**self.scope(first), 'action_id': action,
            'response': res, 'schema_tokens': []})

    def test_default_cached_declaration_admitted_without_execution(self):
        req = request(); req['tools'].append({'type': 'web_search', 'external_web_access': False})
        first = self.acquire(req)
        self.assertEqual(first['capabilities']['hosted'][0]['capability_errors'], ['web_search_cached_unavailable'])
        with self.assertRaisesRegex(ValueError, 'web_search_cached_unavailable'):
            self.prepare(first)
        self.assertEqual(self.finish(first, response()['output'])['status'], 'completed')
        self.assertFalse(self.rt.routes[first['route_id']]['requests'][0].get('hosted_operations'))

    def test_tool_search_discovers_and_invokes_namespaced_schema(self):
        req = request(); req['tools'].append(SEARCH)
        first = self.acquire(req); token = self.schema(first, 'tool_search')
        res = response(); res['output'] = [search_call()]
        args = {**self.scope(first), 'action_id': 'search', 'response': res, 'schema_tokens': [token], 'wait_ms': 0}
        self.assertEqual(self.rt.call_tool('submit_action_and_wait_result', args)['status'], 'pending')
        self.rt.start_http(); port = self.rt.http.server_port
        self.assertEqual(http(port, self.cfg['http_bearer'], self.identity, req)[0], 200)
        self.assertEqual(http(port, self.cfg['http_bearer'], self.identity, req)[1]['error']['code'], 'delivery_outcome_unknown_no_reemission')
        namespace = {'type': 'namespace', 'name': 'mcp__fixture', 'description': 'Fixture namespace', 'tools': [
            {'type': 'function', 'name': 'echo', 'defer_loading': True, 'parameters': {'type': 'object'}}]}
        req['input'] += [search_call(), {'type': 'tool_search_output', 'id': 'tso_1', 'call_id': 'call-search',
            'status': 'completed', 'execution': 'client', 'tools': [namespace]}]
        self.rt.ingest(self.identity, req)
        next_ = self.rt.call_tool('await_result', {**{k: first[k] for k in ('route_id', 'claim_token', 'request_id')},
            'action_id': 'search', 'wait_ms': 0})
        exact = self.schema(next_, 'echo')
        self.assertTrue(exact)
        call = {'id': 'fc_2', 'type': 'function_call', 'call_id': 'invoke', 'namespace': 'mcp__fixture',
                'name': 'echo', 'arguments': '{}'}
        res['output'] = [call]
        self.assertEqual(self.rt.call_tool('submit_action_and_wait_result', {**self.scope(next_), 'action_id': 'invoke',
            'response': res, 'schema_tokens': [exact], 'wait_ms': 0})['status'], 'pending')

    def test_hosted_reservation_result_final_history_restart(self):
        req = request(); req['tools'].append(WEB)
        first = self.acquire(req); prepared = self.prepare(first)
        self.assertTrue(prepared['execute'])
        replay = self.prepare(first)
        self.assertFalse(replay['execute']); self.assertEqual(replay['effect'], 'unknown')
        self.rt.close(); self.rt = create_runtime(self.cfg)
        self.assertFalse(self.prepare(first)['execute'])
        done = self.result(first, prepared)
        msg = response()['output'][0]
        msg['content'][0] = {'type': 'output_text', 'text': 'Fixture result', 'annotations': [
            {'type': 'url_citation', 'url': 'https://example.com/source', 'title': 'Fixture', 'start_index': 0, 'end_index': 7}]}
        self.assertEqual(self.finish(first, [done['item'], msg])['status'], 'completed')
        self.rt.start_http(); self.assertEqual(http(self.rt.http.server_port, self.cfg['http_bearer'], self.identity, req)[0], 200)
        msg['content'][0].pop('annotations')  # Actual pinned typed echo omission.
        req['input'] += [done['item'], msg, {'role': 'user', 'content': 'Continue'}]
        self.assertTrue(self.rt.ingest(self.identity, req))
        events = response_events({**response(), 'output': [done['item'], msg]})
        self.assertIn(b'response.web_search_call.completed', events)

    def test_unreceipted_or_omitted_web_and_forged_citations_rejected(self):
        req = request(); req['tools'].append(WEB)
        first = self.acquire(req); prepared = self.prepare(first)
        with self.assertRaisesRegex(ValueError, 'hosted_operation_pending'):
            self.finish(first, response()['output'])
        done = self.result(first, prepared)
        with self.assertRaisesRegex(ValueError, 'hosted_receipt_mismatch'):
            self.finish(first, response()['output'])
        forged = copy.deepcopy(done['item']); forged['action']['query'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'hosted_receipt_mismatch'):
            self.finish(first, [forged, *response()['output']])
        msg = response()['output'][0]
        msg['content'][0]['annotations'] = [{'type': 'url_citation', 'url': 'https://unverified.example/',
            'title': 'No', 'start_index': 0, 'end_index': 1}]
        with self.assertRaisesRegex(ValueError, 'citation_source_not_verified'):
            self.finish(first, [done['item'], msg])

    def test_no_results_and_failed_results_are_truthful(self):
        req = request(); req['tools'].append(WEB)
        first = self.acquire(req); prepared = self.prepare(first)
        done = self.result(first, prepared, sources=False)
        self.assertEqual(done['sources'], [])
        self.assertEqual(self.finish(first, [done['item'], *response()['output']])['status'], 'completed')

    def test_cancellation_reports_native_uncertainty_and_prior_citations_reuse(self):
        req = request(); req['tools'].append(WEB)
        first = self.acquire(req); prepared = self.prepare(first)
        cancel = self.rt.call_tool('cancel_request', {key: first[key] for key in ('route_id', 'claim_token', 'request_id')})
        self.assertEqual(cancel['effect'], 'unknown')
        self.assertFalse(cancel['execute_again'])

    def test_completed_sources_are_available_only_in_same_route(self):
        req = request(); req['tools'].append(WEB)
        first = self.acquire(req); prepared = self.prepare(first); done = self.result(first, prepared)
        self.finish(first, [done['item'], *response()['output']])
        self.rt.start_http()
        http(self.rt.http.server_port, self.cfg['http_bearer'], self.identity, req)
        req['input'] += [done['item'], *response()['output'], {'role': 'user', 'content': 'Cite it again'}]
        self.rt.ingest(self.identity, req)
        second = self.rt.call_tool('get_request', {'route_id': first['route_id'], 'claim_token': first['claim_token'], 'after_seq': 1, 'wait_ms': 0})
        msg = response(2)['output'][0]
        msg['content'][0]['annotations'] = [{'type': 'url_citation', 'url': 'https://example.com/source', 'title': 'Fixture', 'start_index': 0, 'end_index': 6}]
        self.assertEqual(self.finish(second, [msg], action='finish-2')['status'], 'completed')
        other_request = request(); other_request['tools'].append(WEB)
        other_route, _ = self.rt.ingest({'session-id': 'other', 'thread-id': 'other'}, other_request)
        other = self.rt.call_tool('get_request', {'route_id': other_route, 'worker_id': 'other-worker', 'context_epoch': 'other-epoch', 'model': 'gpt-6-astra', 'reasoning_effort': 'xhigh', 'wait_ms': 0})
        with self.assertRaisesRegex(ValueError, 'citation_source_not_verified'):
            self.finish(other, [msg])

    def test_historical_item_ids_cannot_be_reused(self):
        req = request(); req['input'][0]['id'] = 'ws_old'; req['tools'].append(WEB)
        first = self.acquire(req)
        with self.assertRaisesRegex(ValueError, 'response_item_id_required'):
            self.prepare(first, item='ws_old')
        res = response(); res['output'][0]['id'] = 'ws_old'
        with self.assertRaisesRegex(ValueError, 'response_item_id_required'):
            validate_response(res, req)

    def test_failed_result_has_no_completed_web_item(self):
        req = request(); req['tools'].append(WEB)
        first = self.acquire(req); prepared = self.prepare(first)
        done = self.result(first, prepared, sources=False, status='failed')
        self.assertEqual(done['item']['status'], 'failed')
        self.assertEqual(self.finish(first, [done['item'], *response()['output']])['status'], 'completed')
        self.assertNotIn(b'response.web_search_call.completed', response_events({**response(), 'output': [done['item'], *response()['output']]}))

    def test_web_constraint_and_policy_fail_before_execution(self):
        for field, value, error in [('external_web_access', False, 'cached'), ('indexed_web_access', True, 'indexed'),
                                   ('user_location', {'type': 'approximate'}, 'location'),
                                   ('search_context_size', 'high', 'context_size'), ('search_content_types', ['text'], 'content_types')]:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, error):
                prepare_web({**WEB, field: value}, {'type': 'search', 'query': 'x'}, 'ws_a')
        tool = {**WEB, 'filters': {'allowed_domains': ['example.com']}}
        mapped = prepare_web(tool, {'type': 'search', 'queries': ['a', 'b', 'c', 'd']}, 'ws_a')
        self.assertEqual(mapped['native_arguments']['response_length'], 'medium')
        self.assertEqual(mapped['native_arguments']['search_query'][0]['domains'], ['example.com'])
        for url in ('https://badexample.com/', 'https://example.com@evil.com/'):
            with self.assertRaisesRegex(ValueError, 'domain_forbidden|invalid_web_search_url'):
                prepare_web(tool, {'type': 'open_page', 'url': url}, 'ws_a')
        req = request(); req['tool_choice'] = {'type': 'function', 'name': 'fixture_probe'}
        with self.assertRaisesRegex(ValueError, 'tool_choice_violation'): validate_response(response(), req)
        req['tool_choice'] = 'none'; req['tools'].append(WEB)
        first = self.acquire(req)
        with self.assertRaisesRegex(ValueError, 'tool_choice_violation'): self.prepare(first)

    def test_untrusted_sources_and_changed_result_rejected(self):
        req = request(); req['tools'].append(WEB)
        first = self.acquire(req); prepared = self.prepare(first)
        bad = {'native_tool': 'web.run', 'native_arguments': prepared['native_arguments'],
               'native_result': 'no matching URL', 'status': 'completed',
               'sources': [{'url': 'https://example.com', 'title': 'x', 'reference': 'fake-ref'}]}
        with self.assertRaisesRegex(ValueError, 'native_web_source_not_in_result'):
            self.rt.call_tool('record_hosted_result', {**self.scope(first), 'operation_id': 'web-1',
                'operation_token': prepared['operation_token'], 'result': bad})
        self.result(first, prepared)
        with self.assertRaisesRegex(ValueError, 'hosted_result_conflict'):
            self.result(first, prepared, sources=False)

    def test_named_collisions_do_not_hide_hosted_or_search(self):
        req = request(); req['tools'] += [WEB, SEARCH, {'type': 'function', 'name': 'web_search', 'parameters': {'type': 'object'}}]
        tools = request_tools(req)
        self.assertIn(('web_search',), tools); self.assertIn((None, 'web_search'), tools)

    def test_native_image_block_and_bounds(self):
        buffer = io.BytesIO(); Image.new('RGB', (8, 8), '#00ff00').save(buffer, format='PNG')
        part = {'type': 'input_image', 'image_url': 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode(), 'detail': 'original'}
        req = request(); req['input'][0]['content'] = [{'type': 'input_text', 'text': 'Inspect image'}, part]
        first = self.acquire(req)
        server = MCPServer(self.rt)
        try:
            result = asyncio.run(server._call_tool('get_request', {'route_id': first['route_id'],
                'claim_token': first['claim_token'], 'wait_ms': 0}))
            self.assertEqual(result.content[1].type, 'image')
            self.assertEqual(result.content[1].data, part['image_url'].split(',')[1])
            self.assertEqual(result.content[1].model_dump(by_alias=True)['_meta'], {'codex/imageDetail': 'original'})
        finally:
            server._pool.shutdown()
        for url, error in [('https://example.com/image.png', 'unsupported_image_source'),
                           ('data:image/png;base64,@@@@', 'invalid_image_content'),
                           ('data:image/png;base64,' + 'A' * 800000, 'image_content_too_large')]:
            with self.assertRaisesRegex(ValueError, error): image_payload({'type': 'input_image', 'image_url': url})

    def test_auxiliary_title_json_format_enforced(self):
        req = request(); req['tools'] = []; req['text'] = {'format': {'type': 'json_schema',
            'name': 'codex_output_schema', 'schema': {'type': 'object', 'properties': {'title': {'type': 'string'}},
                'required': ['title'], 'additionalProperties': False}, 'strict': True}}
        valid = response(); valid['output'][0]['content'][0]['text'] = '{"title":"Title"}'
        self.assertEqual(validate_response(valid, req), valid)
        valid['output'][0]['content'][0]['text'] = '{}'
        with self.assertRaisesRegex(ValueError, 'schema_mismatch'): validate_response(valid, req)

if __name__ == '__main__': unittest.main()
