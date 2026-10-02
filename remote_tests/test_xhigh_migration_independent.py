"""Independent migration audit: fixtures only, no live credentials or admission."""
import copy
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from remote_transport import selection as choice
from remote_transport import router_mac as router
from remote_transport import router_bootstrap as bootstrap
from remote_transport import global_control as queue
from remote_transport import global_gateway as gateway
from remote_transport import mac_ui
from remote_transport.codex_catalog import catalog_for_selection
from remote_transport.global_fixture import identity, post, request
from remote_transport.model import ProtocolError, canonical, hash_bytes
from remote_transport.router_join import _source_hashes
from remote_transport.session import _save
import test_mac_launcher as launcher_fixtures
import test_global_gateway as gateway_fixtures
import test_model_selection as selection_fixtures

OLD_VERSION = '2026-10-01.codex-0.159.2.v1'
OLD_HASH = 'ae293637817c1b758d5819b5f6944ad32fd8a21bd46092dd49aeeed5dcf5d583'
OLD_CATALOG_SOURCE_HASH = 'a19e20d55215a29d8d32eaa0ba963fed898faa744511863bab774f1ad27489e9'
OLD_SELECTION_SOURCE_HASH = '1fd2a81e6e2a95680131bdd5bc6e3d33177e8fdb6cf073bafc6652a4fddedfde'
MODELS = ('gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna', 'gpt-5.6-sol')
EFFORTS = ['low', 'medium', 'high', 'xhigh']


def retired(effort='max', model='gpt-6-astra'):
    return {'contract':'dots-native-selection/1', 'catalog_version':OLD_VERSION,
            'catalog_sha256':OLD_HASH, 'model':model, 'reasoning_effort':effort}


