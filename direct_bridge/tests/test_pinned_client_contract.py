"""Actual pinned CLI outbound contracts, with synthetic response provenance.

The fixtures are sanitized excerpts, never a claim of native model execution.
The fixture extractor retains every tool declaration and callback item exactly.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT.parent)]
from facade.wire import request_tools, validate_request, validate_history
from facade.images import content_images
from context.incremental import catalog, catalog_key, ContextStore, NativeContinuity

FIXTURES = Path(__file__).with_name('fixtures') / 'pinned_cli_0159_2'

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()

def fixture(case, number=1):
    return json.loads((FIXTURES / f'{case}-{number}.json').read_text())

def request(case, number=1):
    return fixture(case, number)['request']

class PinnedClientContracts(unittest.TestCase):
    def test_manifest_and_exact_extracted_components_are_hash_bound(self):
        manifest = json.loads((FIXTURES/'manifest.json').read_text())
        self.assertEqual(manifest['fixture_count'], len(manifest['files']))
        for name, expected in manifest['files'].items():
            with self.subTest(fixture=name):
                raw = (FIXTURES/name).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), expected)
                value = json.loads(raw); evidence = value['evidence']; req = value['request']
                self.assertEqual(evidence['actual_client'], 'codex-cli 0.159.2')
                self.assertTrue(evidence['synthetic_server_or_controller_responses'])
                self.assertEqual(evidence['external_model_api_calls'], 0)
                for key, component in [('exact_tools_sha256', req['tools']),
                    ('exact_callback_items_sha256', req['input'][1:]),
                    ('sanitized_request_sha256', req)]:
                    self.assertEqual(hashlib.sha256(canonical(component)).hexdigest(), evidence[key])
                self.assertNotIn('Authorization', raw.decode())
                self.assertNotIn('CAPTURE_LOCAL_BEARER', raw.decode())

    def test_supported_real_requests_and_explicit_reasoning_limit(self):
        manifest = json.loads((FIXTURES/'manifest.json').read_text())
        for name in manifest['files']:
            value = json.loads((FIXTURES/name).read_text()); req = value['request']
            with self.subTest(fixture=name):
                if value['expected_request_error']:
                    with self.assertRaisesRegex(ValueError, value['expected_request_error']): validate_request(req)
                else:
                    self.assertEqual(validate_request(req), req)
                self.assertTrue(request_tools(req))
                self.assertTrue(catalog(req))

    def test_default_cached_live_indexed_and_disabled_declarations(self):
        web = lambda case: request_tools(request(case)).get(('web_search',))
        self.assertEqual(web('default'), {'type':'web_search', 'external_web_access':False})
        self.assertEqual(web('default'), web('web-cached'))
        self.assertEqual(web('web-live'), {'type':'web_search', 'external_web_access':True})
        self.assertEqual(web('web-indexed'), {'type':'web_search', 'external_web_access':True, 'indexed_web_access':True})
        self.assertIsNone(web('web-disabled'))

    def test_configured_web_options_and_text_image_capability(self):
        declared = request_tools(request('web-configured'))[('web_search',)]
        self.assertEqual(declared['filters'], {'allowed_domains':['example.com']})
        self.assertEqual(declared['user_location'], {'type':'approximate','country':'US','city':'Fixture City'})
        self.assertEqual(declared['search_context_size'], 'high')
        self.assertEqual(request_tools(request('web-text-image'))[('web_search',)]['search_content_types'], ['text','image'])

    def test_custom_function_namespace_and_code_mode_declarations(self):
        tools = request_tools(request('web-disabled'))
        self.assertEqual(tools[(None, 'apply_patch')]['type'], 'custom')
        self.assertEqual(tools[(None, 'exec_command')]['type'], 'function')
        self.assertIn(('multi_agent_v1', 'spawn_agent'), tools)
        code_tools = request_tools(request('code-mode-enabled'))
        self.assertEqual(code_tools[(None, 'exec')]['type'], 'custom')
        self.assertEqual(code_tools[(None, 'wait')]['type'], 'function')

    def test_function_and_custom_callback_correlation(self):
        for case, kind in [('function-roundtrip','function'), ('custom-roundtrip','custom_tool')]:
            items = request(case, 2)['input'][1:]
            self.assertEqual(items[0]['type'], kind+'_call')
            self.assertEqual(items[1]['type'], kind+'_call_output')
            self.assertEqual(items[0]['call_id'], items[1]['call_id'])
            self.assertIsInstance(items[1]['output'], str)
        self.assertIn('read-only sandbox', request('custom-roundtrip',2)['input'][-1]['output'])

    def test_client_discovery_reveals_exact_namespace_without_top_level_rewrite(self):
        first, second, third = (request('tool-search-full', n) for n in (1,2,3))
        self.assertEqual(first['tools'], second['tools']); self.assertEqual(second['tools'], third['tools'])
        key = ('mcp__fixture','fixture_echo')
        self.assertNotIn(key, request_tools(first)); self.assertIn(key, request_tools(second))
        output = second['input'][-1]
        self.assertEqual(output['type'], 'tool_search_output'); self.assertEqual(output['execution'], 'client')
        self.assertTrue(output['tools'][0]['tools'][0]['defer_loading'])
        self.assertEqual(request_tools(second)[key], output['tools'][0]['tools'][0])
        self.assertEqual(request_tools(third)[key], request_tools(second)[key])
        self.assertIsInstance(second['input'][-2]['arguments'], dict)
        self.assertEqual(third['input'][-2]['namespace'], key[0])
        self.assertEqual(third['input'][-2]['call_id'], third['input'][-1]['call_id'])

    def test_catalog_keeps_nameless_protocol_and_namespaced_tool_keys_distinct(self):
        req=request('tool-search-full', 3); definitions=catalog(req)
        self.assertIn(catalog_key('tool_search'), definitions)
        self.assertIn(catalog_key('function','mcp__fixture','fixture_echo'), definitions)
        self.assertEqual(definitions[catalog_key('tool_search')]['component']['definition'],
                         request_tools(req)[('tool_search',)])
        self.assertIn(catalog_key('web_search'), catalog(request('default')))

    def test_actual_discovery_history_works_with_incremental_catalog(self):
        binding={'grant_id':'fixture','route_id':'fixture','session_id':'fixture','thread_id':'fixture',
                 'model':'gpt-6-astra','reasoning_effort':'xhigh'}
        store=ContextStore(binding,source_actor='fixture-client')
        continuity=NativeContinuity('fixture-worker','fixture-epoch')
        for n in (1,2,3):
            store.ingest_full(actor='fixture-client',binding=binding,revision=n,request=request('tool-search-full',n))
            delivery=store.prepare_delivery(continuity)
            if n==2:
                changed=delivery.tool_result()['tool_catalog']['changed']
                self.assertIn(catalog_key('function','mcp__fixture','fixture_echo'), changed)
            store.acknowledge_delivery(delivery)
        self.assertEqual(store.snapshot(),request('tool-search-full',3))

    def test_typed_web_echo_preserves_actions_and_drops_known_unmodeled_fields(self):
        items=request('web-roundtrip',2)['input'][1:]
        self.assertEqual(items[0]['action'], {'type':'search','query':'fixture query'})
        self.assertEqual(items[1]['content'], [{'type':'output_text','text':'Fixture answer [example].'}])
        actions=[i['action'] for i in request('web-action-variants',2)['input'] if i.get('type')=='web_search_call']
        self.assertEqual([a['type'] for a in actions],['search','open_page','find_in_page'])
        self.assertEqual(actions[0]['queries'],['fixture query','second fixture'])

    def test_structured_mcp_text_and_actual_image_are_preserved(self):
        output=request('mcp-roundtrip',2)['input'][-1]['output']
        self.assertTrue(all(p['type']=='input_text' for p in output))
        image_request=request('mcp-image-capable',2)
        images=content_images(image_request['input'])
        self.assertEqual(len(images),1); self.assertEqual(images[0]['mimeType'],'image/png')
        self.assertEqual(images[0]['_meta'], {'codex/imageDetail':'high'})
        self.assertEqual(validate_request(image_request),image_request)

if __name__=='__main__': unittest.main(verbosity=2)
