"""Offline local-file safety checks only; zero Google/consent calls."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import mac_google_setup as m

class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def test_create_private_no_overwrite(self):
        p=self.root/'x.json'; m.write_json(p, {'ok': True})
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError): m.write_json(p, {'ok': False})
        self.assertTrue(m.read_private_json(p)['ok'])
    def test_refuse_readable_by_group(self):
        p=self.root/'x.json'; m.write_json(p, {})
        p.chmod(0o640)
        with self.assertRaises(m.SetupError): m.read_private_json(p)
    def test_refuse_symlink(self):
        p=self.root/'x.json'; m.write_json(p, {})
        s=self.root/'s.json'; s.symlink_to(p)
        with self.assertRaises(OSError): m.read_private_json(s)
    def test_relative_path_rejected(self):
        with self.assertRaises(m.SetupError): m.absolute_path('relative.json')
    def test_unapproved_authorization_stops_before_sdk(self):
        with self.assertRaisesRegex(m.SetupError, 'APPROVAL'):
            m.authorize(SimpleNamespace(ack_drive_readonly=False))
    def test_unapproved_create_stops_before_sdk(self):
        with self.assertRaisesRegex(m.SetupError, 'APPROVAL'):
            m.create_folder(SimpleNamespace(confirm_create=False))
    def test_client_file_cannot_substitute_user_token(self):
        p=self.root/'x.json'; m.write_json(p, {'installed':{}})
        with self.assertRaises(m.SetupError): m.credential_info(p)
    def test_scope_check_and_no_cloud_call(self):
        p=self.root/'x.json'; m.write_json(p, {'client_id':'synthetic', 'client_secret':'synthetic',
          'refresh_token':'synthetic-not-a-token', 'scopes':list(m.SCOPES)})
        with patch.object(m, 'credentials', side_effect=AssertionError('no network')):
            result=m.check(SimpleNamespace(credentials=str(p),folder_id=None))
        self.assertTrue(result['scope_record_contains_required'])
        self.assertFalse(result['cloud_access_tested'])
    def test_bad_token_endpoint_rejected(self):
        p=self.root/'x.json'; m.write_json(p, {'client_id':'test','client_secret':'test',
          'refresh_token':'synthetic','scopes':list(m.SCOPES),'token_uri':'https://example.invalid/token'})
        with self.assertRaisesRegex(m.SetupError, 'ENDPOINT'): m.credential_info(p)
    def test_existing_receipt_not_reused(self):
        p=self.root/'r.json'; m.write_json(p, {'status':'OUTCOME_UNKNOWN'})
        with self.assertRaisesRegex(m.SetupError, 'RECEIPT_EXISTS'):
            m.create_folder(SimpleNamespace(confirm_create=True,credentials=str(self.root/'no-token.json'),
              receipt=str(p),name='test'))


# Keep the public helper tests runnable without any optional Google package.
import contextlib
import io
import logging
import sys
import types
import urllib.error
from unittest.mock import Mock


class FlowAndReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.client = self.root / 'client.json'
        self.output = self.root / 'authorized.json'
        self.receipt = self.root / 'receipt.json'
        self.client_value = {'installed': {'client_id': 'synthetic-client',
            'client_secret': 'synthetic-secret', 'auth_uri': m.AUTH_ENDPOINT,
            'token_uri': m.TOKEN_ENDPOINT, 'project_id': 'unused-project',
            'redirect_uris': ['http://localhost']}, 'extra': 'unused'}
        m.write_json(self.client, self.client_value)
        self.credential_value = {'client_id': 'synthetic-client', 'client_secret': 'synthetic-secret',
            'refresh_token': 'synthetic-refresh', 'token': 'synthetic-access',
            'token_uri': m.TOKEN_ENDPOINT, 'scopes': list(m.SCOPES)}
        self.creds = SimpleNamespace(refresh_token='synthetic-refresh', granted_scopes=list(m.SCOPES),
            token='synthetic-access', to_json=lambda: json.dumps(self.credential_value))
        self.flow = Mock(); self.flow.run_local_server.return_value = self.creds
        self.flow_class = Mock(); self.flow_class.from_client_config.return_value = self.flow
        module = types.ModuleType('google_auth_oauthlib.flow'); module.InstalledAppFlow = self.flow_class
        self.modules = patch.dict(sys.modules, {'google_auth_oauthlib': types.ModuleType('google_auth_oauthlib'),
                                               'google_auth_oauthlib.flow': module})
        self.modules.start(); self.addCleanup(self.modules.stop)
        self.tty = patch.object(m.sys.stdin, 'isatty', return_value=True)
        self.tty.start(); self.addCleanup(self.tty.stop)
        self.net = patch('socket.socket', side_effect=AssertionError('offline test forbids network'))
        self.net.start(); self.addCleanup(self.net.stop)

    def auth_args(self):
        return SimpleNamespace(ack_drive_readonly=True, client=str(self.client), output=str(self.output))

    def folder_args(self):
        return SimpleNamespace(confirm_create=True, credentials=str(self.output),
                               receipt=str(self.receipt), name='Synthetic transport')

    def rewrite_client(self, value):
        self.client.write_text(json.dumps(value))

    def test_authorize_success_exact_sdk_contract_and_private_output(self):
        stdout = io.StringIO()
        def run(*, host, port, authorization_prompt_message='DEFAULT {url}',
                success_message, open_browser, timeout_seconds, **kwargs):
            self.assertEqual((host, port, open_browser, timeout_seconds), ('127.0.0.1', 0, True, 300))
            self.assertEqual(kwargs, {'access_type': 'offline', 'prompt': 'consent'})
            self.assertNotIn('{url}', authorization_prompt_message)
            print(authorization_prompt_message.format(url='https://accounts.google.com/?state=synthetic-state'))
            logging.getLogger('google_auth_oauthlib.flow').critical('synthetic-refresh synthetic-state')
            return self.creds
        self.flow.run_local_server.side_effect = run
        before = self.client.read_bytes()
        with contextlib.redirect_stdout(stdout): result = m.authorize(self.auth_args())
        self.flow_class.from_client_config.assert_called_once_with(
            {'installed': {k: self.client_value['installed'][k] for k in
                ('client_id', 'client_secret', 'auth_uri', 'token_uri')}},
            scopes=list(m.SCOPES), autogenerate_code_verifier=True)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(m.read_private_json(self.output), self.credential_value)
        self.assertEqual(before, self.client.read_bytes())
        self.assertEqual(result['status'], 'OAUTH_FILE_SAVED')
        for secret in ('synthetic-refresh', 'synthetic-state', 'synthetic-secret', 'https://accounts'):
            self.assertNotIn(secret, stdout.getvalue() + json.dumps(result))

    def test_dual_config_rejected_before_sdk_or_browser(self):
        self.client_value['web'] = {'auth_uri': 'https://example.invalid/steal'}
        self.rewrite_client(self.client_value)
        with self.assertRaisesRegex(m.SetupError, 'DESKTOP_CLIENT_REQUIRED'): m.authorize(self.auth_args())
        self.flow_class.from_client_config.assert_not_called(); self.assertFalse(self.output.exists())

    def test_web_only_rejected_before_sdk(self):
        self.rewrite_client({'web': self.client_value['installed']})
        with self.assertRaisesRegex(m.SetupError, 'DESKTOP_CLIENT_REQUIRED'): m.authorize(self.auth_args())
        self.flow_class.from_client_config.assert_not_called()

    def test_null_web_key_still_rejected(self):
        self.client_value['web'] = None; self.rewrite_client(self.client_value)
        with self.assertRaisesRegex(m.SetupError, 'DESKTOP_CLIENT_REQUIRED'): m.authorize(self.auth_args())
        self.flow_class.from_client_config.assert_not_called()

    def test_bad_auth_endpoint_rejected_before_sdk(self):
        self.client_value['installed']['auth_uri'] = 'https://example.invalid/auth'
        self.rewrite_client(self.client_value)
        with self.assertRaisesRegex(m.SetupError, 'OAUTH_ENDPOINTS'): m.authorize(self.auth_args())
        self.flow_class.from_client_config.assert_not_called()

    def test_bad_token_endpoint_rejected_before_sdk(self):
        self.client_value['installed']['token_uri'] = 'https://example.invalid/token'
        self.rewrite_client(self.client_value)
        with self.assertRaisesRegex(m.SetupError, 'OAUTH_ENDPOINTS'): m.authorize(self.auth_args())
        self.flow_class.from_client_config.assert_not_called()

    def test_missing_client_secret_rejected_before_sdk(self):
        del self.client_value['installed']['client_secret']; self.rewrite_client(self.client_value)
        with self.assertRaisesRegex(m.SetupError, 'CLIENT_FIELDS_MISSING'): m.authorize(self.auth_args())
        self.flow_class.from_client_config.assert_not_called()

    def test_noninteractive_stops_before_sdk(self):
        with patch.object(m.sys.stdin, 'isatty', return_value=False):
            with self.assertRaisesRegex(m.SetupError, 'INTERACTIVE'): m.authorize(self.auth_args())
        self.flow_class.from_client_config.assert_not_called()

    def test_existing_output_is_unchanged_without_consent(self):
        m.write_json(self.output, {'preserve': True}); before = self.output.read_bytes()
        with self.assertRaisesRegex(m.SetupError, 'OUTPUT_EXISTS'): m.authorize(self.auth_args())
        self.flow_class.from_client_config.assert_not_called(); self.assertEqual(before, self.output.read_bytes())

    def test_missing_refresh_token_never_writes(self):
        self.creds.refresh_token = None
        with self.assertRaisesRegex(m.SetupError, 'REFRESH_TOKEN_MISSING'): m.authorize(self.auth_args())
        self.assertFalse(self.output.exists())

    def test_denied_scope_never_writes(self):
        self.creds.granted_scopes = [m.SCOPES[0]]
        with self.assertRaisesRegex(m.SetupError, 'SCOPES_NOT_GRANTED'): m.authorize(self.auth_args())
        self.assertFalse(self.output.exists())

    def test_missing_scope_record_never_writes(self):
        self.credential_value['scopes'] = [m.SCOPES[0]]
        with self.assertRaisesRegex(m.SetupError, 'REAUTHORIZE'): m.authorize(self.auth_args())
        self.assertFalse(self.output.exists())

    def test_missing_serialized_refresh_token_never_writes(self):
        del self.credential_value['refresh_token']
        with self.assertRaisesRegex(m.SetupError, 'CREDENTIAL_FIELDS'): m.authorize(self.auth_args())
        self.assertFalse(self.output.exists())

    def test_output_created_during_consent_is_not_overwritten(self):
        def consent(**kwargs):
            m.write_json(self.output, {'preserve': True}); return self.creds
        self.flow.run_local_server.side_effect = consent
        with self.assertRaises(FileExistsError): m.authorize(self.auth_args())
        self.assertEqual(m.read_private_json(self.output), {'preserve': True})

    def test_private_parent_required_for_input(self):
        other = self.root / 'public'; other.mkdir(mode=0o755)
        path = other / 'token.json'; path.write_text('{}'); path.chmod(0o600)
        with self.assertRaisesRegex(m.SetupError, '0700_PARENT'): m.read_private_json(path)

    def test_private_parent_required_before_consent(self):
        self.root.chmod(0o750)
        try:
            with self.assertRaisesRegex(m.SetupError, '0700_PARENT'): m.authorize(self.auth_args())
            self.flow_class.from_client_config.assert_not_called()
        finally: self.root.chmod(0o700)

    def test_sdk_exception_redacted_and_logging_restored(self):
        original = logging.root.manager.disable
        self.flow.run_local_server.side_effect = RuntimeError('https://accounts.google.com?state=synthetic-secret')
        output = io.StringIO()
        with patch.object(m, 'main', side_effect=lambda: m.authorize(self.auth_args())), contextlib.redirect_stdout(output):
            self.assertEqual(m.cli(), 1)
        self.assertEqual(logging.root.manager.disable, original)
        self.assertEqual(json.loads(output.getvalue())['error'], 'RuntimeError')
        self.assertNotIn('synthetic-secret', output.getvalue())
        self.assertNotIn('https://accounts', output.getvalue())

    def test_folder_success_receipt_and_one_attempt(self):
        def create(creds, name):
            self.assertEqual(m.read_private_json(self.receipt)['status'], 'CREATE_INTENT')
            self.assertEqual(creds, self.creds); self.assertEqual(name, 'Synthetic transport')
            return {'id': 'synthetic-folder'}
        with patch.object(m, 'credentials', return_value=self.creds), patch.object(m, 'create_folder_once', side_effect=create) as create:
            result = m.create_folder(self.folder_args())
        self.assertEqual(create.call_count, 1)
        self.assertEqual(m.read_private_json(self.receipt), {'status':'CREATED', 'name':'Synthetic transport', 'folder_id':'synthetic-folder'})
        self.assertEqual(self.receipt.stat().st_mode & 0o777, 0o600)
        self.assertTrue(result['receipt_saved']); self.assertFalse(result['sharing_changed'])

    def test_folder_unknown_stops_and_receipt_blocks_rerun(self):
        with patch.object(m, 'credentials', return_value=self.creds), patch.object(m, 'create_folder_once', side_effect=OSError('synthetic-secret')) as create:
            with self.assertRaisesRegex(m.SetupError, 'OUTCOME_UNKNOWN'): m.create_folder(self.folder_args())
            with self.assertRaisesRegex(m.SetupError, 'RECEIPT_EXISTS'): m.create_folder(self.folder_args())
        self.assertEqual(create.call_count, 1)
        self.assertEqual(m.read_private_json(self.receipt)['status'], 'OUTCOME_UNKNOWN')
        self.assertNotIn('synthetic-secret', self.receipt.read_text())

    def test_receipt_reservation_failure_prevents_post(self):
        with patch.object(m, 'credentials', return_value=self.creds), patch.object(m, 'write_json', side_effect=OSError('disk failed')), patch.object(m, 'create_folder_once') as create:
            with self.assertRaises(OSError): m.create_folder(self.folder_args())
        create.assert_not_called()

    def test_receipt_finalization_failure_never_recreates(self):
        write = m.write_json
        def fail_final(path, value, **kw):
            if kw.get('replace'): raise OSError('disk failed')
            return write(path, value, **kw)
        with patch.object(m, 'credentials', return_value=self.creds), patch.object(m, 'write_json', side_effect=fail_final), patch.object(m, 'create_folder_once', return_value={'id':'synthetic-folder'}) as create:
            with self.assertRaisesRegex(m.SetupError, 'OUTCOME_UNKNOWN'): m.create_folder(self.folder_args())
            with self.assertRaisesRegex(m.SetupError, 'RECEIPT_EXISTS'): m.create_folder(self.folder_args())
        self.assertEqual(create.call_count, 1)
        self.assertEqual(m.read_private_json(self.receipt)['status'], 'CREATE_INTENT')

    def test_raw_create_exact_request_and_bounded_reply(self):
        response = Mock(); response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({'id':'synthetic-folder','name':'Synthetic transport',
            'mimeType':'application/vnd.google-apps.folder'}).encode()
        opener = Mock(); opener.open.return_value = response
        with patch.object(m.urllib.request, 'build_opener', return_value=opener) as build:
            result = m.create_folder_once(self.creds, 'Synthetic transport')
        self.assertEqual(result['id'], 'synthetic-folder'); self.assertEqual(opener.open.call_count, 1)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.full_url, 'https://www.googleapis.com/drive/v3/files?fields=id%2Cname%2CmimeType')
        self.assertEqual(json.loads(request.data), {'name':'Synthetic transport','mimeType':'application/vnd.google-apps.folder'})
        self.assertEqual(opener.open.call_args.kwargs, {'timeout':20})
        response.read.assert_called_once_with(m.MAX_CONFIG_BYTES + 1)
        self.assertIsNone(build.call_args.args[0]().redirect_request(None,None,302,'redirect',{},'https://example.invalid'))

    def test_raw_create_connection_failure_not_replayed(self):
        opener = Mock(); opener.open.side_effect = OSError('lost reply')
        with patch.object(m.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(OSError): m.create_folder_once(self.creds, 'Synthetic transport')
        self.assertEqual(opener.open.call_count, 1)

    def test_raw_create_401_not_replayed(self):
        opener = Mock(); opener.open.side_effect = urllib.error.HTTPError('synthetic',401,'Unauthorized',{},None)
        with patch.object(m.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(urllib.error.HTTPError): m.create_folder_once(self.creds, 'Synthetic transport')
        self.assertEqual(opener.open.call_count, 1)

    def test_raw_create_unexpected_metadata_rejected(self):
        response = Mock(); response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"id":"synthetic-folder","name":"unexpected","mimeType":"application/vnd.google-apps.folder"}'
        opener = Mock(); opener.open.return_value = response
        with patch.object(m.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(m.SetupError, 'UNVERIFIED'): m.create_folder_once(self.creds, 'Synthetic transport')
        self.assertEqual(opener.open.call_count, 1)

    def test_check_folder_read_shape_and_scope_of_result(self):
        m.write_json(self.output, self.credential_value)
        service = Mock(); service.files.return_value.get.return_value.execute.return_value = {
            'id':'synthetic-folder','trashed':False,'mimeType':'application/vnd.google-apps.folder'}
        with patch.object(m, 'credentials', return_value=self.creds), patch.object(m, 'sdk_service', return_value=service):
            result = m.check(SimpleNamespace(credentials=str(self.output),folder_id='synthetic-folder'))
        service.files.return_value.get.assert_called_once_with(fileId='synthetic-folder', supportsAllDrives=True, fields='id,mimeType,trashed')
        service.files.return_value.get.return_value.execute.assert_called_once_with(num_retries=0)
        self.assertTrue(result['exact_folder_metadata_readable']); self.assertFalse(result['reverse_connector_probe_tested'])

    def test_helper_import_does_not_execute_authorization(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('isolated_helper', m.__file__)
        isolated = importlib.util.module_from_spec(spec); spec.loader.exec_module(isolated)
        self.flow_class.from_client_config.assert_not_called()


if __name__ == "__main__": unittest.main()