class CatalogMigrationAudit(unittest.TestCase):
    def test_exact_twenty_pairs_xhigh_defaults_and_new_identity(self):
        catalog = choice.load_catalog()
        self.assertEqual(list(catalog['models']), list(MODELS))
        self.assertNotEqual(catalog['version'], OLD_VERSION)
        self.assertNotEqual(choice.catalog_hash(), OLD_HASH)
        self.assertEqual(choice.catalog_hash(), hash_bytes(canonical(catalog)))
        for model in MODELS:
            with self.subTest(model=model):
                self.assertEqual(catalog['models'][model]['bridge_efforts'], EFFORTS)
                self.assertEqual(catalog['models'][model]['default_effort'], 'xhigh')
                for effort in EFFORTS:
                    selected = choice.select(catalog, model, effort)
                    submitted = choice.spawn_arguments(selected, 'audit_worker', 'Offline fixture')
                    self.assertEqual((submitted['model'], submitted['reasoning_effort'], submitted['fork_turns']),
                                     (model, effort, 'none'))

    def test_max_and_ultra_never_coerced_in_current_catalog_or_wire(self):
        for model in MODELS:
            for effort in ['max', 'ultra', 'persistent', 'XHIGH', '', None]:
                with self.subTest(model=model, effort=effort):
                    with self.assertRaises(ProtocolError):
                        choice.select(choice.load_catalog(), model, effort)
                    raw_request = request('audit', model=model, effort=effort)
                    before = canonical(raw_request)
                    with self.assertRaises(ProtocolError):
                        selection_fixtures.validate_remote_request(raw_request, 'responses_tools')
                    self.assertEqual(canonical(raw_request), before)

    def test_removed_max_cannot_hide_in_legacy_or_selected_effort_aliases(self):
        for model in [*MODELS, 'native-subagent-bridge']:
            for scope in ['text_only', 'responses_tools']:
                for location in ['nested', 'reasoning_effort', 'model_reasoning_effort']:
                    body = request('max alias audit', model=model, effort='xhigh')
                    if location == 'nested': body['reasoning']['effort'] = 'max'
                    else: body[location] = 'max'
                    before = canonical(body)
                    with self.subTest(model=model, scope=scope, location=location):
                        with self.assertRaisesRegex(ProtocolError, 'max_effort_removed'):
                            selection_fixtures.validate_remote_request(body, scope)
                        self.assertEqual(canonical(body), before)

    def test_cli_and_global_catalogs_advertise_only_allowed_pairs(self):
        catalog = choice.load_catalog()
        rows = gateway.global_catalog(choice.select(catalog, MODELS[0], 'xhigh'))['models']
        self.assertEqual([row['slug'] for row in rows], list(MODELS))
        self.assertEqual(sum(len(row['supported_reasoning_levels']) for row in rows), 20)
        for row in rows:
            self.assertEqual(row['default_reasoning_level'], 'xhigh')
            self.assertEqual([v['effort'] for v in row['supported_reasoning_levels']], EFFORTS)
            self.assertFalse(row['supports_reasoning_effort_updates'])
        for model in MODELS:
            for effort in EFFORTS:
                row = catalog_for_selection(choice.select(catalog, model, effort))['models'][0]
                self.assertEqual(row['default_reasoning_level'], effort)
                self.assertEqual([v['effort'] for v in row['supported_reasoning_levels']], [effort])
                self.assertFalse(row['use_responses_lite'])
                self.assertNotIn('approval_policy', row)
                self.assertNotIn('sandbox_mode', row)

    def test_explicit_supported_global_default_is_preserved(self):
        for effort in EFFORTS:
            rows = gateway.global_catalog(choice.select(choice.load_catalog(), MODELS[0], effort))['models']
            self.assertEqual({row['default_reasoning_level'] for row in rows}, {effort})

    def test_per_model_fallback_uses_xhigh_instead_of_first_effort(self):
        # Exercise the fallback with a narrower future model fixture, without
        # changing the real released catalog or silently changing an input pair.
        catalog = choice.load_catalog()
        catalog['models']['gpt-6-luna']['bridge_efforts'] = ['medium', 'high', 'xhigh']
        with patch.object(choice, 'load_catalog', return_value=catalog), \
             patch.object(gateway, 'load_catalog', return_value=catalog):
            selected = choice.select(catalog, 'gpt-6.1-sol', 'low')
            rows = {row['slug']:row for row in gateway.global_catalog(selected)['models']}
        self.assertEqual(rows['gpt-6.1-sol']['default_reasoning_level'], 'low')
        self.assertEqual(rows['gpt-6-luna']['default_reasoning_level'], 'xhigh')

    def test_retired_settings_binding_is_read_only_and_not_runtime_authority(self):
        for model in MODELS:
            for effort in EFFORTS + ['max']:
                old = retired(effort, model); before = canonical(old)
                self.assertEqual(choice.validate_settings_selection(old), old)
                with self.assertRaises(ProtocolError): choice.validate_selection(old)
                with self.assertRaises(ProtocolError): choice.spawn_arguments(old, 'audit_worker', 'Offline fixture')
                self.assertEqual(canonical(old), before)

    def test_settings_recovery_does_not_accept_arbitrary_or_malformed_binding(self):
        for change in [{'catalog_sha256':'0'*64}, {'catalog_version':'unknown'}, {'model':'arbitrary'},
                       {'reasoning_effort':'ultra'}, {'reasoning_effort':None}, {'contract':'other'},
                       {'unreviewed':True}]:
            old = {**retired(), **change}; before = canonical(old)
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                choice.validate_settings_selection(old)
            self.assertEqual(canonical(old), before)

    def test_release_source_binding_changes_with_catalog(self):
        hashes = _source_hashes()
        self.assertNotEqual(hashes['remote_transport/native_capabilities.json'], OLD_CATALOG_SOURCE_HASH)
        self.assertNotEqual(hashes['remote_transport/selection.py'], OLD_SELECTION_SOURCE_HASH)
        root = Path(choice.__file__).resolve().parents[1]
        self.assertEqual(hashes['remote_transport/native_capabilities.json'],
                         hash_bytes((root/'remote_transport/native_capabilities.json').read_bytes()))

    def test_signed_global_root_with_retired_source_is_rejected_without_rewrite(self):
        now = int(time.time()); code = 'a'*32
        state = queue.initial(activation_id='a'*32, queue_id='b'*32, folder_id='folder',
            document_id='doc', tab_id='t.0', join_code=code, created=now, expires=now+600,
            runtime_source_hashes=_source_hashes())
        state['runtime_source_hashes']['remote_transport/native_capabilities.json'] = OLD_CATALOG_SOURCE_HASH
        state['root_mac'] = bootstrap.proof(code, 'global-root/2', queue.root_of(state))
        before = canonical(state)
        with self.assertRaisesRegex(ProtocolError, 'source_binding_mismatch'):
            queue.verify(state, code, now=now)
        self.assertEqual(canonical(state), before)


class GatewayMigrationAudit(unittest.TestCase):
    setUp = gateway_fixtures.GlobalGatewayTests.setUp
    tearDown = gateway_fixtures.GlobalGatewayTests.tearDown
    count = gateway_fixtures.GlobalGatewayTests.count

    def test_http_max_rejected_before_route_or_native_admission(self):
        for model in MODELS:
            body = request('unsupported max', model=model, effort='max'); before = canonical(body)
            status, raw = post(self.store, self.generation, identity(), body)
            self.assertEqual(status, 409, raw)
            self.assertIn(b'max_effort_removed_explicit_reselection_required', raw)
            self.assertEqual(canonical(body), before)
        self.assertEqual(self.count('routes'), 0)
        self.assertEqual(self.controller.spawn_count, 0)

    def test_http_missing_effort_never_falls_back_to_default(self):
        for reasoning in [None, {}, {'summary':'none'}]:
            body = request('missing effort')
            if reasoning is None: body.pop('reasoning')
            else: body['reasoning'] = reasoning
            before = canonical(body)
            status, raw = post(self.store, self.generation, identity(), body)
            self.assertEqual(status, 409, raw)
            self.assertEqual(canonical(body), before)
        self.assertEqual(self.count('routes'), 0)
        self.assertEqual(self.controller.spawn_count, 0)

    def test_existing_gateway_activation_not_rebound_by_max_rejection(self):
        before = canonical(self.store.activation())
        with self.assertRaises(ProtocolError): self.store.activate(retired())
        self.assertEqual(canonical(self.store.activation()), before)
        self.assertEqual(self.count('activations'), 1)


