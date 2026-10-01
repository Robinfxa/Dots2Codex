"""Injectable first-run/repeat/recovery tests. No live Google, pip, auth or Mac UI."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from remote_transport import mac_launcher as launch
from remote_transport import mac_environment as envmod
from remote_transport import mac_setup as setup
from remote_transport import mac_ui
from remote_transport import router_mac as router
from remote_transport.model import ProtocolError
from remote_transport.selection import select, load_catalog
from remote_transport.session import _save


class FakeUI:
    def __init__(self, confirm=None, choices=None):
        self.confirms = list(confirm or []); self.choices = list(choices or [])
        self.messages = []; self.confirmed = []
    def confirm(self, message):
        self.confirmed.append(message)
        answer = self.confirms.pop(0) if self.confirms else True
        if isinstance(answer, Exception): raise answer
        return answer
    def choose(self, message, choices, default=None):
        value = self.choices.pop(0) if self.choices else list(choices)[0]
        if isinstance(value, Exception): raise value
        assert value in choices, (value, choices)
        return value
    def notify(self, message): self.messages.append(message)
    def text(self, message, default=''): return default
    def folder(self, message, default=''): return default
    def file(self, message, default=''): return default


class FakePorts:
    def __init__(self):
        self.calls = []; self.google_error = None; self.copy_success = True
    def verify_codex(self, path): self.calls.append(('codex', str(path))); return '/synthetic/codex'
    def google(self, config):
        self.calls.append(('google', copy.deepcopy(config)))
        if self.google_error: raise self.google_error
        return {'folder_verified':True}
    def copy(self, message): self.calls.append(('copy',message)); return self.copy_success
    def start(self, args): self.calls.append(('start',args)); return {'router_ready':True}
    def stop(self, args): self.calls.append(('stop',args)); return {'closed':False,'process_stopped':True,'stage':'RECOVERY_REQUIRED'}
    def active_status(self, active): return {'stage':active['stage'],'router_ready':False}


class LauncherFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.state = self.root/'launcher'; self.config = self.root/'config/router.json'; self.active = self.root/'config/active.json'
        self.work = self.root/'workspace'; self.work.mkdir()
        self.credential = self.root/'credential.json'
        self.secret = 'SYNTHETIC_SECRET_DO_NOT_LOG'
        self.credential_info = {'type':'authorized_user','client_id':'fixture','client_secret':self.secret,'refresh_token':self.secret,
                               'scopes':sorted(setup.SCOPES)}
        _save(self.credential,self.credential_info)
        self.ui = FakeUI(); self.ports = FakePorts()
        self.launcher = launch.Launcher(state=self.state,config=self.config,active=self.active,ui=self.ui,ports=self.ports)
        self.args = SimpleNamespace(operation='start', credentials=str(self.credential),folder_id='folder',workdir=str(self.work),codex='/synthetic/codex',
            model='gpt-6.1-sol',effort='xhigh',catalog=None,no_launch_codex=True,config=str(self.config),active=str(self.active))
    def tearDown(self): self.tmp.cleanup()
    def configure(self): return self.launcher.configure(self.args)
    def active_write(self, **values):
        self.config.parent.mkdir(mode=0o700,exist_ok=True)
        _save(self.active, {'stage':'WAITING_FOR_WORKER','closed':False,'process_stopped':False,**values})


class SetupTests(LauncherFixture):
    def test_first_run_saves_only_paths_and_pair_after_read(self):
        result = self.configure(); value = json.loads(self.config.read_text())
        self.assertTrue(result['configured']);self.assertFalse(result['router_ready'])
        self.assertEqual(value['model_selection']['reasoning_effort'],'xhigh')
        self.assertNotIn(self.secret, self.config.read_text()+self.launcher.consent.read_text()+str(self.ui.messages)+str(self.ui.confirmed))
        self.assertTrue(self.launcher.approved(value));self.assertEqual(stat.S_IMODE(self.config.stat().st_mode),0o600)
        self.assertEqual([c[0] for c in self.ports.calls],['codex','google'])
    def test_settings_preserve_existing_budget_and_policy(self):
        self.configure();config=self.launcher.load();config['seconds']=600;config['max_requests']=5;config['scope']='text_only';_save(self.config,config)
        self.configure();updated=self.launcher.load()
        self.assertEqual((updated['seconds'],updated['max_requests'],updated['scope']),(600,5,'text_only'))
    def test_credential_reuse_cancel_has_no_mutations_or_network(self):
        self.ui.confirms=[False]
        with self.assertRaises(mac_ui.Cancelled):self.configure()
        self.assertFalse(self.config.parent.exists());self.assertFalse(self.state.exists());self.assertEqual(self.ports.calls,[])
    def test_final_save_cancel_does_not_create_configuration(self):
        self.ui.confirms=[True,False]
        with self.assertRaises(mac_ui.Cancelled):self.configure()
        self.assertFalse(self.config.parent.exists());self.assertFalse(self.state.exists())
        self.assertEqual([c[0] for c in self.ports.calls],['codex','google'])
    def test_offline_error_is_not_oauth_or_config_success(self):
        self.ports.google_error=RuntimeError('launcher_google_read_unavailable')
        with self.assertRaises(RuntimeError):self.configure()
        self.assertFalse(self.config.parent.exists());self.assertFalse(self.state.exists())
    def test_scope_insufficient_fails_before_google(self):
        self.credential_info['scopes']=[];_save(self.credential,self.credential_info)
        with self.assertRaisesRegex(ProtocolError,'scopes'):self.configure()
        self.assertEqual(self.ports.calls,[]);self.assertFalse(self.config.parent.exists())
    def test_unexpected_token_endpoint_fails_before_google(self):
        self.credential_info['token_uri']='https://invalid.example/token';_save(self.credential,self.credential_info)
        with self.assertRaisesRegex(ProtocolError,'unexpected_token'):self.configure()
        self.assertEqual(self.ports.calls,[])
    def test_credentials_symlink_rejected(self):
        link=self.root/'link';link.symlink_to(self.credential);self.args.credentials=str(link)
        with self.assertRaisesRegex(ProtocolError,'symlink'):self.configure()
        self.assertEqual(self.ports.calls,[])
    def test_unsafe_credential_mode_rejected(self):
        self.credential.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'unsafe'):self.configure()
        self.assertEqual(self.ports.calls,[])
    def test_workspace_must_exist_never_mkdir_before_google(self):
        self.args.workdir=str(self.root/'new-workspace')
        with self.assertRaisesRegex(ProtocolError,'existing_workspace'):self.configure()
        self.assertFalse(Path(self.args.workdir).exists());self.assertNotIn('google',[c[0] for c in self.ports.calls])
    def test_workspace_symlink_rejected(self):
        link=self.root/'linked-work';link.symlink_to(self.work);self.args.workdir=str(link)
        with self.assertRaisesRegex(ProtocolError,'symlink'):self.configure()
    def test_partial_pair_fails_before_prompts(self):
        self.args.effort=None
        with self.assertRaisesRegex(ProtocolError,'required_together'):self.configure()
        self.assertEqual(self.ui.confirmed,[]);self.assertEqual(self.ports.calls,[])
    def test_settings_cannot_modify_active_runtime(self):
        self.active_write();before=self.active.read_bytes()
        with self.assertRaisesRegex(ProtocolError,'stop_before_settings'):self.configure()
        self.assertEqual(before,self.active.read_bytes());self.assertEqual(self.ports.calls,[])
    def test_save_detects_concurrent_config_change(self):
        original=self.ports.google
        def change(config):
            original(config);self.config.parent.mkdir(mode=0o700);_save(self.config,config)
        self.ports.google=change
        with self.assertRaisesRegex(ProtocolError,'config_changed'):self.configure()
    def test_scope_notice_is_broad_and_no_claim_bidirectional(self):
        self.configure()
        self.assertIn('beyond this folder',self.ui.confirmed[0]);self.assertIn('still pending',self.ui.confirmed[-1])
    def test_credential_changed_requires_reuse_consent_again(self):
        self.configure();config=self.launcher.load();self.assertTrue(self.launcher.approved(config))
        self.credential_info['refresh_token']='OTHER_FIXTURE';_save(self.credential,self.credential_info)
        self.assertFalse(self.launcher.approved(config))
    def test_corrupt_config_fails_closed(self):
        self.config.parent.mkdir(mode=0o700);_save(self.config,{'bad':'shape'})
        with self.assertRaises(ProtocolError):self.configure()
    def test_config_parent_symlink_rejected_without_following(self):
        destination=self.root/'destination';destination.mkdir();(self.root/'linked').symlink_to(destination)
        with self.assertRaisesRegex(ProtocolError,'symlink'):
            launch.Launcher(state=self.state,config=self.root/'linked/router.json',active=self.active)


class SessionTests(LauncherFixture):
    def test_repeat_start_reuses_config_without_setup_prompt(self):
        self.configure();self.ui.confirmed.clear();self.ports.calls.clear()
        result=self.launcher.start(self.args)
        self.assertTrue(result['router_ready']);self.assertEqual(len(self.ui.confirmed),1)
        self.assertEqual([c[0] for c in self.ports.calls],['codex','google','start'])
        self.assertTrue(callable(self.ports.calls[-1][1].join_callback))
    def test_start_cancel_never_creates_resources(self):
        self.configure();self.ports.calls.clear();self.ui.confirms=[False]
        with self.assertRaises(mac_ui.Cancelled):self.launcher.start(self.args)
        self.assertNotIn('start',[c[0] for c in self.ports.calls])
    def test_existing_pairing_routes_to_status_no_duplicate_resources(self):
        self.active_write();self.ui.choices=['Status'];self.launcher.start(self.args)
        self.assertEqual(self.ports.calls,[])
    def test_stale_incomplete_record_blocks_new_start(self):
        for stage in ['PREPARING','RECOVERY_REQUIRED','READY','CLOSED']:
            self.active_write(stage=stage);self.ui.choices=['Status'];self.launcher.start(self.args)
        self.assertEqual(self.ports.calls,[])
    def test_closed_and_process_stopped_allows_new_pairing(self):
        self.configure();self.active_write(stage='CLOSED',closed=True,process_stopped=True)
        self.assertFalse(self.launcher.active_blocks());self.launcher.start(self.args)
        self.assertIn('start',[c[0] for c in self.ports.calls])
    def test_abort_without_stopped_is_not_restartable(self):
        self.active_write(stage='ABORTED',process_stopped=False);self.assertTrue(self.launcher.active_blocks())
    def test_clipboard_failure_reports_without_secret_output(self):
        self.ports.copy_success=False
        active={'join_message_file':str(self.root/'private-message.txt')}
        result=self.launcher.copy_join(active,'join_code=PRIVATE_FIXTURE')
        self.assertFalse(result['join_copied']);self.assertIn('failed',self.ui.messages[-1])
        self.assertNotIn('PRIVATE_FIXTURE',str(self.ui.messages))
    def test_clipboard_cancel_writes_nothing(self):
        self.ui.confirms=[False]
        with self.assertRaises(mac_ui.Cancelled):self.launcher.copy_join({},'PRIVATE_FIXTURE')
        self.assertEqual(self.ports.calls,[])
    def test_recopied_join_requires_pending_exact_private_path(self):
        runtime=self.root/'session';runtime.mkdir(mode=0o700)
        path=runtime/'join-message.txt';path.write_text('join_code=FIXTURE');path.chmod(0o600)
        result=self.launcher.copy_join({'stage':'WAITING_FOR_WORKER','runtime':str(runtime),'join_message_file':str(path)})
        self.assertTrue(result['join_copied'])
        with self.assertRaisesRegex(ProtocolError,'not_pending'):
            self.launcher.copy_join({'stage':'READY'})
    def test_status_does_not_treat_configured_as_ready(self):
        self.configure();result=self.launcher.status()
        self.assertTrue(result['configured']);self.assertFalse(result['router_ready'])
    def test_stop_preserves_partial_result_and_reports_no_restore(self):
        self.active_write();result=self.launcher.stop(self.args)
        self.assertFalse(result['closed']);self.assertTrue(result['process_stopped'])
        self.assertEqual(result['global_config_restore'],'not_applicable_single_session')
    def test_global_restore_alias_delegates_without_single_session_mutations(self):
        self.args.operation='restore-global'
        with patch.object(self.launcher,'global_backend') as backend:
            backend.return_value.restore.return_value={'global_config_restored':False,'stage':'NOT_STARTED'}
            result=self.launcher.dispatch(self.args)
            backend.return_value.restore.assert_called_once_with(self.args)
        self.assertFalse(result['global_config_restored']);self.assertEqual(self.ports.calls,[]);self.assertFalse(self.state.exists())
    def test_safe_errors_drop_arbitrary_provider_secret(self):
        self.assertEqual(launch.safe_error(RuntimeError(self.secret+' https://example.invalid')), 'RuntimeError')
        self.assertEqual(launch.safe_error(ProtocolError('launcher_config_changed_retry_settings')),'launcher_config_changed_retry_settings')
    def test_readiness_requires_exact_binding_and_live_status(self):
        active={'stage':'READY','closed':False,'facade_pid':7,'deployment':'dep','pin_expires':9999999999,
                'model_selection':None,'ready_file':'unused','runtime':str(self.root),'controller_journal':'unused'}
        ready={'pid':7,'deployment':'dep','session_control':'docs_cas','expires':9999999999,'selection':None}
        body={'deployment':'dep','closed':False,'expires':9999999999,'selection':None}
        ports=launch.Ports()
        with patch.object(router,'_owned_process',return_value=True),patch.object(router,'_pid_alive',return_value=True),patch.object(router,'_cancelled',return_value=False),patch.object(router,'_load_private_json',return_value=ready),patch.object(router,'_bridge_call',return_value=(200,body)):
            self.assertTrue(ports.active_status(active)['router_ready'])
            ready['deployment']='other';self.assertFalse(ports.active_status(active)['router_ready'])


class SharedPreflightTests(unittest.TestCase):
    def test_metadata_requires_exact_mime_and_not_trashed(self):
        good={'id':'folder','mimeType':setup.FOLDER_MIME,'trashed':False}
        self.assertTrue(setup.validate_folder_metadata(good,'folder')['folder_verified'])
        for changes in [{'mimeType':'application/vnd.google-apps.document'},{'trashed':True},{'id':'other'},{'mimeType':None}]:
            with self.assertRaises(ProtocolError):setup.validate_folder_metadata({**good,**changes},'folder')
    def test_folder_url_parser_rejects_non_google_and_query(self):
        self.assertEqual(setup.folder_id('https://drive.google.com/drive/folders/abc-_1'),'abc-_1')
        for value in ['https://evil.test/drive/folders/x','https://drive.google.com/drive/folders/x?auth=secret','../bad','folder space']:
            with self.assertRaises(ProtocolError):setup.folder_id(value)
    def test_candidates_are_exact_paths_deduplicated_not_secret_crawl(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'known';path.write_text('not read during discovery')
            secret=root/'browser/token.json';secret.parent.mkdir();secret.write_text('must not discover')
            result=setup.credential_candidates({'authorized_user_file':str(path)},environ={'DOTS_GOOGLE_AUTHORIZED_USER_FILE':str(path),'GOOGLE_APPLICATION_CREDENTIALS':str(secret)},home=root)
            self.assertEqual(result,[path])
    def test_env_restore_after_google_failure(self):
        with patch.dict(os.environ,{'DOTS_GOOGLE_AUTHORIZED_USER_FILE':'prior'}):
            try:
                with setup.credential_environment('candidate'): raise RuntimeError()
            except RuntimeError:pass
            self.assertEqual(os.environ['DOTS_GOOGLE_AUTHORIZED_USER_FILE'],'prior')


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.state=self.root/'state'
        self.env=envmod.Environment(launch.ROOT,self.state)
    def tearDown(self):self.tmp.cleanup()
    def test_install_cancel_creates_nothing(self):
        with self.assertRaises(mac_ui.Cancelled):self.env.ensure(FakeUI(confirm=[False]))
        self.assertFalse(self.state.exists())
    def test_healthy_pointer_never_installs_again(self):
        self.state.mkdir(mode=0o700);target=self.state/'environments/existing';target.mkdir(mode=0o700,parents=True)
        _save(self.env.pointer,{'contract':1,'path':str(target),'fingerprint':self.env.fingerprint})
        with patch.object(self.env,'health',return_value=True),patch.object(self.env,'_run') as run:
            ui=FakeUI();self.assertEqual(self.env.ensure(ui),target/'bin/python3');run.assert_not_called();self.assertEqual(ui.confirmed,[])
    def test_partial_environment_and_pip_failure_never_updates_pointer(self):
        def run(args,**kwargs): return SimpleNamespace(returncode=0 if 'venv' in args else 1,stdout='',stderr='SECRET_PROVIDER_TEXT')
        self.env.run=run
        with self.assertRaisesRegex(ProtocolError,'install_failed'):self.env.ensure(FakeUI())
        self.assertFalse(self.env.pointer.exists())
        stages=list(self.state.glob('environments/*/install-state.json'));self.assertEqual(len(stages),1)
        self.assertEqual(json.loads(stages[0].read_text())['stage'],'incomplete');self.assertNotIn('SECRET_PROVIDER',stages[0].read_text())
    def test_candidate_created_at_final_path_pointer_only_atomic(self):
        calls=[]
        def run(args,**kwargs):calls.append(args);return SimpleNamespace(returncode=0,stdout='',stderr='')
        self.env.run=run
        with patch.object(self.env,'health',return_value=True):python=self.env.ensure(FakeUI())
        pointer=json.loads(self.env.pointer.read_text());self.assertEqual(pointer['path'],str(python.parent.parent))
        self.assertEqual(calls[0][-1],pointer['path']);self.assertIn('https://pypi.org/simple',calls[1]);self.assertIn('--isolated',calls[1])
    def test_double_install_lock_fails_closed(self):
        self.state.mkdir(mode=0o700)
        with envmod.private_lock(self.state/'install.lock'):
            with self.assertRaisesRegex(RuntimeError,'in_progress'):self.env.ensure(FakeUI())
        self.assertFalse((self.state/'environments').exists())
    def test_pointer_symlink_is_not_overwritten(self):
        self.state.mkdir(mode=0o700);outside=self.root/'outside';outside.write_text('preserve');self.env.pointer.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError,'unsafe_environment_pointer'):self.env.ensure(FakeUI())
        self.assertEqual(outside.read_text(),'preserve')
    def test_environment_path_escape_is_not_executed(self):
        self.state.mkdir(mode=0o700);_save(self.env.pointer,{'contract':1,'path':'/tmp/elsewhere','fingerprint':self.env.fingerprint})
        with patch.object(self.env,'health') as health:self.assertIsNone(self.env.current());health.assert_not_called()
    def test_health_requires_imports_exact_pins_and_pip_check(self):
        path=self.root/'env';path.mkdir(mode=0o700);(path/'bin').mkdir();python=path/'bin/python3';python.write_text('fixture');python.chmod(0o700);(path/'pyvenv.cfg').write_text('fixture')
        calls=[]
        def run(args,**kwargs):calls.append(args);return SimpleNamespace(returncode=0,stdout='healthy\n',stderr='')
        self.env.run=run;self.assertTrue(self.env.health(path));self.assertEqual(len(calls),2);self.assertEqual(calls[1][-2:],['pip','check'])
        self.assertIn('sys.prefix == sys.argv[1]',calls[0][4]);self.assertIn('m.version(k)==v',calls[0][4])
    def test_health_rejects_wrong_prefix_missing_imports_and_broken_pip(self):
        path=self.root/'env';path.mkdir(mode=0o700);(path/'bin').mkdir();python=path/'bin/python3';python.write_text('fixture');python.chmod(0o700);(path/'pyvenv.cfg').write_text('fixture')
        for sequence in [[1],[0,1]]:
            results=iter(sequence)
            self.env.run=lambda *a,**kw:SimpleNamespace(returncode=next(results),stdout='healthy\n',stderr='')
            self.assertFalse(self.env.health(path))
    def test_dependency_manifest_is_pinned(self):
        pins=envmod.pinned_requirements(launch.ROOT)
        self.assertEqual(pins['google-auth-oauthlib'],'1.2.2');self.assertEqual(pins['jsonschema'],'4.26.0')
    def test_private_directory_symlink_rejected(self):
        self.state.symlink_to(self.root)
        with self.assertRaisesRegex(ProtocolError,'symlink'):envmod.Environment(launch.ROOT,self.state)


class UITests(unittest.TestCase):
    def test_native_confirmation_cancel_and_timeout_not_approval(self):
        for result in [SimpleNamespace(returncode=1,stdout='',stderr='execution error (-128)'),SimpleNamespace(returncode=0,stdout='__TIMEOUT__\n',stderr='')]:
            ui=mac_ui.UI(run=lambda *a,**kw:result,tty=True,native=True,input_fn=lambda p:'yes')
            with self.assertRaises(mac_ui.Cancelled):ui.confirm('install?')
        def timeout(*args,**kw):raise subprocess.TimeoutExpired('osascript',180)
        with self.assertRaises(mac_ui.Cancelled):mac_ui.UI(run=timeout,tty=True,native=True).confirm('install?')
    def test_native_false_list_cancels(self):
        ui=mac_ui.UI(run=lambda *a,**kw:SimpleNamespace(returncode=0,stdout='false\n',stderr=''),tty=True,native=True)
        with self.assertRaises(mac_ui.Cancelled):ui.choose('model?',['x'])
    def test_unavailable_gui_falls_back_only_to_tty(self):
        result=SimpleNamespace(returncode=1,stdout='',stderr='GUI unavailable')
        ui=mac_ui.UI(run=lambda *a,**kw:result,tty=True,native=True,input_fn=lambda p:'yes')
        self.assertTrue(ui.confirm('install?'))
        ui.tty=False
        with self.assertRaises(mac_ui.Cancelled):ui.confirm('install?')
    def test_tty_empty_is_never_approval(self):
        self.assertFalse(mac_ui.UI(tty=True,native=False,input_fn=lambda p:'').confirm('install?'))
        with self.assertRaises(mac_ui.Cancelled):mac_ui.UI(tty=False,native=False).confirm('install?')
    def test_applescript_arguments_are_not_interpolated_code(self):
        calls=[]
        def run(args,**kw):calls.append(args);return SimpleNamespace(returncode=0,stdout='Continue\n',stderr='')
        value='quotes " and \\ and 中文; do shell script "false"'
        self.assertTrue(mac_ui.UI(run=run,tty=False,native=True).confirm(value))
        self.assertNotIn(value,calls[0][2]);self.assertEqual(calls[0][-1],value)


class SafetyRegressionTests(LauncherFixture):
    def test_approved_config_hash_is_checked_under_router_lease(self):
        self.configure();self.launcher.start(self.args)
        call=self.ports.calls[-1][1]
        changed=self.launcher.load();changed['folder_id']='unapproved-folder';_save(self.config,changed)
        with patch.object(router,'_set_google_env') as google_env, patch('examples.google_clients.create_drive_client') as drive:
            with self.assertRaisesRegex(ProtocolError,'changed_after_launcher_approval'):
                router.start(call)
            google_env.assert_not_called();drive.assert_not_called()
        self.assertFalse(self.active.exists())
    def test_stop_config_corruption_still_runs_exact_cleanup(self):
        runtime=self.root/'runtime';runtime.mkdir(mode=0o700)
        self.active_write(runtime=str(runtime),session_id='fixture',facade_pid=None)
        self.config.write_text('corrupt');self.config.chmod(0o600)
        with patch.object(router,'_cleanup',return_value={'closed':False,'process_stopped':True}) as cleanup:
            result=router.stop(self.args)
        self.assertTrue(result['process_stopped']);self.assertIsNone(cleanup.call_args.args[0])
    def test_stop_before_active_publication_delegates_to_router_lease(self):
        self.assertFalse(self.active.exists())
        result=self.launcher.stop(self.args)
        self.assertTrue(result['process_stopped'])
        self.assertEqual(self.ports.calls[0][0],'stop')
    def test_status_stop_and_menu_do_not_require_installation(self):
        for argv, choices in [(['status'],[]),(['stop'],[]),(['menu'],['Single-session status']),(['menu'],['Stop single-session Router'])]:
            ui=FakeUI(choices=choices)
            with patch.object(launch,'UI',return_value=ui),patch.object(launch,'verify_package'),patch.object(launch.Environment,'ensure') as ensure,patch.object(launch.Environment,'current',return_value=None),patch.object(launch.Launcher,'dispatch',return_value={'router_ready':False}):
                launch.main(argv+['--state',str(self.state),'--config',str(self.config),'--active',str(self.active)])
                ensure.assert_not_called()
    def test_existing_active_double_click_offers_controls_without_install(self):
        self.active_write()
        with patch.object(launch,'UI',return_value=FakeUI(choices=['Status'])),patch.object(launch,'verify_package'),patch.object(launch.Environment,'ensure') as ensure,patch.object(launch.Environment,'current',return_value=None),patch.object(launch.Ports,'active_status',return_value={'router_ready':False}):
            launch.main(['start','--state',str(self.state),'--config',str(self.config),'--active',str(self.active)])
            ensure.assert_not_called()
    def test_join_callback_cancel_uses_existing_router_cleanup(self):
        from remote_tests.test_router_startup import StartupTests
        fixture=StartupTests();fixture.setUp()
        try:
            fixture.args.join_callback=lambda *a: (_ for _ in ()).throw(mac_ui.Cancelled())
            with fixture.environment():
                with self.assertRaises(mac_ui.Cancelled):router.start(fixture.args)
            active=router._load_private_json(fixture.path)
            self.assertEqual(fixture.world.creates,2)
            self.assertEqual(active['stage'],'ABORTED');self.assertTrue(active['process_stopped'])
        finally:fixture.tearDown()
    def test_raw_router_does_not_print_or_copy_join_by_default(self):
        from remote_tests.test_router_startup import StartupTests
        fixture=StartupTests();fixture.setUp()
        try:
            output=io.StringIO()
            with fixture.environment(),contextlib.redirect_stdout(output),patch.object(router,'_launch_facade',side_effect=fixture.launch),patch.object(router,'_copy_clipboard') as clipboard:
                router.start(fixture.args);clipboard.assert_not_called()
            self.assertNotIn('join_code=',output.getvalue())
            active=router._load_private_json(fixture.path)
            self.assertTrue(Path(active['join_message_file']).is_file())
        finally:fixture.tearDown()


if __name__=='__main__':unittest.main()
