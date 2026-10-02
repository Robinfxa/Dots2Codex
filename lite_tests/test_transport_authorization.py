"""Offline transport-scope tests. All grants, secrets and paths are synthetic."""
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from dots_lite import authorization as auth, cli, protocol as p
from dots_lite.launcher import CONTRACT, Launcher, join_text
from dots_lite.private_io import private_dir, private_write, save
from dots_lite.ui import Cancelled
from lite_tests.test_core import grant, resource

KEY = '32' * 32


class AuthorizationBinding(unittest.TestCase):
    def setUp(self):
        self.grant = grant()
        self.join = auth.parse_join(join_text(self.grant, KEY))

    def test_exact_scope_roundtrip_is_secret_free(self):
        scope = auth.validate_join_authorization(self.join, self.grant)
        self.assertEqual(scope, auth.authorization_scope(self.grant))
        self.assertEqual(scope['grant'], self.grant)
        self.assertNotIn(KEY, p.canonical(scope).decode())
        self.assertNotIn('join_code', p.canonical(scope).decode())
        statement = scope['statement']
        for phrase in ('Read', 'Outbox', 'compare-and-swap (CAS)', 'replacing the control Doc body',
                       'Upload actual user-task result JSON', 'Google Drive folder: folder',
                       'native-task', 'authenticator', 'No unrelated Docs', 'deletion',
                       'sharing changes', 'scope expansion', 'Sensitive content',
                       'Platform approvals', 'not a platform approval'):
            self.assertIn(phrase, statement)
        self.assertEqual(self.join['join_code'], KEY)

    def test_scope_handles_the_existing_maximum_pair_count(self):
        g = copy.deepcopy(self.grant)
        g['allowed_pairs'] = [{'model': 'model-' + str(i) + '-' + 'x' * 240,
                               'reasoning_effort': 'xhigh'} for i in range(32)]
        self.assertEqual(auth.parse_join(join_text(g, KEY))['transport_authorization'],
                         auth.authorization_scope(g))

    def test_legacy_four_fields_fail_closed_without_inferred_authority(self):
        old = {k: self.join[k] for k in auth.JOIN_KEYS}
        with self.assertRaisesRegex(p.ProtocolError, '^fresh_transport_authorization_required$'):
            auth.parse_join(p.JOIN_MARKER + ' ' + p.canonical(old).decode())

    def test_missing_or_extra_authorization_fields_rejected(self):
        for mutate in (lambda j: j['transport_authorization'].pop('statement'),
                       lambda j: j.update(platform_approval_id='invented'),
                       lambda j: j['transport_authorization'].update(approved=True),
                       lambda j: j.update(transport_authorization=None)):
            with self.subTest(mutate=mutate):
                value = copy.deepcopy(self.join); mutate(value)
                with self.assertRaises(p.ProtocolError):
                    auth.parse_join(p.JOIN_MARKER + ' ' + p.canonical(value).decode())

    def test_altered_or_widened_statement_rejected(self):
        for text in ('I authorize all Drive files and any future action',
                     self.join['transport_authorization']['statement'].replace('No unrelated Docs', 'All unrelated Docs'),
                     self.join['transport_authorization']['statement'] + '\nPlatform approved: yes'):
            value = copy.deepcopy(self.join)
            value['transport_authorization']['statement'] = text
            with self.assertRaisesRegex(p.ProtocolError, 'transport_authorization_binding_mismatch'):
                auth.parse_join(p.JOIN_MARKER + ' ' + p.canonical(value).decode())

    def test_each_binding_must_match_authenticated_grant(self):
        changes = {'activation_id': 'other-activation', 'inbox_id': 'other-inbox',
                   'folder_id': 'other-folder', 'package_sha256': 'f' * 64,
                   'created_at': self.grant['created_at'] - 1,
                   'expires_at': self.grant['expires_at'] + 1,
                   'limits': {**self.grant['limits'], 'max_routes': 2, 'max_children': 2},
                   'allowed_pairs': [{'model': 'gpt-6-astra', 'reasoning_effort': 'high'}]}
        for field, replacement in changes.items():
            with self.subTest(field=field):
                altered = {**self.grant, field: replacement}
                claimed = auth.parse_join(join_text(altered, KEY))
                with self.assertRaises(p.ProtocolError):
                    auth.validate_join_authorization(claimed, self.grant)

    def test_sidecar_cannot_use_python_numeric_equality_for_different_grant_json(self):
        value = copy.deepcopy(self.join)
        value['transport_authorization']['grant']['created_at'] = float(self.grant['created_at'])
        with self.assertRaisesRegex(p.ProtocolError, 'transport_authorization_binding_mismatch'):
            auth.validate_join_authorization(value, self.grant)

    def test_scope_cannot_mutate_grant_or_returned_join(self):
        scope = auth.authorization_scope(self.grant)
        scope['grant']['limits']['max_routes'] = 1
        self.assertEqual(self.grant['limits']['max_routes'], 3)
        result = auth.validate_join_authorization(self.join, self.grant)
        result['grant']['limits']['max_routes'] = 2
        self.assertEqual(self.join['transport_authorization']['grant']['limits']['max_routes'], 3)

    def test_duplicate_keys_and_non_text_and_oversize_fail(self):
        for raw in (b'\xff', None, p.JOIN_MARKER + ' {"join_code":"x","join_code":"y"}',
                    b'x' * (auth.MAX_JOIN_BYTES + 1)):
            with self.subTest(raw=type(raw)):
                with self.assertRaises(p.ProtocolError): auth.parse_join(raw)

    def test_signed_doc_alone_does_not_supply_missing_join_consent(self):
        signed = p.make_inbox(self.grant, [], KEY, 'operation')
        legacy = {k: self.join[k] for k in auth.JOIN_KEYS}
        config = {'join': legacy, 'grant': None, 'package_sha256': self.grant['package_sha256']}
        with mock.patch.object(cli, 'package_hash', return_value=self.grant['package_sha256']):
            with self.assertRaises(p.ProtocolError):
                cli.inbox_from(resource(p.canonical(signed).decode() + '\n', document_id='inbox'), config)

    def test_inbox_authentication_precedes_scope_acceptance(self):
        signed = p.make_inbox(self.grant, [], KEY, 'operation')
        signed['mac'] = '0' * 64
        config = {'join': self.join, 'grant': None, 'package_sha256': self.grant['package_sha256']}
        with mock.patch.object(cli, 'package_hash', return_value=self.grant['package_sha256']):
            with self.assertRaisesRegex(p.ProtocolError, 'record_authentication_failed'):
                cli.inbox_from(resource(p.canonical(signed).decode() + '\n', document_id='inbox'), config)

    def test_missing_consent_or_owner_reference_has_no_init_side_effects(self):
        with tempfile.TemporaryDirectory() as folder:
            root = private_dir(Path(folder) / 'inputs', create=True)
            source = root / 'join.txt'
            for legacy, message_id in ((True, 'synthetic-owner-message'), (False, '')):
                with self.subTest(legacy=legacy):
                    value = {k: self.join[k] for k in auth.JOIN_KEYS} if legacy else self.join
                    private_write(source, (p.JOIN_MARKER + ' ' + p.canonical(value).decode()).encode())
                    state = Path(folder) / ('state-' + str(legacy))
                    args = SimpleNamespace(state_dir=str(state), join_file=str(source), actor_task_id='/root',
                                           authorization_message_id=message_id, available_child_slots=3)
                    with mock.patch.object(cli, 'package_hash', return_value=self.grant['package_sha256']):
                        with self.assertRaises(p.ProtocolError): cli.initialize(args)
                    self.assertFalse(state.exists())


