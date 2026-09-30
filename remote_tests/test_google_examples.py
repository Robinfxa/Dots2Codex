"""Offline tests for optional SDK factories/setup; no real tokens or API calls."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from remote_transport import deployment, ProtocolError
from remote_transport.control import initial_state, block_for
from examples import google_clients as clients
from examples.remote_setup import initialize_blank


class FakeRequest:
    def __init__(self, owner, response): self.owner, self.response = owner, response
    def execute(self, **kw):
        self.owner.calls.append(('execute', kw))
        if self.owner.error: raise self.owner.error
        return self.response


class FakeService:
    def __init__(self): self.calls = []; self.error = None
    def documents(self): return self
    def get(self, **kw): self.calls.append(('get', kw)); return FakeRequest(self, {'documentId': 'doc'})
    def batchUpdate(self, **kw): self.calls.append(('batch', kw)); return FakeRequest(self, {'documentId': 'doc', 'replies': []})


class BlankDocument:
    def __init__(self): self.text = '\n'; self.calls = []; self.unknown = False
    def get_document(self, document_id):
        return {'documentId': document_id, 'revisionId': 'revision', 'suggestionsViewMode': 'SUGGESTIONS_INLINE',
                'tabs': [{'tabProperties': {'tabId': 'tab'}, 'documentTab': {'body': {'content': [
                    {'sectionBreak': {}}, {'paragraph': {'elements': [{'textRun': {'content': self.text}}]}}
                ]}}}]}
    def batch_update_document(self, document_id, requests, write_control):
        self.calls.append((document_id, requests, write_control))
        self.text = requests[0]['insertText']['text'] + '\n'
        if self.unknown: raise TimeoutError('private provider message')
        return {'documentId': document_id, 'replies': [{}]}


class ExampleTests(unittest.TestCase):
    def test_import_has_no_google_dependency_or_io(self):
        script = "import sys; import examples.google_clients; assert not any(k.startswith('google.') for k in sys.modules)"
        subprocess.run([sys.executable, '-B', '-c', script], cwd=Path(__file__).resolve().parents[1], check=True)

    def test_missing_credential_path_rejected(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ProtocolError, 'explicit_absolute'):
            clients._credential_info()

    def test_relative_credential_path_rejected(self):
        with patch.dict(os.environ, {'DOTS_GOOGLE_AUTHORIZED_USER_FILE': 'relative.json'}), self.assertRaisesRegex(ProtocolError, 'explicit_absolute'):
            clients._credential_info()

    def info(self, changes=None, mode=0o600):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        path = Path(temp.name) / 'authorized-user.json'
        value = {'type': 'authorized_user', 'client_id': 'synthetic-client', 'client_secret': 'synthetic-placeholder',
                 'refresh_token': 'synthetic-placeholder'}
        value.update(changes or {}); path.write_text(json.dumps(value)); path.chmod(mode)
        return path, value

    def test_private_explicit_file_loaded_unchanged(self):
        path, value = self.info()
        with patch.dict(os.environ, {'DOTS_GOOGLE_AUTHORIZED_USER_FILE': str(path)}): self.assertEqual(clients._credential_info(), value)

    def test_world_readable_file_rejected_without_contents(self):
        path, _ = self.info(mode=0o644)
        with patch.dict(os.environ, {'DOTS_GOOGLE_AUTHORIZED_USER_FILE': str(path)}), self.assertRaisesRegex(ProtocolError, '^authorized_user_file_unreadable$'):
            clients._credential_info()

    def test_symlink_file_rejected(self):
        path, _ = self.info(); link = path.parent / 'link'; link.symlink_to(path)
        with patch.dict(os.environ, {'DOTS_GOOGLE_AUTHORIZED_USER_FILE': str(link)}), self.assertRaises(ProtocolError): clients._credential_info()

    def test_external_endpoint_rejected(self):
        path, _ = self.info({'token_uri': 'https://example.invalid/token'})
        with patch.dict(os.environ, {'DOTS_GOOGLE_AUTHORIZED_USER_FILE': str(path)}), self.assertRaisesRegex(ProtocolError, 'unexpected_token_endpoint'):
            clients._credential_info()

    def test_other_credential_type_rejected(self):
        path, _ = self.info({'type': 'external_account'})
        with patch.dict(os.environ, {'DOTS_GOOGLE_AUTHORIZED_USER_FILE': str(path)}), self.assertRaisesRegex(ProtocolError, 'authorized_user_credentials_required'):
            clients._credential_info()

    def test_other_universe_rejected(self):
        path, _ = self.info({'universe_domain': 'example.invalid'})
        with patch.dict(os.environ, {'DOTS_GOOGLE_AUTHORIZED_USER_FILE': str(path)}), self.assertRaisesRegex(ProtocolError, 'unexpected_google_universe'):
            clients._credential_info()

    def test_docs_get_requests_full_tabs_and_inline_suggestions(self):
        service = FakeService(); clients.DocsSDKClient(service).get_document('doc')
        self.assertEqual(service.calls, [('get', {'documentId': 'doc', 'includeTabsContent': True, 'suggestionsViewMode': 'SUGGESTIONS_INLINE'}), ('execute', {'num_retries': 0})])

    def test_docs_write_preserves_exact_revision_and_disables_retry(self):
        service = FakeService(); requests = [{'replaceAllText': {'synthetic': True}}]
        clients.DocsSDKClient(service).batch_update_document('doc', requests, {'requiredRevisionId': 'r'})
        self.assertEqual(service.calls, [('batch', {'documentId': 'doc', 'body': {'requests': requests, 'writeControl': {'requiredRevisionId': 'r'}}}), ('execute', {'num_retries': 0})])

    def test_target_revision_or_missing_revision_rejected(self):
        for wc in ({}, {'targetRevisionId': 'r'}, {'requiredRevisionId': ''}, {'requiredRevisionId': 'r', 'targetRevisionId': 'r'}):
            service = FakeService()
            with self.assertRaises(ProtocolError): clients.DocsSDKClient(service).batch_update_document('doc', [], wc)
            self.assertEqual(service.calls, [])

    def test_write_error_is_unknown_sanitized_and_not_retried(self):
        service = FakeService(); service.error = ValueError('400 private-secret stale revision')
        with self.assertRaisesRegex(ProtocolError, '^docs_write_outcome_unknown$'):
            clients.DocsSDKClient(service).batch_update_document('doc', [], {'requiredRevisionId': 'r'})
        self.assertEqual(len(service.calls), 2)

    def test_read_error_sanitized(self):
        service = FakeService(); service.error = ValueError('private doc text')
        with self.assertRaisesRegex(ProtocolError, '^docs_read_failed$'): clients.DocsSDKClient(service).get_document('doc')

    def test_blank_init_exact_bytes_and_revision(self):
        doc = BlankDocument(); pin = deployment('setup-session', 'synthetic-native')
        result = initialize_blank(doc, pin, 'doc', 'tab', 'control', 'writer')
        self.assertEqual(doc.text, block_for(initial_state(pin, 'control')))
        self.assertEqual(doc.calls[0][2], {'requiredRevisionId': 'revision'})
        self.assertEqual(doc.calls[0][1][0]['insertText']['location'], {'index': 1, 'tabId': 'tab'})
        self.assertFalse(doc.calls[0][1][0]['insertText']['text'].endswith('\n'))
        self.assertFalse(result['native_execution_authorized'])

    def test_init_nonblank_never_writes(self):
        doc = BlankDocument(); doc.text = 'existing contents\n'; pin = deployment('setup-session', 'synthetic-native')
        with self.assertRaisesRegex(ProtocolError, 'must_be_blank'): initialize_blank(doc, pin, 'doc', 'tab', 'control', 'writer')
        self.assertEqual(doc.calls, [])

    def test_init_unknown_write_cannot_be_repeated(self):
        doc = BlankDocument(); doc.unknown = True; pin = deployment('setup-session', 'synthetic-native')
        with self.assertRaises(TimeoutError): initialize_blank(doc, pin, 'doc', 'tab', 'control', 'writer')
        with self.assertRaisesRegex(ProtocolError, 'must_be_blank'): initialize_blank(doc, pin, 'doc', 'tab', 'control', 'writer')
        self.assertEqual(len(doc.calls), 1)

    def test_init_wrong_tab_never_writes(self):
        doc = BlankDocument(); pin = deployment('setup-session', 'synthetic-native')
        with self.assertRaises(ProtocolError): initialize_blank(doc, pin, 'doc', 'wrong-tab', 'control', 'writer')
        self.assertEqual(doc.calls, [])


if __name__ == '__main__': unittest.main()
