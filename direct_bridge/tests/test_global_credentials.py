"""Synthetic-only local credential tests. Never reads the user's home or keys."""
import contextlib
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import global_credentials as credentials

KEY = 'synthetic-control-plane-key-not-live'
BEARER = 'synthetic-http-bearer-not-live'
VALUES = {credentials.KEY_NAME: KEY, credentials.BEARER_NAME: BEARER}


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='direct-credentials-fixture-')
        self.state = Path(self.temp.name).resolve() / 'private state'
        self.output = io.StringIO()
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(contextlib.redirect_stderr(self.output))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def file(self, name, text):
        self.state.mkdir(mode=0o700, exist_ok=True)
        path = self.state / name
        path.write_text(text, encoding='utf-8')
        path.chmod(0o600)
        return path

    def saved(self, values=VALUES):
        return self.file(credentials.ENV_NAME, credentials._serialize(values).decode())

    def interactive(self):
        self.stack.enter_context(patch.object(credentials.sys.stdin, 'isatty', return_value=True))
        self.stack.enter_context(patch.object(credentials.sys.stderr, 'isatty', return_value=True))

    def assert_no_leak(self, *values):
        for value in (*VALUES.values(), *values):
            self.assertNotIn(value, self.output.getvalue())

    def test_load_from_environment_has_no_filesystem_side_effect(self):
        info = credentials.load_credentials(self.state, VALUES)
        self.assertEqual(info.local_bearer, BEARER)
        self.assertEqual(info.control_plane_api_key, KEY)
        self.assertFalse(self.state.exists())
        self.assertEqual(repr(info), 'Credentials(<redacted>)')
        self.assert_no_leak()

    def test_load_missing_is_noninteractive_and_redacted(self):
        with patch.object(credentials.getpass, 'getpass') as get, patch('builtins.input') as ask:
            with self.assertRaises(credentials.CredentialError) as caught:
                credentials.load_credentials(self.state, {})
            get.assert_not_called()
            ask.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assertIn('Missing', str(caught.exception))
        self.assert_no_leak()

    def test_reuse_saved_key_and_exact_existing_http_bearer(self):
        envfile = self.saved({credentials.KEY_NAME: KEY})
        bearerfile = self.file(credentials.BEARER_FILE, BEARER + '\n')
        before = (envfile.read_bytes(), envfile.stat().st_mtime_ns,
                  bearerfile.read_bytes(), bearerfile.stat().st_mtime_ns)
        with patch.object(credentials.getpass, 'getpass') as get, patch('builtins.input') as ask:
            info = credentials.prepare_credentials(self.state, {})
            get.assert_not_called()
            ask.assert_not_called()
        self.assertEqual(info.sources[credentials.BEARER_NAME], 'http-bearer')
        self.assertEqual(before, (envfile.read_bytes(), envfile.stat().st_mtime_ns,
                                 bearerfile.read_bytes(), bearerfile.stat().st_mtime_ns))
        self.assert_no_leak()

    def test_environment_takes_precedence_over_saved_data(self):
        self.saved()
        override = 'synthetic-override-bearer-only'
        info = credentials.load_credentials(self.state, {credentials.BEARER_NAME: override})
        self.assertEqual(info.local_bearer, override)
        self.assertEqual(info.control_plane_api_key, KEY)
        self.assert_no_leak(override)

    def test_invalid_explicit_env_fails_instead_of_falling_back(self):
        self.saved()
        for bad in ('', 'short', BEARER + '\n', 'é' * 20):
            with self.subTest(value_type='invalid'), self.assertRaises(credentials.CredentialError) as caught:
                credentials.load_credentials(self.state, {credentials.BEARER_NAME: bad})
            self.assertNotIn(BEARER, str(caught.exception))

    def test_minimal_environments_separate_tunnel_key_before_children(self):
        base = {**VALUES, 'PATH': '/fixture/bin', 'HOME': '/fixture/home', 'CODEX_HOME': '/fixture/codex',
                'OPENAI_API_KEY': 'unrelated-model-secret', 'PYTHONPATH': '/untrusted',
                'BASH_ENV': '/untrusted', 'PYTHONSTARTUP': '/untrusted'}
        info = credentials.load_credentials(self.state, base)
        self.assertEqual(info.tunnel_env()[credentials.KEY_NAME], KEY)
        for child in (info.bridge_env(), info.codex_env()):
            self.assertNotIn(credentials.KEY_NAME, child)
            self.assertEqual(child[credentials.BEARER_NAME], BEARER)
        for child in (info.tunnel_env(), info.bridge_env(), info.codex_env()):
            self.assertEqual(child['PATH'], '/fixture/bin')
            self.assertEqual(child['CODEX_HOME'], '/fixture/codex')
            self.assertEqual(child['PYTHONNOUSERSITE'], '1')
            for denied in ('OPENAI_API_KEY', 'PYTHONPATH', 'BASH_ENV', 'PYTHONSTARTUP'):
                self.assertNotIn(denied, child)
        self.assertEqual(base[credentials.KEY_NAME], KEY)
        self.assert_no_leak()

    def test_data_parser_never_executes_or_interpolates_shell_syntax(self):
        marker = Path(self.temp.name) / 'must-not-exist'
        value = '$(touch${IFS}' + str(marker) + ')`whoami`$HOME'
        text = credentials._serialize({credentials.KEY_NAME: value, credentials.BEARER_NAME: BEARER}).decode()
        parsed = credentials.parse_private_env(text)
        self.assertEqual(parsed[credentials.KEY_NAME], value)
        self.assertFalse(marker.exists())
        self.assertEqual(credentials.parse_private_env(credentials.KEY_NAME + '=$HOME')[credentials.KEY_NAME], '$HOME')
        self.assert_no_leak(value)

    def test_parser_rejects_unknown_fields_duplicate_export_or_malformed_values(self):
        for text in ('OTHER="' + KEY + '"', 'export ' + credentials.KEY_NAME + '=' + KEY,
                     credentials.KEY_NAME + '=' + KEY + '\n' + credentials.KEY_NAME + '=' + KEY,
                     credentials.KEY_NAME + '="' + KEY, credentials.BEARER_NAME + '="short"',
                     credentials.KEY_NAME + '="embedded\\n' + KEY + '"',
                     credentials.KEY_NAME + '=value with whitespace', '\x00' + KEY):
            with self.subTest(case='malformed'), self.assertRaises(credentials.CredentialError) as caught:
                credentials.parse_private_env(text)
            self.assertNotIn(KEY, str(caught.exception))
        self.assert_no_leak()

    def test_first_use_requests_only_missing_key_then_explicit_save(self):
        self.interactive()
        bearerfile = self.file(credentials.BEARER_FILE, BEARER)
        before = bearerfile.read_bytes()
        with patch.object(credentials.getpass, 'getpass', return_value=KEY) as get, \
             patch('builtins.input', return_value='SAVE'):
            info = credentials.prepare_credentials(self.state, {})
        self.assertEqual(get.call_count, 1)
        self.assertIn(credentials.KEY_NAME, get.call_args.args[0])
        self.assertEqual(info.local_bearer, BEARER)
        saved = self.state / credentials.ENV_NAME
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(credentials.parse_private_env(saved.read_text()), {credentials.KEY_NAME: KEY})
        self.assertEqual(bearerfile.read_bytes(), before)
        self.assertIn(str(saved), self.output.getvalue())
        self.assertIn('Create a new file', self.output.getvalue())
        self.assert_no_leak()

    def test_complete_environment_save_creates_only_approved_private_file(self):
        self.interactive()
        with patch('builtins.input', return_value='SAVE'), patch.object(credentials.getpass, 'getpass') as get:
            credentials.prepare_credentials(self.state, VALUES)
            get.assert_not_called()
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), [credentials.ENV_NAME])
        self.assertEqual(credentials.load_credentials(self.state, {}).local_bearer, BEARER)
        self.assert_no_leak()

    def test_declining_save_keeps_values_session_only_without_mutation(self):
        self.interactive()
        with patch('builtins.input', return_value=''):
            info = credentials.prepare_credentials(self.state, VALUES)
        self.assertFalse(self.state.exists())
        self.assertEqual(info.tunnel_env()[credentials.KEY_NAME], KEY)
        self.assert_no_leak()

    def test_existing_file_replace_requires_separate_exact_path_save(self):
        path = self.saved()
        before = path.read_bytes()
        changed = {**VALUES, credentials.KEY_NAME: 'synthetic-new-tunnel-value'}
        self.interactive()
        with patch('builtins.input', return_value=''):
            credentials.prepare_credentials(self.state, changed)
        self.assertEqual(path.read_bytes(), before)
        self.assertIn('Replace the reviewed existing file', self.output.getvalue())
        with patch('builtins.input', return_value='SAVE'):
            credentials.prepare_credentials(self.state, changed)
        self.assertEqual(credentials.parse_private_env(path.read_text()), changed)
        self.assert_no_leak(changed[credentials.KEY_NAME])

    def test_changed_file_after_review_is_not_overwritten(self):
        path = self.saved()
        competing = {**VALUES, credentials.KEY_NAME: 'synthetic-competing-change'}
        self.interactive()
        def approve(prompt):
            path.write_bytes(credentials._serialize(competing))
            return 'SAVE'
        with patch('builtins.input', side_effect=approve):
            with self.assertRaisesRegex(credentials.CredentialError, 'changed after review'):
                credentials.prepare_credentials(self.state, {**VALUES, credentials.KEY_NAME: 'synthetic-requested-change'})
        self.assertEqual(credentials.parse_private_env(path.read_text()), competing)
        self.assertFalse((self.state / '.credentials.lock').exists())
        self.assertEqual(len(list(self.state.iterdir())), 1)
        self.assert_no_leak(competing[credentials.KEY_NAME])

    def test_file_appearing_after_review_is_not_overwritten(self):
        self.interactive()
        def approve(prompt):
            self.saved({credentials.KEY_NAME: 'synthetic-racing-create'})
            return 'SAVE'
        with patch('builtins.input', side_effect=approve):
            with self.assertRaisesRegex(credentials.CredentialError, 'changed after review'):
                credentials.prepare_credentials(self.state, VALUES)
        self.assertEqual(credentials.parse_private_env((self.state / credentials.ENV_NAME).read_text()),
                         {credentials.KEY_NAME: 'synthetic-racing-create'})

    def test_interrupted_atomic_save_preserves_original_and_cleans_temp(self):
        path = self.saved()
        before = path.read_bytes()
        self.interactive()
        with patch('builtins.input', return_value='SAVE'), patch.object(credentials.os, 'replace', side_effect=OSError('private-error-' + KEY)):
            with self.assertRaises(credentials.CredentialError) as caught:
                credentials.prepare_credentials(self.state, {**VALUES, credentials.KEY_NAME: 'synthetic-changed-key'})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), [credentials.ENV_NAME])
        self.assertNotIn(KEY, str(caught.exception))
        self.assert_no_leak()

    def test_symlink_files_and_symlink_directories_are_refused(self):
        actual = self.saved()
        target = actual.with_name('other-file')
        actual.rename(target)
        actual.symlink_to(target)
        with self.assertRaises(credentials.CredentialError):
            credentials.load_credentials(self.state, VALUES)
        actual.unlink()
        alias = self.state.with_name('alias')
        alias.symlink_to(self.state, target_is_directory=True)
        with self.assertRaises(credentials.CredentialError):
            credentials.load_credentials(alias, VALUES)

    def test_hardlinked_file_refused(self):
        path = self.saved()
        os.link(path, path.with_name('hardlink'))
        with self.assertRaises(credentials.CredentialError):
            credentials.load_credentials(self.state, {})

    def test_nonprivate_directory_file_and_fifo_refused(self):
        path = self.saved()
        path.chmod(0o644)
        with self.assertRaises(credentials.CredentialError):
            credentials.load_credentials(self.state, {})
        path.chmod(0o600)
        self.state.chmod(0o755)
        with self.assertRaises(credentials.CredentialError):
            credentials.load_credentials(self.state, {})
        self.state.chmod(0o700)
        path.unlink()
        os.mkfifo(path, 0o600)
        with self.assertRaises(credentials.CredentialError):
            credentials.load_credentials(self.state, {})

    def test_oversized_and_invalid_utf8_are_redacted(self):
        path = self.file(credentials.ENV_NAME, 'x' * (credentials.MAX_BYTES + 1))
        with self.assertRaises(credentials.CredentialError):
            credentials.load_credentials(self.state, {})
        path.write_bytes(b'\xff' + KEY.encode())
        with self.assertRaises(credentials.CredentialError) as caught:
            credentials.load_credentials(self.state, {})
        self.assertNotIn(KEY, str(caught.exception))

    def test_source_checkout_parent_components_and_other_git_repo_refused(self):
        for path in (ROOT / 'private', ROOT.parent / 'private', self.state / '../bad'):
            with self.assertRaises(credentials.CredentialError):
                credentials.load_credentials(path, VALUES)
        repo = Path(self.temp.name) / 'checkout'
        repo.mkdir()
        (repo / '.git').write_text('gitdir: /fixture/external')
        with self.assertRaises(credentials.CredentialError):
            credentials.load_credentials(repo / 'private', VALUES)

    def test_noninteractive_and_getpass_fallback_cannot_accept_secrets(self):
        with patch.object(credentials.sys.stdin, 'isatty', return_value=False), \
             patch.object(credentials.getpass, 'getpass') as get:
            with self.assertRaises(credentials.CredentialError):
                credentials.prepare_credentials(self.state, {})
            get.assert_not_called()
        self.interactive()
        with patch.object(credentials.getpass, 'getpass', side_effect=credentials.getpass.GetPassWarning('fixture')):
            with self.assertRaises(credentials.CredentialError):
                credentials.prepare_credentials(self.state, {})
        self.assertFalse(self.state.exists())

    def test_read_only_noninteractive_path_does_not_prompt_to_save(self):
        with patch('builtins.input') as ask, patch.object(credentials.getpass, 'getpass') as get:
            info = credentials.prepare_credentials(self.state, VALUES, interactive=False)
            ask.assert_not_called()
            get.assert_not_called()
        self.assertEqual(info.local_bearer, BEARER)
        self.assertFalse(self.state.exists())

    def test_clear_requires_confirmation_and_preserves_bearer_by_default(self):
        private = self.saved({credentials.KEY_NAME: KEY})
        bearer = self.file(credentials.BEARER_FILE, BEARER)
        self.interactive()
        with patch('builtins.input', return_value=''):
            self.assertFalse(credentials.forget_saved_credentials(self.state))
        self.assertTrue(private.exists())
        with patch('builtins.input', return_value='CLEAR'):
            self.assertTrue(credentials.forget_saved_credentials(self.state))
        self.assertFalse(private.exists())
        self.assertTrue(bearer.exists())
        self.assertIn('NOT revoked', self.output.getvalue())
        self.assert_no_leak()

    def test_separate_explicit_clear_can_include_only_exact_bearer(self):
        self.saved()
        bearer = self.file(credentials.BEARER_FILE, BEARER)
        other = self.file('unrelated.env', 'unrelated fixture')
        self.interactive()
        with patch('builtins.input', return_value='CLEAR'):
            self.assertTrue(credentials.forget_saved_credentials(self.state, include_http_bearer=True))
        self.assertFalse(bearer.exists())
        self.assertTrue(other.exists())

    def test_real_child_environment_has_bearer_but_no_tunnel_or_model_key(self):
        info = credentials.load_credentials(self.state, {**VALUES, 'OPENAI_API_KEY': 'unrelated-secret'})
        result = subprocess.run([sys.executable, '-c',
            'import os; assert "CONTROL_PLANE_API_KEY" not in os.environ; '
            'assert "OPENAI_API_KEY" not in os.environ; '
            'assert len(os.environ["DOTS_BRIDGE_HTTP_BEARER"]) >= 16'],
            env=info.bridge_env(), capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'')

    def test_same_value_cannot_be_used_for_tunnel_and_http_before_save(self):
        self.interactive()
        values = {credentials.KEY_NAME: BEARER, credentials.BEARER_NAME: BEARER}
        with patch('builtins.input') as ask:
            with self.assertRaisesRegex(credentials.CredentialError, 'must be separate'):
                credentials.prepare_credentials(self.state, values)
            ask.assert_not_called()
        self.assertFalse(self.state.exists())
        with self.assertRaises(credentials.CredentialError):
            credentials.load_credentials(self.state, values)

    def test_eof_on_save_does_not_write(self):
        self.interactive()
        with patch('builtins.input', side_effect=EOFError):
            with self.assertRaises(credentials.CredentialError):
                credentials.prepare_credentials(self.state, VALUES)
        self.assertFalse(self.state.exists())
        self.assert_no_leak()

    def test_existing_lock_is_preserved_without_overwriting_credentials(self):
        path = self.saved()
        before = path.read_bytes()
        lock = self.file('.credentials.lock', 'fixture-lock')
        self.interactive()
        with patch('builtins.input', return_value='SAVE'):
            with self.assertRaisesRegex(credentials.CredentialError, 'Another credential'):
                credentials.prepare_credentials(self.state, {**VALUES, credentials.KEY_NAME: 'synthetic-replacement-key'})
        self.assertTrue(lock.exists())
        self.assertEqual(path.read_bytes(), before)

    def test_unknown_directory_owner_is_rejected(self):
        self.saved()
        with patch.object(credentials.os, 'getuid', return_value=os.getuid() + 100000):
            with self.assertRaisesRegex(credentials.CredentialError, 'owner'):
                credentials.load_credentials(self.state, VALUES)

    def test_malformed_unicode_parser_error_does_not_contain_input(self):
        with self.assertRaises(credentials.CredentialError) as caught:
            credentials.parse_private_env(credentials.KEY_NAME + '=' + KEY + '\ud800')
        self.assertNotIn(KEY, str(caught.exception))

    def test_cli_check_uses_only_supplied_fixture_path_and_safe_messages(self):
        self.saved()
        with patch.dict(os.environ, {}, clear=True):
            code = credentials.main(['check', '--state-dir', str(self.state)])
        self.assertEqual(code, 0)
        self.assertIn('values are hidden', self.output.getvalue())
        self.assert_no_leak()


if __name__ == '__main__':
    unittest.main()
