"""Drive-only CA selection and single-send fences; no external network."""
import builtins
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from dots_lite.google_ports import DriveHTTPClient, _drive_ssl_context
from dots_lite.protocol import ProtocolError


class DriveTLS(unittest.TestCase):
    def assert_verified(self, context):
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_certifi_selected_without_explicit_overrides(self):
        context = ssl.create_default_context()
        bundle = SimpleNamespace(where=mock.Mock(return_value='/installed/certifi/cacert.pem'))
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.dict(sys.modules, {'certifi': bundle}), \
             mock.patch('dots_lite.google_ports.ssl.create_default_context', return_value=context) as create:
            self.assertIs(_drive_ssl_context(), context)
        bundle.where.assert_called_once_with()
        create.assert_called_once_with(cafile='/installed/certifi/cacert.pem')
        self.assert_verified(context)

    def test_explicit_file_directory_or_both_exclude_certifi(self):
        for overrides in ({'SSL_CERT_FILE': '/explicit/ca.pem'},
                          {'SSL_CERT_DIR': '/explicit/ca-dir'},
                          {'SSL_CERT_FILE': '/explicit/ca.pem', 'SSL_CERT_DIR': '/explicit/ca-dir'}):
            with self.subTest(overrides=overrides):
                context = ssl.create_default_context()
                bundle = SimpleNamespace(where=mock.Mock(side_effect=AssertionError('must not use certifi')))
                with mock.patch.dict(os.environ, overrides, clear=True), \
                     mock.patch.dict(sys.modules, {'certifi': bundle}), \
                     mock.patch('dots_lite.google_ports.ssl.create_default_context', return_value=context) as create:
                    self.assertIs(_drive_ssl_context(), context)
                create.assert_called_once_with(cafile=overrides.get('SSL_CERT_FILE'),
                                               capath=overrides.get('SSL_CERT_DIR'))
                bundle.where.assert_not_called()
                self.assert_verified(context)

    def test_empty_explicit_override_never_selects_any_fallback(self):
        for overrides in ({'SSL_CERT_FILE': ''}, {'SSL_CERT_DIR': ''},
                          {'SSL_CERT_FILE': '', 'SSL_CERT_DIR': '/configured'},
                          {'SSL_CERT_FILE': '/configured', 'SSL_CERT_DIR': ''}):
            with self.subTest(overrides=overrides), mock.patch.dict(os.environ, overrides, clear=True), \
                 mock.patch('dots_lite.google_ports.ssl.create_default_context') as create, \
                 mock.patch('dots_lite.google_ports.http.client.HTTPSConnection') as connect:
                token = mock.Mock()
                with self.assertRaisesRegex(ProtocolError, '^drive_transport_outcome_unknown$'):
                    DriveHTTPClient(token).create_bytes('folder', 'name', b'{}')
                create.assert_not_called()
                token.assert_not_called()
                connect.assert_not_called()

    def test_only_absent_certifi_uses_verified_stdlib_default(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.dict(sys.modules, {'certifi': None}), \
             mock.patch('dots_lite.google_ports.ssl.create_default_context', wraps=ssl.create_default_context) as create:
            context = _drive_ssl_context()
        create.assert_called_once_with()
        self.assert_verified(context)

    def test_broken_certifi_import_does_not_fallback(self):
        original_import = builtins.__import__
        for failure in (ImportError('private import failure'),
                        ModuleNotFoundError('private missing dependency', name='certifi_dependency')):
            def importing(name, *args, **kwargs):
                if name == 'certifi':
                    raise failure
                return original_import(name, *args, **kwargs)
            with self.subTest(failure=type(failure).__name__), \
                 mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch('builtins.__import__', side_effect=importing), \
                 mock.patch('dots_lite.google_ports.ssl.create_default_context') as create:
                with self.assertRaisesRegex(ProtocolError, '^drive_transport_outcome_unknown$'):
                    _drive_ssl_context()
                create.assert_not_called()

    def test_broken_certifi_location_does_not_fallback(self):
        bundle = SimpleNamespace(where=mock.Mock(side_effect=OSError('private bundle path')))
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.dict(sys.modules, {'certifi': bundle}), \
             mock.patch('dots_lite.google_ports.ssl.create_default_context') as create:
            with self.assertRaisesRegex(ProtocolError, '^drive_transport_outcome_unknown$'):
                _drive_ssl_context()
            create.assert_not_called()

    def test_empty_or_invalid_certifi_location_does_not_select_defaults(self):
        for path in (None, '', 123):
            bundle = SimpleNamespace(where=mock.Mock(return_value=path))
            with self.subTest(path=path), mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch.dict(sys.modules, {'certifi': bundle}), \
                 mock.patch('dots_lite.google_ports.ssl.create_default_context') as create:
                with self.assertRaisesRegex(ProtocolError, '^drive_transport_outcome_unknown$'):
                    _drive_ssl_context()
                create.assert_not_called()

    def test_invalid_explicit_or_certifi_bundle_fails_before_token_or_post(self):
        with tempfile.TemporaryDirectory() as directory:
            malformed = Path(directory) / 'private-malformed.pem'
            malformed.write_text('not a CA bundle')
            for path in (str(Path(directory) / 'private-missing.pem'), str(malformed)):
                for explicit in (True, False):
                    overrides = {'SSL_CERT_FILE': path} if explicit else {}
                    bundle = SimpleNamespace(where=mock.Mock(return_value=path))
                    with self.subTest(path=Path(path).name, explicit=explicit), \
                         mock.patch.dict(os.environ, overrides, clear=True), \
                         mock.patch.dict(sys.modules, {'certifi': bundle}), \
                         mock.patch('dots_lite.google_ports.ssl.create_default_context', wraps=ssl.create_default_context) as create, \
                         mock.patch('dots_lite.google_ports.http.client.HTTPSConnection') as connect:
                        token = mock.Mock()
                        with self.assertRaisesRegex(ProtocolError, '^drive_transport_outcome_unknown$') as caught:
                            DriveHTTPClient(token).create_bytes('folder', 'name', b'{}')
                        self.assertNotIn(path, str(caught.exception))
                        self.assertEqual(create.call_count, 1)
                        token.assert_not_called()
                        connect.assert_not_called()
                        if explicit:
                            bundle.where.assert_not_called()

    def test_explicit_empty_or_missing_ca_directory_loads_no_other_roots(self):
        # OpenSSL loads hashed CA directories lazily; an empty/missing directory
        # must never silently import unrelated default roots.
        with tempfile.TemporaryDirectory() as directory:
            for path in (directory, str(Path(directory) / 'missing')):
                with self.subTest(path=path), \
                     mock.patch.dict(os.environ, {'SSL_CERT_DIR': path}, clear=True):
                    context = _drive_ssl_context()
                self.assert_verified(context)
                self.assertEqual(context.get_ca_certs(), [])

    def test_one_context_reused_without_extra_requests_or_write_retry(self):
        context = ssl.create_default_context()
        connections = []
        class Connection:
            def __init__(self, host, *, timeout, context):
                self.host, self.timeout, self.context = host, timeout, context
                self.sock = None
                self.connects = self.closes = 0
                self.requests = []
                connections.append(self)
            def connect(self): self.connects += 1
            def request(self, *args, **kwargs): self.requests.append((args, kwargs))
            def getresponse(self):
                return SimpleNamespace(status=200, read=lambda limit: b'{"id":"created","name":"name","parents":["folder"]}')
            def close(self): self.closes += 1
        token = mock.Mock(return_value='authorized-test-token')
        with mock.patch('dots_lite.google_ports._drive_ssl_context', return_value=context) as create, \
             mock.patch('dots_lite.google_ports.http.client.HTTPSConnection', Connection):
            port = DriveHTTPClient(token, timeout=7)
            self.assertEqual(port.create_document_once('folder', 'name'), 'created')
            self.assertEqual(port.get_metadata('created')['id'], 'created')
            self.assertEqual(port.create_bytes('folder', 'name', b'{}'), 'created')
        create.assert_called_once_with()
        self.assertEqual(token.call_count, 3)
        self.assertEqual(len(connections), 3)
        self.assertEqual([conn.requests[0][0][0] for conn in connections], ['POST', 'GET', 'POST'])
        for conn in connections:
            self.assertIs(conn.context, context)
            self.assert_verified(conn.context)
            self.assertEqual((conn.host, conn.timeout, conn.connects, conn.closes, conn.auto_open),
                             ('www.googleapis.com', 7, 1, 1, 0))
            self.assertEqual(len(conn.requests), 1)

    def test_tls_handshake_failure_is_unknown_without_post_or_retry(self):
        for failure in (ssl.SSLCertVerificationError('private certificate failure'),
                        TimeoutError('private connection timeout')):
            conn = mock.Mock()
            conn.connect.side_effect = failure
            token = mock.Mock(return_value='authorized-test-token')
            with self.subTest(failure=type(failure).__name__), \
                 mock.patch('dots_lite.google_ports.http.client.HTTPSConnection', return_value=conn) as connect:
                port = DriveHTTPClient(token)
                with self.assertRaisesRegex(ProtocolError, '^drive_transport_outcome_unknown$'):
                    port.create_bytes('folder', 'name', b'{}')
            self.assertEqual(connect.call_count, 1)
            self.assertEqual(token.call_count, 1)
            conn.connect.assert_called_once_with()
            conn.request.assert_not_called()
            conn.close.assert_called_once_with()

    def test_google_ports_import_remains_stdlib_only(self):
        program = (
            "import sys; import dots_lite.google_ports; "
            "assert not any(n == 'certifi' or n == 'requests' or n == 'google' "
            "or n.startswith('google.') for n in sys.modules)"
        )
        subprocess.run([sys.executable, '-B', '-S', '-c', program],
                       check=True, cwd=Path(__file__).resolve().parents[1])


if __name__ == '__main__':
    unittest.main()
