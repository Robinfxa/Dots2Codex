"""Synthetic loopback replay regressions; no inference or client-tool execution."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT.parent)]
from facade.runtime import create_runtime
from facade.test_global_runtime import config, http, request, response


class HTTPReplayRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = config(self.temp.name)
        self.cfg['http_wait_ms'] = 40
        self.runtime = create_runtime(self.cfg)
        self.runtime.start_http()
        self.identity = {'session-id': 'replay-fixture', 'thread-id': 'same-thread'}
        self.request = request('Synthetic bounded HTTP replay.')

    def tearDown(self):
        self.runtime.close()
        self.assertTrue(self.runtime.diagnostics.wait_idle())
        self.temp.cleanup()

    def post(self, body=None, key=None):
        return http(self.runtime.http.server_port, self.cfg['http_bearer'],
                    self.identity, self.request if body is None else body, key=key)

    def claim(self, route_id):
        return self.runtime.call_tool('get_request', {
            'route_id': route_id, 'worker_id': 'fixture-worker', 'context_epoch': 'stable',
            'model': 'gpt-6-astra', 'reasoning_effort': 'xhigh', 'wait_ms': 0})

    @staticmethod
    def scope(item):
        return {key: item[key] for key in ('route_id', 'claim_token', 'request_id', 'context_token')}

    def finish(self, item):
        return self.runtime.call_tool('finish_request', {
            **self.scope(item), 'action_id': 'same-finish',
            'response': response(), 'schema_tokens': []})

    def state(self):
        return self.runtime.status()['routes'][0]

    def restart(self):
        port = self.runtime.http.server_port
        self.runtime.close()
        self.assertTrue(self.runtime.diagnostics.wait_idle())
        self.runtime = create_runtime(self.cfg)
        self.runtime.start_http(port=port)

    def test_late_text_exact_replay_does_not_recommit(self):
        code, pending = self.post()
        self.assertEqual(code, 504)
        item = self.claim(pending['route_id'])
        self.finish(item)
        self.assertEqual(self.state()['metrics']['http_emissions'], 0)
        first = self.post()
        self.assertEqual(first[0], 200)
        self.assertEqual(self.post(), first)
        self.finish(item)  # Same native action receipt, not another execution.
        state = self.state()
        self.assertEqual(state['requests'], 1)
        self.assertEqual(state['metrics']['full_context_returns'], 1)
        self.assertEqual(state['metrics']['response_commits'], 1)
        self.assertEqual(state['metrics']['http_emissions'], 1)

    def test_two_timeouts_keep_one_request_and_late_commit_unemitted(self):
        first = self.post()
        second = self.post()
        self.assertEqual(first[0], 504)
        self.assertEqual(second, first)
        item = self.claim(first[1]['route_id'])
        self.finish(item)
        state = self.state()
        self.assertEqual(state['requests'], 1)
        self.assertFalse(state['request_pending'])
        self.assertEqual(state['metrics']['response_commits'], 1)
        self.assertEqual(state['metrics']['http_emissions'], 0)
        self.assertFalse(self.runtime.routes[item['route_id']]['requests'][0]['delivery_started'])

    def test_timeout_restart_preserves_owner_request_and_context(self):
        code, pending = self.post()
        self.assertEqual(code, 504)
        original = self.claim(pending['route_id'])
        self.restart()
        replay = self.runtime.call_tool('get_request', {
            'route_id': original['route_id'], 'claim_token': original['claim_token'],
            'after_seq': 0, 'wait_ms': 0})
        self.assertTrue(replay['replayed'])
        self.assertEqual(self.scope(replay), self.scope(original))
        self.assertEqual(replay['context'], original['context'])
        self.assertEqual(self.state()['worker_id'], 'fixture-worker')
        self.finish(replay)
        self.assertEqual(self.post()[0], 200)
        self.assertEqual(self.state()['requests'], 1)
        self.assertEqual(self.state()['metrics']['full_context_returns'], 1)
        self.assertEqual(self.state()['metrics']['response_commits'], 1)

    def test_emitted_tool_stays_fenced_after_restart_and_action_replay(self):
        route, unused = self.runtime.ingest(self.identity, self.request)
        item = self.claim(route)
        found = self.runtime.call_tool('discover_tools', {
            **self.scope(item), 'query': 'fixture_probe'})['matches'][0]
        schema = self.runtime.call_tool('lookup_schema', {
            **self.scope(item), 'name': found['key'], 'sha256': found['schema_sha256']})
        action = {**self.scope(item), 'action_id': 'same-tool-action',
                  'response': response(tool=True), 'schema_tokens': [schema['schema_token']],
                  'wait_ms': 0}
        self.runtime.call_tool('submit_action_and_wait_result', action)
        self.assertEqual(self.post()[0], 200)
        self.restart()
        code, blocked = self.post()
        self.assertEqual(code, 409)
        self.assertEqual(blocked['error']['code'], 'delivery_outcome_unknown_no_reemission')
        self.runtime.call_tool('submit_action_and_wait_result', action)
        state = self.state()
        self.assertEqual(state['requests'], 1)
        self.assertEqual(state['metrics']['response_commits'], 1)
        self.assertEqual(state['metrics']['http_emissions'], 1)
        self.assertEqual(len(self.runtime.routes[route]['actions']), 1)

    def test_same_key_changed_payload_remains_conflict(self):
        route, unused = self.runtime.ingest(self.identity, self.request, 'same-key')
        self.finish(self.claim(route))
        changed = copy.deepcopy(self.request)
        changed['input'][-1]['content'] = 'Different client operation.'
        code, blocked = self.post(changed, key='same-key')
        self.assertEqual(code, 409)
        self.assertEqual(blocked['error']['code'], 'request_id_conflict')
        self.assertEqual(self.state()['requests'], 1)
        self.assertEqual(self.state()['metrics']['response_commits'], 1)
        self.assertEqual(self.state()['metrics']['http_emissions'], 0)


if __name__ == '__main__':
    unittest.main()
