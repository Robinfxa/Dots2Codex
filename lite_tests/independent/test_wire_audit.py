"""Independent, offline Responses-wire and protocol admission regressions."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent
REPO = Path(os.environ["LITE_REPO"]) if "LITE_REPO" in os.environ else next(parent for parent in ROOT.parents if (parent / "dots_lite").is_dir())
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(ROOT))
from dots_lite import protocol as p, wire


def request(text='Synthetic project A task'):
    return {'model': 'gpt-6.1-sol', 'reasoning': {'effort': 'xhigh'}, 'stream': True,
            'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}],
            'tools': [{'type': 'namespace', 'name': 'local', 'tools': [
                {'type': 'function', 'name': 'read_file', 'parameters': {
                    'type': 'object', 'properties': {'path': {'type': 'string'}},
                    'required': ['path'], 'additionalProperties': False}},
                {'type': 'custom', 'name': 'apply_patch', 'format': {'type': 'text'}}]}]}


def response(item, rid='resp_synthetic_a'):
    return {'id': rid, 'object': 'response', 'model': 'gpt-6.1-sol',
            'status': 'completed', 'output': [item]}


def function_call():
    return {'id': 'fc_synthetic_a', 'type': 'function_call', 'call_id': 'call_synthetic_a',
            'namespace': 'local', 'name': 'read_file', 'arguments': '{"path":"src/a.py"}'}


def custom_call():
    return {'id': 'ct_synthetic_a', 'type': 'custom_tool_call', 'call_id': 'call_custom_a',
            'namespace': 'local', 'name': 'apply_patch', 'input': '*** Begin Patch\n*** End Patch'}


class WireAudit(unittest.TestCase):
    def test_function_and_custom_roundtrip_preserve_exact_ids(self):
        for item, output_kind in [(function_call(), 'function_call_output'),
                                  (custom_call(), 'custom_tool_call_output')]:
            with self.subTest(kind=item['type']):
                first = request()
                result = response(item)
                original = copy.deepcopy(result)
                self.assertEqual(wire.validate_response(result, first), original)
                continued = copy.deepcopy(first)
                continued['input'] += [copy.deepcopy(item), {'type': output_kind,
                    'call_id': item['call_id'], 'output': 'synthetic tool result'}]
                wire.validate_request(continued)
                wire.validate_history(continued, first, [result])
                data = wire.response_events(result)
                events = [json.loads(line[6:]) for line in data.splitlines() if line.startswith(b'data: ')]
                self.assertEqual(events[-1]['response'], original)
                self.assertEqual([e['sequence_number'] for e in events], list(range(len(events))))
                self.assertEqual(result, original)

    def test_forged_tool_arguments_namespace_and_call_id_are_rejected(self):
        first = request(); result = response(function_call())
        for field, value in [('arguments', '{"path":"unapproved.py"}'),
                             ('namespace', 'different'), ('call_id', 'call_different'),
                             ('id', 'fc_different')]:
            continued = copy.deepcopy(first); altered = copy.deepcopy(result['output'][0])
            altered[field] = value
            continued['input'] += [altered, {'type': 'function_call_output',
                'call_id': altered['call_id'], 'output': 'synthetic result'}]
            with self.subTest(field=field), self.assertRaises(p.ProtocolError):
                wire.validate_history(continued, first, [result])

    def test_other_project_call_history_cannot_be_borrowed(self):
        other = request('Synthetic unrelated project B task')
        item = function_call()
        other['input'] += [item, {'type': 'function_call_output', 'call_id': item['call_id'], 'output': 'x'}]
        with self.assertRaises(p.ProtocolError):
            wire.validate_history(other, request('Synthetic unrelated project B task'), [])

    def test_unresolved_duplicate_and_wrong_kind_tool_outputs_rejected(self):
        for tail in [[], [{'type': 'custom_tool_call_output', 'call_id': 'call_synthetic_a', 'output': 'x'}],
                     [{'type': 'function_call_output', 'call_id': 'call_synthetic_a', 'output': 'x'}] * 2]:
            candidate = request(); candidate['input'] += [function_call()] + tail
            with self.subTest(tail=tail), self.assertRaises(p.ProtocolError): wire.validate_request(candidate)

    def test_schema_reference_and_invalid_arguments_are_rejected(self):
        invalid = response(function_call()); invalid['output'][0]['arguments'] = '{"path":1}'
        with self.assertRaises(p.ProtocolError): wire.validate_response(invalid, request())
        invalid = response(function_call()); invalid['output'][0]['arguments'] = '{"path":"a","path":"b"}'
        with self.assertRaises(p.ProtocolError): wire.validate_response(invalid, request())
        invalid_request = request()
        invalid_request['tools'][0]['tools'][0]['parameters']['properties']['path']['$ref'] = 'https://example.invalid/schema'
        with self.assertRaises(p.ProtocolError): wire.validate_request(invalid_request)

    def test_previous_response_id_is_explicitly_unsupported(self):
        candidate = request(); candidate['previous_response_id'] = 'resp_unknown'
        with self.assertRaisesRegex(p.ProtocolError, 'previous_response_id_unsupported'):
            wire.validate_request(candidate)

    def test_ambiguous_effort_aliases_rejected(self):
        for alias in ['reasoning_effort', 'model_reasoning_effort']:
            candidate = request(); candidate[alias] = 'low'
            with self.subTest(alias=alias), self.assertRaises(p.ProtocolError): wire.validate_request(candidate)

    def test_request_and_result_caps_apply_before_delivery(self):
        with self.assertRaises(p.ProtocolError): wire.validate_request(request(), max_bytes=1)
        with self.assertRaises(p.ProtocolError): wire.validate_response(response(function_call()), request(), max_bytes=1)

    def test_import_graph_is_independent_of_legacy_runtime(self):
        self.assertFalse(any(name == 'remote_transport' or name.startswith('remote_transport.') for name in sys.modules))


if __name__ == '__main__': unittest.main(verbosity=2)
