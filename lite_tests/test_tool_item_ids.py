"""Pinned-client tool item IDs are validated, never repaired or rewritten."""
import copy
import unittest

from dots_lite.protocol import ProtocolError
from dots_lite.wire import validate_response, validate_request, validate_history
from lite_tests.gateway_fixtures import request, response


class ToolItemIds(unittest.TestCase):
    def test_function_and_custom_ids_require_prefix_and_suffix(self):
        for kind in ('function_call', 'custom_tool_call'):
            for item_id in ('withoutseparator', '_suffix', 'prefix_', '_'):
                with self.subTest(kind=kind, item_id=item_id):
                    value = response('synthetic', kind)
                    value['output'][0]['id'] = item_id
                    before = copy.deepcopy(value)
                    with self.assertRaisesRegex(ProtocolError, '^tool_item_id_prefix_suffix_required$'):
                        validate_response(value, request())
                    self.assertEqual(value, before)

    def test_valid_ids_and_separate_call_id_survive_full_history(self):
        for kind in ('function_call', 'custom_tool_call'):
            with self.subTest(kind=kind):
                req = request()
                value = response('synthetic', kind)
                item = value['output'][0]
                item['id'] = 'clientprefix_suffix_with_underscores'
                item['call_id'] = 'separate-call-identity'
                checked = validate_response(value, req)
                self.assertEqual(checked, value)
                self.assertEqual(item['namespace'], 'functions')
                self.assertNotIn('.', item['name'])
                follow = copy.deepcopy(req)
                follow['input'] += [copy.deepcopy(item), {
                    'type': 'function_call_output' if kind == 'function_call' else 'custom_tool_call_output',
                    'call_id': 'separate-call-identity', 'output': 'synthetic output'}]
                self.assertEqual(validate_request(follow), follow)
                validate_history(follow, req, [checked])

    def test_message_ids_keep_existing_non_tool_contract(self):
        value = response('synthetic')
        value['output'][0]['id'] = 'plain-message-id'
        self.assertEqual(validate_response(value, request()), value)


if __name__ == '__main__': unittest.main()
