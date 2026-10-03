"""Codex text-history serialization regressions; synthetic local fixtures only."""
import copy
import tempfile
import unittest

from facade.runtime import create_runtime
from facade.test_global_runtime import config, http
from facade.wire import validate_history, validate_request, validate_response


# Exact public text response from the first Desktop acceptance turn. No claims,
# bearer, user environment, or other private source context is captured here.
REAL_FIRST_RESPONSE = {
    'id': 'resp_direct_text_reply_1', 'object': 'response', 'status': 'completed',
    'model': 'gpt-6-astra', 'output': [{
        'id': 'msg_direct_text_reply_1', 'type': 'message', 'role': 'assistant',
        'status': 'completed', 'content': [
            {'type': 'output_text', 'text': 'DIRECT_OK', 'annotations': []}]}]}


def first_request():
    return {'model': 'gpt-6-astra', 'reasoning': {'effort': 'xhigh'},
            'stream': True, 'tools': [], 'input': [
                {'role': 'user', 'content': '只回复 DIRECT_OK，不调用任何工具。'}]}


def codex_echo(item):
    # Source-derived typed ResponseItem::Message + ContentItem::OutputText
    # projection in rust-v0.159.2 (ff6aec96948b70d94983af2641a6b67c94faeff5).
    # These types retain text, but cannot retain annotations or logprobs.
    return {'type': 'message', 'role': item['role'], 'content': [
        {'type': part['type'], 'text': part['text']} for part in item['content']]}


def next_request(emitted=REAL_FIRST_RESPONSE):
    value = first_request()
    value['input'] += [codex_echo(item) for item in emitted['output']]
    value['input'].append({'role': 'user',
        'content': '你上一条回复的原文是什么？只回复原文，不调用任何工具。'})
    return value


class HistoryEchoTests(unittest.TestCase):
    def test_exact_real_envelope_and_codex_echo(self):
        original = copy.deepcopy(REAL_FIRST_RESPONSE)
        current = next_request()
        validate_request(current)
        validate_response(REAL_FIRST_RESPONSE, first_request())
        validate_history(current, first_request(), [REAL_FIRST_RESPONSE])
        self.assertEqual(REAL_FIRST_RESPONSE, original)
        self.assertEqual(current, next_request())

    def test_only_known_output_text_metadata_is_ignored(self):
        for key in ('annotations', 'logprobs'):
            for value in ([], [{'synthetic': 'metadata'}]):
                with self.subTest(key=key, value=value):
                    emitted = copy.deepcopy(REAL_FIRST_RESPONSE)
                    emitted['output'][0]['content'][0][key] = value
                    validate_history(next_request(emitted), first_request(), [emitted])
        emitted = copy.deepcopy(REAL_FIRST_RESPONSE)
        emitted['output'][0]['content'][0]['unknown_field'] = 'must remain exact'
        with self.assertRaisesRegex(ValueError, 'previous_assistant_message_missing'):
            validate_history(next_request(emitted), first_request(), [emitted])

    def test_missing_or_changed_text_role_type_and_boundaries_rejected(self):
        altered = [
            {'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'DIRECT_OK '} ]},
            {'role': 'user', 'content': [{'type': 'output_text', 'text': 'DIRECT_OK'}]},
            {'role': 'assistant', 'content': [{'type': 'input_text', 'text': 'DIRECT_OK'}]},
            {'role': 'assistant', 'content': 'DIRECT_OK'},
            {'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'DIRECT_'},
                                              {'type': 'output_text', 'text': 'OK'}]},
        ]
        for echo in altered:
            with self.subTest(echo=echo):
                current = next_request(); current['input'][1] = echo
                validate_request(current)
                with self.assertRaisesRegex(ValueError, 'previous_assistant_message_missing'):
                    validate_history(current, first_request(), [REAL_FIRST_RESPONSE])
        current = next_request(); current['input'].pop(1)
        with self.assertRaisesRegex(ValueError, 'previous_assistant_message_missing'):
            validate_history(current, first_request(), [REAL_FIRST_RESPONSE])

    def test_text_part_order_and_unknown_metadata_remain_exact(self):
        emitted = copy.deepcopy(REAL_FIRST_RESPONSE)
        emitted['output'][0]['content'].append({'type': 'output_text', 'text': 'second'})
        current = next_request(emitted)
        current['input'][1]['content'].reverse()
        with self.assertRaisesRegex(ValueError, 'previous_assistant_message_missing'):
            validate_history(current, first_request(), [emitted])
        current = next_request()
        current['input'][1]['content'][0]['unknown_field'] = 'new'
        with self.assertRaisesRegex(ValueError, 'previous_assistant_message_missing'):
            validate_history(current, first_request(), [REAL_FIRST_RESPONSE])

    def test_prior_client_prefix_stays_exact(self):
        current = next_request()
        current['input'][0]['content'] += ' '
        with self.assertRaisesRegex(ValueError, 'full_history_prefix_mismatch'):
            validate_history(current, first_request(), [REAL_FIRST_RESPONSE])

    def test_tool_call_binding_is_not_normalized(self):
        emitted = copy.deepcopy(REAL_FIRST_RESPONSE)
        tool = {'id': 'fc_fixture', 'type': 'function_call', 'call_id': 'call-fixture',
                'name': 'probe', 'arguments': '{}'}
        emitted['output'].append(tool)
        current = next_request()
        current['input'][2:2] = [copy.deepcopy(tool), {'type': 'function_call_output',
            'call_id': tool['call_id'], 'output': 'fixture'}]
        validate_history(current, first_request(), [emitted])
        for key, changed in [('id', 'fc_changed'), ('call_id', 'changed'),
                             ('name', 'other'), ('arguments', '{ }')]:
            with self.subTest(key=key):
                candidate = copy.deepcopy(current); candidate['input'][2][key] = changed
                with self.assertRaisesRegex(ValueError, 'tool_history_does_not_match_delivered_calls'):
                    validate_history(candidate, first_request(), [emitted])


class GlobalHistoryEchoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = config(self.temp.name)
        self.runtime = create_runtime(self.cfg)
        self.runtime.start_http()
        self.identity = {'session-id': 'fixture-session', 'thread-id': 'fixture-thread'}

    def tearDown(self):
        self.runtime.close()
        if hasattr(self.runtime, "diagnostics"):
            self.assertTrue(self.runtime.diagnostics.wait_idle())
        self.temp.cleanup()

    def restart(self):
        self.runtime.close()
        if hasattr(self.runtime, "diagnostics"):
            self.assertTrue(self.runtime.diagnostics.wait_idle())
        self.runtime = create_runtime(self.cfg)
        self.runtime.start_http()

    def prepare(self, emitted=REAL_FIRST_RESPONSE):
        route, rid = self.runtime.ingest(self.identity, first_request())
        claimed = self.runtime.call_tool('get_request', {'route_id': route,
            'worker_id': 'native-fixture', 'context_epoch': 'fixture-epoch',
            'model': 'gpt-6-astra', 'reasoning_effort': 'xhigh', 'wait_ms': 0})
        self.runtime.call_tool('finish_request', {**self.scope(claimed),
            'action_id': 'fixture-first', 'response': emitted, 'schema_tokens': []})
        code, returned = http(self.runtime.http.server_port, self.cfg['http_bearer'],
                              self.identity, first_request())
        self.assertEqual(code, 200)
        self.assertEqual(returned, emitted)
        return claimed

    @staticmethod
    def scope(claimed):
        return {k: claimed[k] for k in ('route_id', 'claim_token', 'request_id', 'context_token')}

    def observe(self, claimed, after_seq):
        return self.runtime.call_tool('get_request', {'route_id': claimed['route_id'],
            'claim_token': claimed['claim_token'], 'after_seq': after_seq, 'wait_ms': 0})

    def test_real_envelope_survives_restart_then_two_more_turns(self):
        claimed = self.prepare()
        self.restart()
        current = next_request()
        route, rid = self.runtime.ingest(self.identity, current)
        self.assertEqual(route, claimed['route_id'])
        second = self.observe(claimed, 1)
        self.assertEqual(second['request_id'], rid)
        self.assertEqual(second['context']['kind'], 'delta')
        self.assertEqual(second['context']['append'], current['input'][1:])
        self.assertEqual(self.runtime.routes[route]['requests'][0]['response'], REAL_FIRST_RESPONSE)
        final = copy.deepcopy(REAL_FIRST_RESPONSE)
        final['id'] = 'resp_second'; final['output'][0]['id'] = 'msg_second'
        final['output'][0]['content'][0]['logprobs'] = []
        self.runtime.call_tool('finish_request', {**self.scope(second),
            'action_id': 'fixture-second', 'response': final, 'schema_tokens': []})
        self.assertEqual(http(self.runtime.http.server_port, self.cfg['http_bearer'],
                              self.identity, current)[0], 200)
        third = copy.deepcopy(current)
        third['input'] += [codex_echo(final['output'][0]), {'role': 'user', 'content': 'third'}]
        self.runtime.ingest(self.identity, third)
        observed = self.observe(claimed, 2)
        self.assertEqual(observed['context']['kind'], 'delta')
        self.assertEqual(observed['context']['append'], third['input'][len(current['input']):])
        metrics = self.runtime.routes[route]['metrics']
        self.assertEqual(metrics['full_context_returns'], 1)
        self.assertEqual(metrics['delta_context_returns'], 2)
        self.assertEqual(metrics['response_commits'], 2)

    def test_assistant_top_level_optional_annotations_supported(self):
        claimed = self.prepare()
        current = next_request()
        current['input'][1].pop('type')
        self.runtime.ingest(self.identity, current)
        self.assertEqual(self.observe(claimed, 1)['context']['append'], current['input'][1:])

    def test_rejected_history_does_not_mutate_route_or_admit_request(self):
        claimed = self.prepare()
        original = copy.deepcopy(self.runtime.routes[claimed['route_id']])
        for change in ('text', 'role', 'prefix', 'missing', 'extra_top_level', 'message_order'):
            with self.subTest(change=change):
                current = next_request()
                if change == 'text': current['input'][1]['content'][0]['text'] += ' '
                elif change == 'role': current['input'][1]['role'] = 'user'
                elif change == 'prefix': current['input'][0]['content'] += ' '
                elif change == 'missing': current['input'].pop(1)
                elif change == 'extra_top_level': current['input'][1]['phase'] = 'final_answer'
                else: current['input'][1:] = list(reversed(current['input'][1:]))
                with self.assertRaises(ValueError):
                    self.runtime.ingest(self.identity, current)
                self.assertEqual(self.runtime.routes[claimed['route_id']], original)

    def test_http_regression_admits_serialized_echo_without_recommit(self):
        # A short HTTP wait returns 504 after successful admission if no worker
        # has replied yet; the bug instead rejects before admission with 409.
        self.runtime.http_wait_ms = 10
        claimed = self.prepare()
        code, body = http(self.runtime.http.server_port, self.cfg['http_bearer'],
                          self.identity, next_request())
        self.assertEqual(code, 504, body)
        route = self.runtime.routes[claimed['route_id']]
        self.assertEqual(len(route['requests']), 2)
        self.assertEqual(route['metrics']['response_commits'], 1)
        self.assertEqual(self.observe(claimed, 1)['context']['kind'], 'delta')


if __name__ == '__main__':
    unittest.main()
