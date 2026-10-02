"""Offline local-reference compatibility and closed-resolution regressions."""
import copy
import json
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dots_lite import wire
from dots_lite.protocol import ProtocolError

DRAFT2020 = 'https://json-schema.org/draft/2020-12/schema'
DRAFT2019 = 'https://json-schema.org/draft/2019-09/schema'


def request(schema):
    return {'model': 'fixture-model', 'reasoning': {'effort': 'xhigh'}, 'stream': True,
            'input': [{'role': 'user', 'content': 'Offline fixture only'}],
            'tools': [{'type': 'namespace', 'name': 'fixture', 'tools': [
                {'type': 'function', 'name': 'local_ref', 'parameters': schema}]}]}


def response(value):
    return {'id': 'resp_exact_fixture', 'status': 'completed', 'output': [{
        'id': 'fc_exact_fixture', 'type': 'function_call', 'call_id': 'call_exact_fixture',
        'namespace': 'fixture', 'name': 'local_ref', 'arguments': json.dumps(value)}]}


def reference_schema(ref='#/$defs/Value'):
    return {'type': 'object', 'properties': {'value': {'$ref': ref}}, 'required': ['value'],
            'additionalProperties': False, '$defs': {'Value': {'type': 'string', 'minLength': 2}}}


class LocalSchemaReferences(unittest.TestCase):
    def assertAccepts(self, schema, value):
        original = copy.deepcopy(schema)
        req, result = request(schema), response(value)
        self.assertEqual(wire.validate_request(req), req)
        self.assertEqual(wire.validate_response(result, req), result)
        self.assertEqual(schema, original)
        return req, result

    def assertMismatch(self, schema, value):
        with self.assertRaisesRegex(ProtocolError, 'tool_arguments_schema_mismatch'):
            wire.validate_response(response(value), request(schema))

    def test_local_definition_preserves_wire_schema_ids_and_history(self):
        schema = reference_schema()
        req, result = self.assertAccepts(schema, {'value': 'ok'})
        continued = copy.deepcopy(req)
        continued['input'] += result['output'] + [{'type': 'function_call_output',
            'call_id': 'call_exact_fixture', 'output': 'fixture'}]
        self.assertEqual(wire.validate_request(continued), continued)
        wire.validate_history(continued, req, [result])
        self.assertIn('"$ref"', json.dumps(req))
        self.assertMismatch(schema, {'value': 3})
        self.assertMismatch(schema, {'value': 'x'})
        self.assertMismatch(schema, {'value': 'ok', 'extra': True})

    def test_pinned_codex_source_derived_fixtures(self):
        fixtures = json.loads((Path(__file__).parent / 'fixtures' /
                               'codex_0_159_2_schema_refs.json').read_text())
        values = [
            ({'channel_id': 'C_FIXTURE', 'message': 'fixture', 'post_at': 1, 'thread_ts': '1.0'},
             {'channel_id': 'C_FIXTURE', 'message': 'fixture', 'post_at': 1, 'thread_ts': 3}),
            ({'event': {'title': 'fixture', 'start': {'dateTime': 'fixture', 'timeZone': 'UTC'}}},
             {'event': {'start': {'timeZone': 'Not/AZone'}}}),
            ({'user': {'name': 'fixture'}}, {'user': {'name': 3}}),
            ({'node': {'next': {'next': {}}}}, {'node': {'next': 'wrong'}}),
            ({'user': 'fixture', 'profile': 'fixture'}, {'user': 3, 'profile': 'fixture'}),
        ]
        self.assertEqual(fixtures['source_commit'], 'ff6aec96948b70d94983af2641a6b67c94faeff5')
        self.assertEqual(len(fixtures['cases']), len(values))
        for case, (good, bad) in zip(fixtures['cases'], values):
            with self.subTest(case=case['name']):
                self.assertAccepts(case['parameters'], good)
                self.assertMismatch(case['parameters'], bad)

    def test_escaped_and_percent_encoded_json_pointers(self):
        for key, fragment in [('slash/key', 'slash~1key'), ('tilde~key', 'tilde~0key'),
                              ('space key', 'space%20key'), ('%2F', '%252F')]:
            schema = reference_schema('#/%24defs/' + fragment)
            schema['$defs'] = {key: {'type': 'string', 'minLength': 2}}
            with self.subTest(key=key):
                self.assertAccepts(schema, {'value': 'ok'})
                self.assertMismatch(schema, {'value': 2})
        schema = reference_schema('#%2F$defs%2FValue')
        self.assertAccepts(schema, {'value': 'ok'})

    def test_encoded_fragments_with_unevaluated_annotation_helpers(self):
        for ref in ['#%2F$defs%2FV', '#%61']:
            schema = {'type': 'object', '$ref': ref, '$defs': {'V': {
                '$anchor': 'a', 'properties': {'n': {'type': 'integer'}}}},
                'unevaluatedProperties': False}
            with self.subTest(ref=ref):
                self.assertAccepts(schema, {'n': 2})
                self.assertMismatch(schema, {'n': 'wrong'})
                self.assertMismatch(schema, {'n': 2, 'extra': 3})
        schema = {'type': 'object', '$defs': {'V': {'prefixItems': [{'type': 'integer'}]}},
                  'properties': {'arr': {'$ref': '#%2F$defs%2FV', 'unevaluatedItems': False}}}
        self.assertAccepts(schema, {'arr': [2]})
        self.assertMismatch(schema, {'arr': [2, 3]})

    def test_local_anchors_and_embedded_id_resources(self):
        schema = reference_schema('#%76alue')
        schema['$defs']['Value']['$anchor'] = 'value'
        self.assertAccepts(schema, {'value': 'ok'})
        self.assertMismatch(schema, {'value': 4})
        for root_id in ['https://example.invalid/root.json', 'folder/root.json', 'file:///never/read/root.json']:
            schema = {'type': 'object', '$id': root_id, '$defs': {'Child': {
                '$id': 'sub/child.json', 'type': 'object', '$defs': {'Text': {'type': 'string'}},
                'properties': {'text': {'$ref': '#/$defs/Text'}}}},
                'properties': {'value': {'$ref': 'sub/child.json'}}}
            with self.subTest(root_id=root_id):
                self.assertAccepts(schema, {'value': {'text': 'ok'}})
                self.assertMismatch(schema, {'value': {'text': 3}})

    def test_local_refs_keep_sibling_constraints(self):
        schema = reference_schema()
        schema['properties']['value']['maxLength'] = 3
        self.assertAccepts(schema, {'value': 'ok'})
        self.assertMismatch(schema, {'value': 'longer'})

    def test_ref_spelling_in_property_names_and_instance_data_is_inert(self):
        example = {'$ref': 'https://never.invalid/schema', '$dynamicRef': 'file:///never',
                   '$recursiveRef': 'missing.json'}
        schema = {'type': 'object', 'properties': {key: {'type': 'string'} for key in example},
                  'examples': [example], 'default': example, 'const': example,
                  'enum': [example], 'x-annotation': example}
        self.assertAccepts(schema, example)
        self.assertMismatch(schema, {'$ref': 1})

    def test_bad_or_external_refs_fail_before_value_validation_without_io(self):
        refs = ['https://never.invalid/schema', 'file:///etc/passwd', '//never.invalid/schema',
                '../missing.json', '#/$defs/Missing', '#missing', '#/$defs/Value/type',
                '#/$defs/Bad~2Name', 'https://json-schema.org/draft/2020-12/schema']
        with patch.object(socket, 'socket', side_effect=AssertionError('No network allowed')), \
             patch('urllib.request.urlopen', side_effect=AssertionError('No URL retrieval allowed')):
            for ref in refs:
                with self.subTest(ref=ref), self.assertRaises(ProtocolError):
                    wire.validate_request(request(reference_schema(ref)))

    def test_array_pointer_indices_are_strict(self):
        schema = reference_schema('#/prefixItems/0')
        schema['prefixItems'] = [{'type': 'string'}]
        self.assertAccepts(schema, {'value': 'ok'})
        for index in ['-1', '00', '+0', '-', '1']:
            schema['properties']['value']['$ref'] = '#/prefixItems/' + index
            with self.subTest(index=index), self.assertRaises(ProtocolError):
                wire.validate_request(request(schema))

    def test_nested_id_fragment_does_not_escape_its_resource(self):
        schema = reference_schema()
        schema['properties']['value'] = {'$id': 'https://never.invalid/child', '$ref': '#/$defs/Value'}
        with self.assertRaisesRegex(ProtocolError, 'reference_unresolvable'):
            wire.validate_request(request(schema))

    def test_unused_branch_external_reference_is_rejected(self):
        schema = reference_schema()
        schema['$defs']['Unused'] = {'$ref': 'https://never.invalid/schema'}
        with self.assertRaisesRegex(ProtocolError, 'reference_unresolvable'):
            wire.validate_request(request(schema))

    def test_explicit_reference_target_is_a_schema_even_in_an_annotation(self):
        schema = reference_schema('#/x-schema')
        schema['x-schema'] = {'type': 'integer', 'minimum': 3}
        self.assertAccepts(schema, {'value': 4})
        self.assertMismatch(schema, {'value': 2})
        schema['x-schema'] = {'$ref': 'https://never.invalid/schema'}
        with self.assertRaisesRegex(ProtocolError, 'reference_unresolvable'):
            wire.validate_request(request(schema))

    def test_recursive_schemas_validate_values_and_bound_nonproductive_cycles(self):
        schema = {'type': 'object', 'properties': {'next': {'$ref': '#'}}}
        self.assertAccepts(schema, {'next': {'next': {}}})
        self.assertMismatch(schema, {'next': {'next': 3}})
        for schema in [{'type': 'object', '$ref': '#'},
                       {'type': 'object', '$defs': {'A': {'$ref': '#/$defs/B'},
                        'B': {'$ref': '#/$defs/A'}}, '$ref': '#/$defs/A'}]:
            wire.validate_request(request(schema))
            with self.subTest(schema=schema), self.assertRaisesRegex(ProtocolError, 'too_complex'):
                wire.validate_response(response({}), request(schema))

    def test_dynamic_and_recursive_references_under_their_dialects(self):
        for dialect, anchor, keyword in [(DRAFT2020, '$dynamicAnchor', '$dynamicRef'),
                                         (DRAFT2019, '$recursiveAnchor', '$recursiveRef')]:
            schema = {'$schema': dialect, 'type': 'object', anchor: 'node' if dialect == DRAFT2020 else True,
                      'properties': {'next': {keyword: '#node' if dialect == DRAFT2020 else '#'}}}
            with self.subTest(dialect=dialect):
                self.assertAccepts(schema, {'next': {'next': {}}})
                self.assertMismatch(schema, {'next': {'next': 3}})
                schema['properties']['next'][keyword] = 'https://never.invalid/schema'
                with self.assertRaises(ProtocolError):
                    wire.validate_request(request(schema))

    def test_explicit_nested_dialect_cannot_bypass_validation_budget(self):
        schema = {'$schema': DRAFT2020, 'type': 'object', '$defs': {
            'Loop': {'$schema': DRAFT2020, '$ref': '#/$defs/Loop'}}, '$ref': '#/$defs/Loop'}
        with self.assertRaisesRegex(ProtocolError, 'too_complex'):
            wire.validate_response(response({}), request(schema))

    def test_combinatorial_validation_is_bounded(self):
        schema = {'type': 'object', '$defs': {'Leaf': {'type': 'object'}}}
        for i in range(15):
            previous = 'Leaf' if i == 0 else str(i - 1)
            schema['$defs'][str(i)] = {'allOf': [{'$ref': '#/$defs/' + previous}] * 2}
        schema['$ref'] = '#/$defs/14'
        wire.validate_request(request(schema))
        with self.assertRaisesRegex(ProtocolError, 'too_complex'):
            wire.validate_response(response({}), request(schema))

    def test_schema_size_depth_dialects_and_ambiguous_resources_fail_closed(self):
        schema = reference_schema()
        with patch.object(wire, 'SCHEMA_MAX_NODES', 4), self.assertRaisesRegex(ProtocolError, 'too_complex'):
            wire.validate_request(request(schema))
        for dialect in ['https://unknown.invalid/schema', 'http://json-schema.org/draft-07/schema#']:
            schema = reference_schema(); schema['$schema'] = dialect
            with self.subTest(dialect=dialect), self.assertRaisesRegex(ProtocolError, 'dialect_unsupported'):
                wire.validate_request(request(schema))
        schema = reference_schema(); schema['$defs']['Value']['$schema'] = DRAFT2019
        with self.assertRaisesRegex(ProtocolError, 'dialect_unsupported'):
            wire.validate_request(request(schema))
        for field, val, code in [('$id', 'same', 'resource_duplicate'), ('$anchor', 'same', 'anchor_duplicate')]:
            schema = {'type': 'object', '$defs': {'A': {field: val}, 'B': {field: val}}}
            with self.subTest(field=field), self.assertRaisesRegex(ProtocolError, code):
                wire.validate_request(request(schema))


if __name__ == '__main__':
    unittest.main(verbosity=2)
