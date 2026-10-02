"""Offline lossless request-view and exact schema-cache regressions."""
import copy
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dots_lite import request_view as rv
from dots_lite.protocol import ProtocolError, canonical, sha256
from dots_lite.storage import private_write


def function(name='act', description='Execute the exact bounded fixture action.'):
    return {'type': 'function', 'name': name, 'description': description,
            'strict': True, 'parameters': {'type': 'object', 'properties': {
                'value': {'$ref': '#/$defs/Value'}}, 'required': ['value'],
                'additionalProperties': False, '$defs': {'Value': {'type': 'string'}}},
            'x-original-field': {'do_not_drop': [None, False, 0, '😀']}}


def request():
    return {'model': 'fixture-model', 'reasoning': {'effort': 'xhigh', 'summary': 'auto'},
            'stream': True, 'instructions': 'All original instructions. 中文 😀\nSecond line.',
            'input': [{'role': 'system', 'content': 'Keep this system text.'},
                      {'role': 'developer', 'content': [{'type': 'input_text', 'text': 'Keep this too.'}]},
                      {'role': 'user', 'content': 'A complete user request.'},
                      {'type': 'function_call', 'id': 'historical-item', 'call_id': 'historical-call',
                       'namespace': 'ns', 'name': 'act', 'arguments': '{"value":"old"}'},
                      {'type': 'function_call_output', 'call_id': 'historical-call',
                       'output': [{'type': 'input_text', 'text': 'exact original output'}]}],
            'tools': [{'type': 'namespace', 'name': 'ns', 'description': 'Full namespace instruction ' * 40,
                       'x-extra': {'nested': [1, False, None]}, 'tools': [function()]}],
            'tool_choice': 'auto', 'parallel_tool_calls': False,
            'metadata': {'keep': 'all remaining fields'}, 'x-future': [1, 2, 3]}


def binding(raw, **updates):
    return {'actor_task_id': '/root/fixture-child', 'request_id': 'request-fixture',
            'request_sha256': sha256(raw), 'package_sha256': 'a' * 64,
            'route_id': 'route-fixture', **updates}


def response(namespace='ns', name='act', kind='function_call'):
    return {'id': 'response-exact', 'status': 'completed', 'output': [{
        'id': 'fc_new_exact_item' if kind == 'function_call' else 'ctc_new_exact_item', 'type': kind, 'call_id': 'new-exact-call',
        'namespace': namespace, 'name': name,
        **({'arguments': '{"value":"new"}'} if kind == 'function_call' else {'input': 'exact custom input'})}]}