class LauncherConsent(unittest.TestCase):
    def test_start_cancel_does_not_create_state_or_read_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ui = mock.Mock(); ui.confirm.return_value = False
            ports = mock.Mock(); ports.package.return_value = 'a' * 64
            launcher = Launcher(root=root, state=root / 'new-state', ui=ui, ports=ports)
            settings = {'folder_id': 'synthetic-folder', 'codex_home': str(root / 'codex-home'),
                        'selection': {'model': 'gpt-6-astra', 'reasoning_effort': 'xhigh'},
                        'authorized_user_file': str(root / 'synthetic-credentials.json')}
            with mock.patch.object(launcher, 'settings', return_value=settings), \
                 mock.patch('dots_lite.launcher.credential_check') as credentials:
                with self.assertRaises(Cancelled): launcher.start(SimpleNamespace())
            self.assertFalse((root / 'new-state').exists())
            credentials.assert_not_called(); ports.spawn.assert_not_called(); ports.copy.assert_not_called()
            message = ui.confirm.call_args.args[0]
            self.assertIn('replacing the control Doc body', message)
            self.assertIn('Upload actual user-task result JSON', message)
            self.assertIn('separate exact-patch approval', message)

    def test_copy_cancel_preserves_files_and_never_exposes_secret(self):
        self._copy_scenario(consent=False)

    def test_old_activation_cannot_acquire_new_scope_by_copy(self):
        self._copy_scenario(contract=None, expected_error='fresh_transport_authorization_activation_required')

    def test_changed_package_cannot_copy_new_scope(self):
        self._copy_scenario(package_hash='f' * 64, expected_error='lightweight_package_changed_since_start')

    def _copy_scenario(self, *, consent=True, contract=auth.CONTRACT, package_hash='a' * 64, expected_error=None):
        with tempfile.TemporaryDirectory() as folder:
            root = private_dir(folder)
            state = private_dir(root / 'state', create=True)
            g = grant()
            runtime = private_dir(state / 'runs' / g['activation_id'], create=True)
            save(runtime / 'grant.json', g)
            save(runtime / 'spec.json', {'package_sha256': g['package_sha256'],
                                        'transport_authorization_contract': contract})
            private_write(runtime / 'join-key', KEY.encode())
            save(state / 'current.json', {'contract': CONTRACT, 'run_id': g['activation_id'],
                                          'runtime': str(runtime), 'pid': None, 'process_identity': None})
            before = {str(path): path.read_bytes() for path in state.rglob('*') if path.is_file()}
            ui = mock.Mock(); ui.confirm.return_value = consent
            ports = mock.Mock(); ports.package.return_value = package_hash; ports.now.return_value = g['created_at'] + 1
            launcher = Launcher(root=root, state=state, ui=ui, ports=ports)
            with mock.patch.object(launcher, '_ready', return_value={}):
                if expected_error:
                    with self.assertRaisesRegex(p.ProtocolError, expected_error): launcher.copy_join()
                else:
                    with self.assertRaises(Cancelled): launcher.copy_join()
            ports.copy.assert_not_called()
            after = {str(path): path.read_bytes() for path in state.rglob('*') if path.is_file()}
            self.assertEqual(before, after)
            if not expected_error:
                self.assertNotIn(KEY, ui.confirm.call_args.args[0])
                self.assertIn('BOTH result uploads and control-Doc writes', ui.confirm.call_args.args[0])


if __name__ == '__main__': unittest.main()
