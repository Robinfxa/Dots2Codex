"""Pinned text-only trial and read-only, owner-routed recovery regressions."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import tomllib
import unittest
from unittest import mock

from dots_lite import config_transaction as tx
from dots_lite.client_catalog import catalog_for_selection, load_catalog, select
from dots_lite.launcher import CONTRACT, Launcher, Ports, parser, retrieve_result
from dots_lite.private_io import private_dir, private_write, read, save
from dots_lite.protocol import DEFAULT_LIMITS, PROTOCOL, ProtocolError, canonical, hash_bytes


class TrialCapabilities(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = private_dir(self.root / 'home', create=True)
        self.state = private_dir(self.root / 'state', create=True)
        self.path = self.home / 'config.toml'
        self.pair = select(load_catalog(), 'gpt-6-astra', 'xhigh')
        self.info = {'protocol': PROTOCOL, 'generation': 'a'*32, 'selection': self.pair,
                     'catalog_path': str(self.state / 'catalog.json'),
                     'base_url': 'http://127.0.0.1:43187/activations/' + 'a'*32 + '/v1'}

    def apply(self):
        plan = tx.preview(self.state, self.home, self.info)
        return tx.apply(self.state, self.home, self.info, expected_before_hash=plan['before_hash'],
                        expected_after_hash=plan['after_hash'], confirm=True, check_ready=lambda: None)

    def test_all_advertised_pairs_are_text_only_standard_responses_without_server_search(self):
        for model, entry in load_catalog()['models'].items():
            for effort in entry['bridge_efforts']:
                with self.subTest(model=model, effort=effort):
                    row, = catalog_for_selection(select(load_catalog(), model, effort))['models']
                    self.assertEqual(row['slug'], model)
                    self.assertEqual(row['default_reasoning_level'], effort)
                    self.assertEqual([x['effort'] for x in row['supported_reasoning_levels']], [effort])
                    self.assertIs(row['supports_search_tool'], False)
                    self.assertIs(row['use_responses_lite'], False)
                    self.assertIs(row['supports_reasoning_effort_updates'], False)
                    self.assertIs(row['supports_reasoning_summary_parameter'], False)
                    self.assertEqual(row['input_modalities'], ['text'])
                    self.assertEqual(row['experimental_supported_tools'], [])
                    self.assertNotIn('approval_policy', row)
                    self.assertNotIn('sandbox_mode', row)

    def test_default_trial_patch_consent_and_exact_crlf_restore(self):
        original = (b'# personal\r\nmodel = "old" # keep model\r\nweb_search = "cached" # keep search\r\n'
                    b'[profiles.work]\r\nmodel = "profile-model"\r\nweb_search = "live"\r\n'
                    b'[projects."/tmp/project"]\r\nmodel = "project-model"\r\n')
        private_write(self.path, original)
        plan = tx.preview(self.state, self.home, self.info)
        self.assertIn('web_search:', plan['diff']); self.assertIn('"disabled"', plan['diff'])
        self.assertTrue(any('override' in warning for warning in plan['warnings']))
        self.assertEqual(self.path.read_bytes(), original)
        result = self.apply(); raw = self.path.read_bytes(); value = tomllib.loads(raw.decode())
        self.assertEqual((value['model'], value['model_reasoning_effort']), ('gpt-6-astra', 'xhigh'))
        self.assertEqual(value['web_search'], 'disabled')
        self.assertEqual(value['profiles']['work'], {'model': 'profile-model', 'web_search': 'live'})
        self.assertEqual(value['projects']['/tmp/project']['model'], 'project-model')
        self.assertNotIn(b'\n', raw.replace(b'\r\n', b''))
        self.assertIn(b'# keep search\r\n', raw)
        manifest = read(self.state / 'config-transactions' / (result['transaction_id'] + '.json'))
        self.assertIn(['web_search'], manifest['owned_paths'])
        tx.restore(self.state, result['transaction_id'], confirm=True)
        self.assertEqual(self.path.read_bytes(), original)

    def test_three_way_restore_preserves_unrelated_crlf_edit_and_original_search_comment(self):
        original = b'# original\r\nweb_search = "live" # user choice\r\n'
        private_write(self.path, original); result = self.apply()
        private_write(self.path, self.path.read_bytes() + b'\r\n[extra]\r\nflag = true # later edit\r\n')
        restored = tx.restore(self.state, result['transaction_id'], confirm=True)
        self.assertEqual(restored['mode'], 'three_way_owned_values')
        raw = self.path.read_bytes()
        self.assertIn(b'web_search = "live" # user choice\r\n', raw)
        self.assertIn(b'flag = true # later edit\r\n', raw)
        self.assertNotIn(b'\n', raw.replace(b'\r\n', b''))

    def test_changed_search_value_or_comment_blocks_restore(self):
        for change in (b'web_search = "cached"', b'web_search = "disabled" # user changed'):
            with self.subTest(change=change), tempfile.TemporaryDirectory(dir=self.root) as nested:
                state = private_dir(Path(nested) / 'state', create=True)
                plan = tx.preview(state, self.home, self.info)
                result = tx.apply(state, self.home, self.info, expected_before_hash=plan['before_hash'],
                                  expected_after_hash=plan['after_hash'], confirm=True, check_ready=lambda: None)
                private_write(self.path, self.path.read_bytes().replace(b'web_search = "disabled"', change))
                current = self.path.read_bytes()
                with self.assertRaisesRegex(ProtocolError, 'restore_owned_(value|syntax)_conflict'):
                    tx.restore(state, result['transaction_id'], confirm=True)
                self.assertEqual(self.path.read_bytes(), current)
                self.path.unlink()

    def test_old_transaction_does_not_newly_own_search_setting(self):
        private_write(self.path, b'web_search = "cached" # original\n')
        with mock.patch.object(tx, 'OWNED', tx.LEGACY_OWNED):
            result = self.apply()
        journal = self.state / 'config-transactions' / (result['transaction_id'] + '.json')
        value = read(journal); value.pop('owned_paths'); save(journal, value)
        private_write(self.path, self.path.read_bytes().replace(b'"cached" # original', b'"live" # new user setting'))
        tx.restore(self.state, result['transaction_id'], confirm=True)
        self.assertIn(b'web_search = "live" # new user setting', self.path.read_bytes())
        self.assertNotIn(b'dots2codex_lightweight_v3', self.path.read_bytes())


class LauncherRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.state = private_dir(self.root/'state', create=True)
        self.activation = 'a'*32; self.request_id = 'b'*32
        self.runtime = private_dir(self.state/'runs'/self.activation, create=True)
        self.active = {'contract': CONTRACT, 'run_id': self.activation, 'runtime': str(self.runtime),
                       'pid': 42, 'process_identity': 'owner'}
        save(self.state/'current.json', self.active)
        self.grant = {'protocol': PROTOCOL, 'activation_id': self.activation, 'folder_id': 'folder',
                      'inbox_id': 'inbox', 'created_at': 1, 'expires_at': 2,
                      'allowed_pairs': [{'model': 'gpt-6-astra', 'reasoning_effort': 'xhigh'}],
                      'limits': dict(DEFAULT_LIMITS), 'package_sha256': 'd'*64}
        save(self.runtime/'grant.json', self.grant); private_write(self.runtime/'join-key', b'c'*64)
        self.info = {'protocol': PROTOCOL, 'generation': self.activation,
                     'selection': select(load_catalog(), 'gpt-6-astra', 'xhigh'),
                     'base_url': 'http://127.0.0.1:43187/activations/' + self.activation + '/v1'}
        save(self.runtime/'config-info.json', self.info)
        self.response = {'id': 'response-id', 'object': 'response', 'status': 'completed', 'model': 'gpt-6-astra',
                         'output': [{'type': 'message', 'id': 'message-id', 'role': 'assistant',
                                     'content': [{'type': 'output_text', 'text': 'The late verified answer 🌲'}]}]}
        self.snapshot = {'protocol': PROTOCOL, 'activation_id': self.activation,
                         'ticket': {'request_id': self.request_id, 'route_id': 'e'*32, 'seq': 1,
                                    'request_sha256': 'f'*64, 'state': 'verified'},
                         'result_available': True, 'result': self.response, 'result_locator': {},
                         'delivery_started': False, 'tool_output_withheld': False,
                         'read_only': True, 'sse_emitted': False, 'client_connection_resumed': False}
        self.ports = mock.Mock()
        self.ports.health.return_value = {'protocol': PROTOCOL, 'activation_id': self.activation,
                                         'listener_ready': True, 'stopped': True}
        self.ports.recover.return_value = self.snapshot
        self.launcher = Launcher(root=self.root, state=self.state, ports=self.ports)
        for name in ('alive', 'owned'):
            patch = mock.patch('dots_lite.launcher.' + name, return_value=True)
            patch.start(); self.addCleanup(patch.stop)

    def test_expired_closed_actor_result_uses_live_owner_and_saves_private_text_only(self):
        from dots_lite.gateway import recovery_token
        with mock.patch('dots_lite.gateway.MacGateway', side_effect=AssertionError('second owner')), \
             mock.patch('dots_lite.storage.Journal', side_effect=AssertionError('journal read')):
            result = self.launcher.recover(self.request_id)
        self.assertTrue(result['verified_result_saved']); self.assertNotIn('output_text', result)
        self.assertFalse(result['client_connection_resumed']); self.assertFalse(result['sse_emitted'])
        self.assertEqual(Path(result['result_path']).stat().st_mode & 0o777, 0o600)
        saved = read(result['result_path']); self.assertEqual(saved['result'], self.response)
        self.assertEqual(saved['result_sha256'], hash_bytes(canonical(self.response)))
        self.ports.recover.assert_called_once_with(self.info['base_url'], self.activation, self.request_id,
            recovery_token('c'*64, self.activation, self.request_id), max_bytes=DEFAULT_LIMITS['max_result_bytes'])
        self.ports.spawn.assert_not_called()
        printed = self.launcher.recover(self.request_id, print_result=True)
        self.assertEqual(printed['output_text'], 'The late verified answer 🌲')
        self.assertNotIn('c'*64, json.dumps(printed))

    def test_pending_and_tools_never_saved_or_printed(self):
        for available, withheld, state in ((False, False, 'published'), (True, True, 'verified'),
                                           (False, False, 'acknowledged')):
            with self.subTest(state=state):
                self.snapshot.update(result_available=available, result=None, tool_output_withheld=withheld)
                self.snapshot['ticket']['state'] = state
                result = self.launcher.recover(self.request_id, print_result=True)
                self.assertFalse(result['verified_result_saved']); self.assertNotIn('output_text', result)
                self.assertFalse((self.runtime/'recovered').exists())

    def test_offline_owner_cannot_open_actor_or_start_new_grant(self):
        with mock.patch('dots_lite.launcher.alive', return_value=False), \
             mock.patch('dots_lite.gateway.MacGateway', side_effect=AssertionError('second owner')):
            with self.assertRaisesRegex(ProtocolError, 'resume_same_activation_with_matching_package'):
                self.launcher.recover(self.request_id)
        self.ports.recover.assert_not_called(); self.ports.spawn.assert_not_called()

    def test_wrong_activation_identity_or_false_resume_claim_fails_closed(self):
        for key, invalid in (('activation_id', 'x'*32), ('read_only', False),
                             ('client_connection_resumed', True), ('sse_emitted', True)):
            with self.subTest(key=key):
                self.ports.recover.return_value = {**self.snapshot, key: invalid}
                with self.assertRaises(ProtocolError): self.launcher.recover(self.request_id)
                self.assertFalse((self.runtime/'recovered').exists())
        self.ports.recover.return_value = {**self.snapshot, 'ticket': {**self.snapshot['ticket'], 'request_id': '0'*32}}
        with self.assertRaisesRegex(ProtocolError, 'recovery_ticket_mismatch'): self.launcher.recover(self.request_id)

    def test_tool_smuggling_and_noncompleted_response_are_not_saved(self):
        for result in ({**self.response, 'status': 'in_progress'},
                       {**self.response, 'output': [{'type': 'function_call', 'id': 'tool', 'call_id': 'call',
                         'name': 'exec_command', 'arguments': '{}'}]}):
            with self.subTest(result=result):
                self.ports.recover.return_value = {**self.snapshot, 'result': result}
                with self.assertRaises(ProtocolError): self.launcher.recover(self.request_id, print_result=True)
                self.assertFalse((self.runtime/'recovered').exists())

    def test_cli_requires_explicit_request_and_output_flag(self):
        args = parser().parse_args(['recover', '--request-id', self.request_id])
        self.assertEqual(args.request_id, self.request_id); self.assertFalse(args.print_result)
        self.assertTrue(parser().parse_args(['recover', '--request-id', self.request_id, '--print-result']).print_result)
        for invalid in ('../secret', 'A'*32, 'a'*31, '', None):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ProtocolError, 'invalid_local_request_id'):
                self.launcher.recover(invalid)
        self.ports.recover.assert_not_called()

    def test_menu_recovery_prompts_for_exact_request_without_printing_text(self):
        from dots_lite.launcher import main
        ui = mock.Mock(); ui.choose.return_value = 'Recover existing request'; ui.text.return_value = self.request_id
        launcher = mock.Mock(); launcher.recover.return_value = {'result_available': False}
        with mock.patch('dots_lite.launcher.UI', return_value=ui), \
             mock.patch('dots_lite.launcher.Launcher', return_value=launcher):
            self.assertEqual(main(['menu']), 0)
        launcher.recover.assert_called_once_with(self.request_id, print_result=False)
        launcher.start.assert_not_called(); launcher.resume.assert_not_called()

    def test_existing_recovery_file_cannot_be_overwritten_with_different_text(self):
        result = self.launcher.recover(self.request_id)
        path = Path(result['result_path']); before = path.read_bytes()
        self.snapshot['result'] = copy.deepcopy(self.response)
        self.snapshot['result']['output'][0]['content'][0]['text'] = 'A different verified answer with longer text 🌲'
        with self.assertRaisesRegex(ProtocolError, 'immutable_file_conflict'):
            self.launcher.recover(self.request_id, print_result=True)
        self.assertEqual(path.read_bytes(), before)

    def test_http_helper_is_one_get_with_scoped_token_only(self):
        import http.server
        seen = []
        value = self.snapshot
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, dict(self.headers)))
                body = canonical(value)
                self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args): pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}/activations/{self.activation}/v1'
            result = retrieve_result(base, self.activation, self.request_id, '1'*64, max_bytes=1024*1024)
            self.assertEqual(result, self.snapshot)
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0][0], f'/activations/{self.activation}/v1/responses/{self.request_id}')
            self.assertEqual(seen[0][1]['X-Dots-Recovery-Token'], '1'*64)
            self.assertNotIn('session-id', seen[0][1]); self.assertNotIn('Authorization', seen[0][1])
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_real_owner_http_recovery_verifies_late_result_without_second_gateway(self):
        import time
        from dots_lite.gateway import MacGateway, ResponsesServer
        from lite_tests.gateway_fixtures import FakeGoogle, KEY, request
        self.grant.update(created_at=int(time.time())-1, expires_at=int(time.time())+3600,
                          inbox_id='inbox-test', folder_id='folder-test')
        save(self.runtime/'grant.json', self.grant); private_write(self.runtime/'join-key', KEY.encode())
        google = FakeGoogle()
        gateway = MacGateway(self.runtime/'gateway', self.grant, KEY, google, google,
                             create=True, poll_seconds=.01, wait_seconds=1)
        gateway.initialize()
        ticket = gateway.submit({'session-id': 'original-session', 'thread-id': 'original-thread'},
                                canonical(request(model='gpt-6-astra', effort='xhigh')))
        google.publish_result(gateway, ticket, self.response)
        gateway.stop()  # Existing result remains readable; no new admission.
        server = ResponsesServer(gateway)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            save(self.runtime/'config-info.json', {**self.info, 'base_url': server.base_url})
            self.launcher.ports = Ports()
            mutations_before = [call for call in google.calls if call[0] in ('upload', 'batch_update', 'create_document')]
            with mock.patch('dots_lite.gateway.MacGateway', side_effect=AssertionError('second owner')):
                result = self.launcher.recover(ticket['request_id'], print_result=True)
            self.assertEqual(result['output_text'], 'The late verified answer 🌲')
            self.assertTrue(result['verified_result_saved'])
            job = gateway.journal.read()['requests'][ticket['request_id']]
            self.assertEqual(job['state'], 'verified'); self.assertFalse(job['delivery_started'])
            self.assertEqual(mutations_before, [call for call in google.calls
                if call[0] in ('upload', 'batch_update', 'create_document')])
        finally:
            server.shutdown(); server.server_close(); thread.join(); gateway.close()

    def test_http_helper_rejects_remote_or_mismatched_endpoint(self):
        for base in ('https://example.com/v1', 'http://127.0.0.1:43/activations/wrong/v1',
                     'http://user@127.0.0.1:43/activations/' + self.activation + '/v1'):
            with self.subTest(base=base), self.assertRaisesRegex(ProtocolError, 'invalid_recovery_endpoint'):
                retrieve_result(base, self.activation, self.request_id, '1'*64, max_bytes=1024)


if __name__ == '__main__': unittest.main()