class RequestViews(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / 'cache'
        self.store = rv.RequestViewStore(self.directory)

    def prepare(self, req=None, raw=None, **updates):
        raw = canonical(request() if req is None else req) if raw is None else raw
        return self.store.prepare(raw, binding=binding(raw, **updates))

    def test_original_exact_bytes_and_all_non_definition_fields_round_trip(self):
        original = request()
        # Noncanonical source spelling/whitespace is preserved as the authority.
        raw = (' \n' + json.dumps(original, ensure_ascii=True, indent=3) + '\n').encode()
        view = self.prepare(raw=raw)
        self.assertEqual(view.original_bytes(), raw)
        self.assertEqual(view.original_request(), original)
        self.assertEqual(view.request_sha256, sha256(raw))
        projected = view.model_view()['request']
        for key in original.keys() - {'tools'}:
            self.assertEqual(projected[key], original[key])
        selected = view.exact_schema('ns', 'act')
        restored = copy.deepcopy(projected)
        restored['tools'][0]['tools'][0] = selected['definition']
        self.assertEqual(restored, original)
        self.assertEqual(view.model_view_bytes(), canonical(view.model_view()))
        index = projected['tools'][0]['tools'][0]
        self.assertEqual(index['schema_sha256'], selected['schema_sha256'])
        self.assertNotIn('reference_sha256', index)
        self.assertEqual(view.model_view()['binding_sha256'], selected['reference']['binding_sha256'])

    def test_all_namespace_metadata_in_initial_and_selected_view(self):
        req = request()
        meta = {key: value for key, value in req['tools'][0].items() if key != 'tools'}
        view = self.prepare(req)
        projected = view.model_view()['request']['tools'][0]
        self.assertEqual({key: value for key, value in projected.items() if key != 'tools'}, meta)
        self.assertEqual(view.exact_schema('ns', 'act')['namespace_metadata'], meta)
        self.assertEqual(view.exact_schema('ns', 'act')['definition'], req['tools'][0]['tools'][0])

    def test_source_mutation_and_returned_containers_cannot_change_view(self):
        req = request()
        view = self.prepare(req)
        before = view.model_view_bytes()
        req['instructions'] = 'changed caller object'
        view.original_request()['instructions'] = 'changed returned object'
        view.model_view()['request']['instructions'] = 'changed returned view'
        selected = view.exact_schema('ns', 'act')
        selected['definition']['parameters']['required'].clear()
        view.schema_receipt('ns', 'act')['schema_sha256'] = 'b' * 64
        self.assertEqual(view.model_view_bytes(), before)
        self.assertEqual(view.exact_schema('ns', 'act')['definition']['parameters']['required'], ['value'])

    def test_complete_discovery_custom_function_optional_and_additional_tools(self):
        req = request()
        req['tools'] += [function('top'), {'type': 'namespace', 'name': 'empty',
                         'description': 'Empty namespace remains explicit.', 'tools': []}]
        req['input'].insert(2, {'type': 'additional_tools', 'id': 'additional-exact',
                               'x-extra': 'preserved', 'tools': [
                                   {'type': 'custom', 'name': 'custom_top', 'description': 'Exact raw grammar.',
                                    'format': {'type': 'grammar', 'syntax': 'lark', 'definition': 'start: "hi"'}},
                                   {'type': 'namespace', 'name': 'other', 'x-meta': {'v': 2},
                                    'tools': [function('nested_additional')]}]})
        view = self.prepare(req)
        projected = view.model_view()['request']
        self.assertEqual(projected['tools'][1]['name'], 'top')
        self.assertEqual(projected['tools'][2], req['tools'][2])
        additional = projected['input'][2]
        self.assertEqual({k: v for k, v in additional.items() if k != 'tools'},
                         {k: v for k, v in req['input'][2].items() if k != 'tools'})
        for ns, name in [('ns', 'act'), (None, 'top'), (None, 'custom_top'), ('other', 'nested_additional')]:
            exact = view.exact_schema(ns, name)
            self.assertEqual(exact['definition']['name'], name)
            self.assertEqual(exact['reference'], view.schema_receipt(ns, name))
        custom = view.exact_schema(None, 'custom_top')
        self.assertIsNone(custom['namespace_metadata'])
        self.assertEqual(custom['definition']['format']['definition'], 'start: "hi"')
        view.validate_exposed(response(None, 'custom_top', 'custom_tool_call'), [custom['reference']])
        for omitted in ['missing', None, []]:
            small = request()
            if omitted == 'missing':
                small.pop('tools')
            else:
                small['tools'] = omitted
            rebuilt = self.prepare(small).model_view()['request']
            self.assertEqual(rebuilt, small)

    def test_repeated_namespace_name_preserves_each_distinct_metadata(self):
        req = request()
        req['tools'].append({'type': 'namespace', 'name': 'ns', 'description': 'Other precise metadata',
                             'x-other': 7, 'tools': [function('second')]})
        view = self.prepare(req)
        self.assertNotEqual(view.exact_schema('ns', 'act')['namespace_metadata'],
                            view.exact_schema('ns', 'second')['namespace_metadata'])
        self.assertEqual(view.exact_schema('ns', 'second')['namespace_metadata']['x-other'], 7)

    def test_duplicate_definitions_rejected_across_declaration_locations(self):
        req = request()
        req['input'].append({'type': 'additional_tools', 'tools': copy.deepcopy(req['tools'])})
        with self.assertRaisesRegex(ProtocolError, 'duplicate_tool'):
            self.prepare(req)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_discovery_description_is_bounded_verbatim_not_constraint_summary(self):
        req = request()
        description = '中文😀 original mandatory rule\n' * 80
        req['tools'][0]['tools'][0]['description'] = description
        view = self.prepare(req)
        discovery = view.model_view()['request']['tools'][0]['tools'][0]['description_discovery']
        self.assertEqual(discovery, {'text': description[:rv.DISCOVERY_DESCRIPTION_CHARS], 'truncated': True})
        self.assertEqual(view.model_view()['tool_definition_policy']['description_discovery'],
                         {'kind': 'verbatim_prefix_not_full_constraints', 'max_chars': rv.DISCOVERY_DESCRIPTION_CHARS})
        self.assertEqual(view.exact_schema('ns', 'act')['definition']['description'], description)

    def test_schema_hash_is_computed_from_exact_full_component(self):
        view = self.prepare()
        exact = view.exact_schema('ns', 'act')
        component = {'definition': exact['definition'], 'namespace': 'ns',
                     'namespace_metadata': exact['namespace_metadata']}
        self.assertEqual(exact['schema_sha256'], sha256(canonical(component)))
        self.assertEqual((self.directory / (exact['schema_sha256'] + '.json')).read_bytes(), canonical(component))
        forged = dict(exact['reference'], schema_sha256='e' * 64)
        with self.assertRaisesRegex(ProtocolError, 'reference_mismatch'):
            view.exact_schema('ns', 'act', reference=forged)
        self.assertFalse((self.directory / ('e' * 64 + '.json')).exists())

    def test_changed_renamed_removed_and_added_definitions_have_current_membership(self):
        old = self.prepare()
        old_ref = old.exact_schema('ns', 'act')['reference']
        for field in ['description', 'schema', 'namespace_metadata', 'rename', 'remove']:
            req = request()
            if field == 'description': req['tools'][0]['tools'][0]['description'] += ' new constraint'
            elif field == 'schema': req['tools'][0]['tools'][0]['parameters']['properties']['value'] = {'type': 'integer'}
            elif field == 'namespace_metadata': req['tools'][0]['x-new'] = 'new namespace rule'
            elif field == 'rename': req['tools'][0]['tools'][0]['name'] = 'renamed'
            elif field == 'remove': req['tools'][0]['tools'] = []
            new = self.prepare(req, request_id='next-' + field)
            with self.subTest(field=field), self.assertRaises(ProtocolError):
                new.exact_schema('ns', 'act', reference=old_ref)
        added = request()
        added['tools'][0]['tools'].append(function('new_tool'))
        self.assertEqual(self.prepare(added).exact_schema('ns', 'new_tool')['definition']['name'], 'new_tool')
        with self.assertRaisesRegex(ProtocolError, 'unadvertised_tool'):
            old.exact_schema('ns', 'new_tool')

    def test_same_schema_cross_request_route_actor_and_package_references_fail(self):
        old = self.prepare()
        reference = old.exact_schema('ns', 'act')['reference']
        for change in [{'request_id': 'new-request'}, {'route_id': 'new-route'},
                       {'actor_task_id': '/root/other-child'}, {'package_sha256': 'b' * 64}]:
            new = self.prepare(**change)
            # Identical content can be cached; public references never grant cross-binding use.
            self.assertEqual(new.exact_schema('ns', 'act')['schema_sha256'], reference['schema_sha256'])
            with self.subTest(change=change), self.assertRaisesRegex(ProtocolError, 'reference_mismatch'):
                new.exact_schema('ns', 'act', reference=reference)
            with self.assertRaisesRegex(ProtocolError, 'reference_mismatch'):
                new.validate_exposed(response(), [reference])
        raw = b' ' + canonical(request())
        self.assertNotEqual(self.prepare(raw=raw).request_sha256, old.request_sha256)
        with self.assertRaisesRegex(ProtocolError, 'reference_mismatch'):
            self.prepare(raw=raw).exact_schema('ns', 'act', reference=reference)

    def test_exact_schema_required_before_tool_call_but_text_needs_no_schema(self):
        view = self.prepare()
        with self.assertRaisesRegex(ProtocolError, 'schema_not_exposed'):
            view.validate_exposed(response(), [])
        selected = view.exact_schema('ns', 'act')
        view.validate_exposed(response(), [selected['reference']])
        text = {'id': 'r', 'status': 'completed', 'output': [{'id': 'm', 'type': 'message',
                'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Exact answer'}]}]}
        view.validate_exposed(text, [])
        bad = response()
        bad['output'][0]['arguments'] = '{"value":2}'
        with self.assertRaisesRegex(ProtocolError, 'tool_arguments_schema_mismatch'):
            view.validate_exposed(bad, [selected['reference']])
        with self.assertRaisesRegex(ProtocolError, 'unadvertised_tool'):
            view.validate_exposed(response(None, 'act'), [selected['reference']])

    def test_cache_corruption_fails_without_repair(self):
        view = self.prepare()
        exact = view.exact_schema('ns', 'act')
        path = self.directory / (exact['schema_sha256'] + '.json')
        corrupt = b'{"tampered":true}'
        private_write(path, corrupt)
        with self.assertRaisesRegex(ProtocolError, 'cache_corrupt'):
            view.exact_schema('ns', 'act')
        self.assertEqual(path.read_bytes(), corrupt)

    def test_cache_symlink_and_unrecognized_entries_fail_closed(self):
        view = self.prepare()
        (self.directory / ('a' * 64 + '.json')).symlink_to(Path(self.tmp.name) / 'outside')
        with self.assertRaisesRegex(ProtocolError, 'cache_corrupt'):
            view.exact_schema('ns', 'act')
        (self.directory / ('a' * 64 + '.json')).unlink()
        private_write(self.directory / 'untrusted.json', b'{}')
        with self.assertRaisesRegex(ProtocolError, 'cache_corrupt'):
            view.exact_schema('ns', 'act')

    def test_deterministic_cache_reuse_and_bounded_eviction(self):
        store = rv.RequestViewStore(self.directory, max_entries=2)
        req = request()
        req['tools'][0]['tools'] += [function('b'), function('c')]
        raw = canonical(req)
        view = store.prepare(raw, binding=binding(raw))
        refs = [view.exact_schema('ns', name)['reference'] for name in ['act', 'b']]
        before = set(path.stem for path in self.directory.glob('*.json'))
        self.assertEqual(len(before), 2)
        view.exact_schema('ns', 'act')
        self.assertEqual(set(path.stem for path in self.directory.glob('*.json')), before)
        c = view.exact_schema('ns', 'c')['schema_sha256']
        self.assertEqual(set(path.stem for path in self.directory.glob('*.json')), {max(before), c})
        # Evicted content is recreated only from the present authoritative request.
        for ref in refs:
            view.exact_schema(ref['namespace'], ref['name'], reference=ref)
            self.assertLessEqual(len(list(self.directory.glob('*.json'))), 2)

    def test_cache_byte_budget_and_preexisting_oversize_fail_closed(self):
        req = request()
        req['tools'][0]['tools'].append(function('other'))
        initial = self.prepare(req)
        exact = initial.exact_schema('ns', 'act')
        component_size = len((self.directory / (exact['schema_sha256'] + '.json')).read_bytes())
        other_size = len(canonical({'definition': req['tools'][0]['tools'][1], 'namespace': 'ns',
                                   'namespace_metadata': exact['namespace_metadata']}))
        # Capacity permits either member individually, but not both together.
        store = rv.RequestViewStore(self.directory, max_bytes=max(component_size, other_size))
        raw = canonical(req)
        view = store.prepare(raw, binding=binding(raw))
        selected = view.exact_schema('ns', 'other')
        self.assertEqual([path.stem for path in self.directory.glob('*.json')], [selected['schema_sha256']])
        extra = b'{}'
        private_write(self.directory / (sha256(extra) + '.json'), extra)
        with self.assertRaisesRegex(ProtocolError, 'cache_bounds_exceeded'):
            view.exact_schema('ns', 'act')
        self.assertEqual(len(list(self.directory.glob('*.json'))), 2)

    def test_same_named_tools_in_different_namespaces_remain_distinct(self):
        req = request()
        req['tools'] += [function('act'), {'type': 'namespace', 'name': 'second',
                        'tools': [function('act', 'Different namespace constraints.')]}]
        view = self.prepare(req)
        references = [view.exact_schema(ns, 'act')['reference'] for ns in [None, 'ns', 'second']]
        self.assertEqual(len({sha256(canonical(ref)) for ref in references}), 3)
        for ref in references:
            view.validate_exposed(response(ref['namespace']), [ref])
        with self.assertRaisesRegex(ProtocolError, 'schema_not_exposed'):
            view.validate_exposed(response('second'), [references[1]])

    def test_size_limits_and_bad_input_have_no_schema_write(self):
        raw = canonical(request())
        with self.assertRaisesRegex(ProtocolError, 'binding_mismatch'):
            self.store.prepare(raw, binding=binding(raw, request_sha256='f' * 64))
        with self.assertRaisesRegex(ProtocolError, 'invalid_request_view_binding'):
            self.store.prepare(raw, binding={**binding(raw), 'caller_schema_hash': 'a' * 64})
        with self.assertRaises(ProtocolError):
            self.store.prepare(b'{"model":1,"model":2}', binding=binding(b'{"model":1,"model":2}'))
        self.assertEqual(list(self.directory.iterdir()), [])
        small = rv.RequestViewStore(self.directory, max_request_bytes=len(raw) - 1)
        with self.assertRaisesRegex(ProtocolError, 'request_too_large'):
            small.prepare(raw, binding=binding(raw))
        small = rv.RequestViewStore(self.directory, max_bytes=20)
        view = small.prepare(raw, binding=binding(raw))
        with self.assertRaisesRegex(ProtocolError, 'component_too_large'):
            view.exact_schema('ns', 'act')
        self.assertEqual(list(self.directory.iterdir()), [])
        for options in [{'max_entries': 0}, {'max_bytes': rv.MAX_CACHE_BYTES + 1},
                        {'max_request_bytes': rv.MAX_BYTES + 1}]:
            with self.subTest(options=options), self.assertRaises(ProtocolError):
                rv.RequestViewStore(self.directory, **options)

    def test_numeric_constraints_never_silently_round_in_projection(self):
        base = canonical(request())
        for number in [b'1.2345678901234567890123456789', b'1e-400', b'-0', b'1e-9999999999999999999999999']:
            raw = base[:-1] + b',"x-number":' + number + b'}'
            with self.subTest(number=number), self.assertRaisesRegex(ProtocolError, 'numeric_precision_loss'):
                self.prepare(raw=raw)
        for number in [b'1.0', b'1e2', b'0.1', b'-0.0', b'-0e0', b'123456789012345678901234567890']:
            raw = base[:-1] + b',"x-number":' + number + b'}'
            self.assertEqual(self.prepare(raw=raw).original_bytes(), raw)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_schema_numeric_enum_minimum_exponents_large_ints_and_negative_zero(self):
        for keyword, template in [('minimum', 0), ('enum', [0])]:
            req = request()
            req['tools'][0]['tools'][0]['parameters']['properties']['value'] = {
                'type': 'number', keyword: template}
            raw = canonical(req)
            for numeric in [b'1.2345678901234567890123456789', b'1.00000000000000001e20', b'1e-400']:
                old = ('"' + keyword + '":' + ('0' if keyword == 'minimum' else '[0]')).encode()
                new = ('"' + keyword + '":').encode() + (numeric if keyword == 'minimum' else b'[' + numeric + b']')
                exact_raw = raw.replace(old, new)
                with self.subTest(keyword=keyword, number=numeric), self.assertRaisesRegex(ProtocolError, 'numeric_precision_loss'):
                    rv.preflight(exact_raw, binding=binding(exact_raw))
        req = request()
        req['tools'][0]['tools'][0]['parameters']['properties']['value'] = {
            'type': 'number', 'minimum': -0.0, 'enum': [10**80, -0.0, 1e20, 0.1]}
        raw = canonical(req)
        rv.preflight(raw, binding=binding(raw))
        exact = self.prepare(raw=raw).exact_schema('ns', 'act')['definition']['parameters']['properties']['value']
        self.assertEqual(exact['enum'][0], 10**80)
        self.assertEqual(math.copysign(1, exact['minimum']), -1)
        self.assertEqual(math.copysign(1, exact['enum'][1]), -1)

    def test_complete_projection_preflight_has_no_files_or_exposure_side_effects(self):
        raw = canonical(request())
        before = list(self.directory.iterdir())
        with patch.object(rv, 'private_dir', side_effect=AssertionError('No directory creation')), \
             patch.object(rv, 'private_write', side_effect=AssertionError('No artifact publication')), \
             patch.object(rv.RequestViewStore, '_component', side_effect=AssertionError('No schema acquisition')):
            self.assertIsNone(rv.preflight(raw, binding=binding(raw)))
            with patch.object(rv, 'MAX_VIEW_BYTES', 1), self.assertRaisesRegex(ProtocolError, 'request_view_too_large'):
                rv.preflight(raw, binding=binding(raw))
        self.assertEqual(list(self.directory.iterdir()), before)

    def test_namespace_component_amplification_is_bounded_during_private_preflight(self):
        raw = canonical(request())
        with patch.object(rv, 'MAX_COMPONENT_WORK_BYTES', 1), \
             self.assertRaisesRegex(ProtocolError, 'component_work_too_large'):
            rv.preflight(raw, binding=binding(raw))
        self.assertEqual(list(self.directory.iterdir()), [])
        req = request()
        req['tools'][0]['tools'] = [function('a'), function('b')]
        view = self.prepare(req)
        # Metadata is shared by immutable source references until selection.
        self.assertIs(view._sources[('ns', 'a')][1], view._sources[('ns', 'b')][1])
        self.assertEqual(view.exact_schema('ns', 'a')['namespace_metadata'],
                         view.exact_schema('ns', 'b')['namespace_metadata'])

    def test_hosted_search_and_other_unsupported_declarations_remain_rejected(self):
        for kind in ['web_search', 'tool_search', 'unrecognized']:
            req = request()
            req['tools'].append({'type': kind, 'name': 'example'})
            with self.subTest(kind=kind), self.assertRaises(ProtocolError):
                self.prepare(req)

    def test_chunking_exact_coverage_unicode_and_limits(self):
        data = ('😀ABC中文' * 5).encode('utf-8')
        offset, chunks = 0, []
        while offset < len(data):
            part = rv.chunk_bytes(data, offset, 5)
            self.assertEqual(part['offset'], offset)
            self.assertEqual(part['sha256'], sha256(data))
            self.assertLessEqual(len(part['text'].encode()), 5)
            self.assertEqual(part['total_bytes'], len(data))
            offset = part['next_offset']
            self.assertEqual(part['complete'], offset == len(data))
            chunks.append(part['text'].encode())
        self.assertEqual(b''.join(chunks), data)
        self.assertEqual(rv.chunk_bytes(data, len(data), 4)['text'], '')
        for offset, limit in [(1, 4), (-1, 4), (len(data) + 1, 4), (0, 3), (0, rv.MAX_CHUNK_BYTES + 1)]:
            with self.subTest(offset=offset, limit=limit), self.assertRaises(ProtocolError):
                rv.chunk_bytes(data, offset, limit)
        with self.assertRaises(ProtocolError):
            rv.chunk_bytes(b'\xff')

    def test_large_unused_tool_definitions_are_not_repeated_in_model_view(self):
        req = request()
        req['tools'][0]['tools'] = [function('tool_' + str(i), 'Original constraint ' * 60) for i in range(100)]
        raw = canonical(req)
        view = self.prepare(req)
        self.assertLess(len(view.model_view_bytes()), len(raw) * .5)
        self.assertEqual(view.model_view()['request']['input'], req['input'])
        self.assertEqual(len(view.model_view()['request']['tools'][0]['tools']), 100)
        self.assertEqual(list(self.directory.iterdir()), [])  # Discovery does not acquire schemas.
        self.assertEqual(view.exact_schema('ns', 'tool_99')['definition'], req['tools'][0]['tools'][99])

    def test_prepare_and_lookup_have_no_network_or_external_tool_execution(self):
        with patch('socket.socket', side_effect=AssertionError('No external effects')), \
             patch('subprocess.run', side_effect=AssertionError('No process execution')):
            view = self.prepare()
            view.exact_schema('ns', 'act')
            view.validate_exposed(response(), [view.schema_receipt('ns', 'act')])


if __name__ == '__main__':
    unittest.main()