class LauncherMigrationAudit(launcher_fixtures.LauncherFixture):
    def old_config(self, effort='max'):
        self.configure()
        config = self.launcher.load(); config['model_selection'] = retired(effort)
        config['seconds'] = 600; config['max_requests'] = 7; config['scope'] = 'text_only'
        _save(self.config, config)
        self.ui.confirmed.clear(); self.ports.calls.clear()
        self.args.model = self.args.effort = None
        return config

    def use_default_choices(self):
        seen = []
        def choose(message, choices, default=None):
            seen.append((message, list(choices), default))
            return default if default is not None else list(choices)[0]
        self.ui.choose = choose
        return seen

    def test_normal_load_and_starts_cannot_silently_rebind_retired_config(self):
        self.old_config(); before = self.config.read_bytes()
        for action in [self.launcher.load, lambda:self.launcher.start(self.args),
                       lambda:self.launcher.global_start(self.args)]:
            with self.assertRaises(ProtocolError): action()
            self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.ui.confirmed, [])
        self.assertEqual(self.ports.calls, [])

    def test_cancel_retired_settings_keeps_config_consent_and_auth_exact(self):
        self.old_config(); seen = self.use_default_choices()
        before = {p:p.read_bytes() for p in [self.config,self.launcher.consent,self.credential]}
        self.ui.confirms = [True,False]
        with self.assertRaises(mac_ui.Cancelled): self.configure()
        for path, raw in before.items(): self.assertEqual(path.read_bytes(), raw)
        self.assertFalse(any(call[0] in {'start','stop','copy'} for call in self.ports.calls))
        self.assertTrue(any(default == 'xhigh' for _,_,default in seen))

    def test_explicit_recovery_changes_only_pair_and_retired_catalog_path(self):
        original = self.old_config(); self.use_default_choices()
        old_catalog = self.root/'old-catalog.json'; _save(old_catalog, {'fixture':'old catalog bytes'})
        original['catalog'] = str(old_catalog); _save(self.config, original)
        sentinel = self.work/'config.toml'; sentinel.write_bytes(b'# unrelated config\nmodel = "untouched"\n')
        evidence = self.work/'signed-root.json'; evidence.write_bytes(b'{"fixture":"signed root never rewritten"}')
        protected = {p:p.read_bytes() for p in [self.credential,old_catalog,sentinel,evidence]}
        result = self.configure(); current = self.launcher.load()
        self.assertTrue(result['configured']); self.assertFalse(result['global_config_changed'])
        self.assertEqual(current['model_selection'], choice.select(choice.load_catalog(), 'gpt-6-astra', 'xhigh'))
        expected = {k:v for k,v in original.items() if k not in {'model_selection','catalog'}}
        self.assertEqual({k:v for k,v in current.items() if k != 'model_selection'}, expected)
        for path, raw in protected.items(): self.assertEqual(path.read_bytes(), raw)
        self.assertNotIn(self.secret, str(self.ui.confirmed)+str(self.ui.messages))
        self.assertFalse(any(call[0] in {'start','stop','copy'} for call in self.ports.calls))

    def test_active_single_session_prevents_retired_settings_recovery(self):
        self.old_config(); self.active_write(); before = self.config.read_bytes(); active = self.active.read_bytes()
        with self.assertRaisesRegex(ProtocolError, 'stop_before_settings'): self.configure()
        self.assertEqual(self.config.read_bytes(), before); self.assertEqual(self.active.read_bytes(), active)
        self.assertEqual(self.ui.confirmed, []); self.assertEqual(self.ports.calls, [])

    def test_current_explicit_low_settings_default_remains_low(self):
        self.args.effort = 'low'; self.configure()
        self.args.model = self.args.effort = None; seen = self.use_default_choices()
        self.configure()
        self.assertEqual(self.launcher.load()['model_selection']['reasoning_effort'], 'low')
        self.assertTrue(any(default == 'low' for _,_,default in seen))

    def test_corrupt_unrelated_old_config_is_never_recovered(self):
        original = self.old_config(); original['scope'] = 'unbounded'; _save(self.config, original)
        before = self.config.read_bytes()
        with self.assertRaises(ProtocolError): self.configure()
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.ui.confirmed, []); self.assertEqual(self.ports.calls, [])


if __name__ == '__main__': unittest.main()
