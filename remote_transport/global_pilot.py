"""Short-lived, same-owner PILOT evidence; never production readiness.

No inference, Google, native admission, installation, or config write occurs
here. The supervisor explicitly observes binary versions, prepares one private
challenge, sends it through the gateway, and verifies the stored result. HMACs
prevent accidental substitution by untrusted inputs, not a malicious local
owner who already controls Store.key. Native receipts remain parent-recorded
admission evidence, not independent underlying-model attestation.
"""
import copy
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import time
import uuid

from . import global_control as queue
from . import codex_desktop as app_identity
from .backend import read_private_file
from .codex_catalog import CODEX_VERSION, CODEX_SOURCE_COMMIT
from .global_gateway import (Store, PROTOCOL, endpoint, global_catalog, private_dir,
                             private_write, probe, route_key, strict_json)
from .model import Object, ProtocolError, canonical, hash_bytes, require
from .selection import pin_selection, validate_admission

CONTRACT='dots-global-pilot/1'
CONFIG_TRIAL='client-config-trial/1'
PLAN_SECONDS=600
PROOF_SECONDS=300
VERSION_SECONDS=1800
MAX_EVIDENCE=8*1024*1024


def _store(value):return value if isinstance(value,Store) else Store(value)


def _seal(store,kind,value):
    body={'contract':CONTRACT,'kind':kind,**copy.deepcopy(value)}
    return {**body,'mac':hmac.new(store.key.encode(),canonical(body,max_bytes=MAX_EVIDENCE),hashlib.sha256).hexdigest()}


def _unseal(store,kind,value):
    require(isinstance(value,dict),'pilot_signed_evidence_required')
    body={k:v for k,v in value.items() if k!='mac'};mac=value.get('mac')
    require(body.get('contract')==CONTRACT and body.get('kind')==kind and isinstance(mac,str)
            and hmac.compare_digest(mac,hmac.new(store.key.encode(),canonical(body,max_bytes=MAX_EVIDENCE),hashlib.sha256).hexdigest()),
            'pilot_evidence_signature_mismatch')
    return body


def _path(store,kind,identifier,*,create=False):
    require(isinstance(identifier,str) and re.fullmatch(r'[0-9a-f]{32}',identifier),'invalid_pilot_evidence_id')
    return private_dir(store.root/'pilot',create=create)/(kind+'-'+identifier+'.json')


def _save(store,kind,value):
    signed=_seal(store,kind,value)
    private_write(_path(store,kind,value['id'],create=True),canonical(signed,max_bytes=MAX_EVIDENCE))
    return signed


def _load(store,kind,identifier):
    value=strict_json(read_private_file(_path(store,kind,identifier),MAX_EVIDENCE))
    body=_unseal(store,kind,value)
    require(body.get('id')==identifier,'pilot_evidence_id_mismatch')
    return body


def _file(path,*,executable=False):
    path=Path(path).expanduser().absolute()
    require(not any(p.is_symlink() for p in [path,*path.parents]),'pilot_symlink_rejected')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        before=os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and not before.st_mode&0o022 and before.st_size<=256*1024*1024,
                'pilot_unsafe_evidence_file')
        require(not executable or os.access(path,os.X_OK),'pilot_executable_required')
        digest=hashlib.sha256()
        while True:
            raw=os.read(fd,1024*1024)
            if not raw:break
            digest.update(raw)
        after=os.fstat(fd)
        identity=lambda s:[s.st_dev,s.st_ino,s.st_uid,s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns]
        require(identity(before)==identity(after),'pilot_evidence_changed_during_read')
        return {'path':str(path),'sha256':digest.hexdigest(),'identity':identity(after)}
    finally:os.close(fd)


def _sources():
    root=Path(__file__).resolve().parents[1]
    files=sorted((root/'remote_transport').glob('*.py'))+sorted((root/'native_connector').glob('*.js'))
    files += [root/'remote_transport/native_capabilities.json',root/'requirements-global.txt',root/'docs/GLOBAL_NATIVE_CONTROLLER.md']
    return {'package_root':str(root),'files':{str(p.relative_to(root)):_file(p)['sha256'] for p in files}}


def _parser_binding():
    from .global_config import parser
    module=parser()
    return {'version':module.__version__,'source':_file(module.__file__)}


def _binary_file(path):
    invocation=Path(path).expanduser().absolute()
    # Homebrew/CLI launchers may be legitimate symlinks. Preserve the chosen
    # invocation and bind its resolution to the same regular executable bytes.
    resolved=invocation.resolve(strict=True)
    return {**_file(resolved,executable=True),'invocation_path':str(invocation)}


def _observe_binary(path):
    before=_binary_file(path)
    try:
        result=subprocess.run([before['path'],'--version'],capture_output=True,text=True,timeout=10,check=False)
    except (OSError,subprocess.SubprocessError):raise ProtocolError('pilot_binary_version_observation_failed') from None
    require(result.returncode==0 and result.stdout.strip()==CODEX_VERSION,'pilot_binary_version_mismatch')
    require(_binary_file(path)==before,'pilot_binary_changed_during_observation')
    return {**before,'version':result.stdout.strip(),'output_sha256':hash_bytes(result.stdout.encode())}


def observe_versions(store,cli_path,desktop_path):
    """Legacy strict profile: both explicitly selected binaries must match."""
    store=_store(store)
    value={'id':secrets.token_hex(16),'observed_at':time.time(),
           'profile':'strict-client-binaries/1',
           'binaries':{'cli':_observe_binary(cli_path),'desktop':_observe_binary(desktop_path)},
           'parser':_parser_binding(),'package':_sources()}
    return _save(store,'versions',value)


def observe_desktop_app(store,cli_path,desktop_app):
    """Config trial: exact terminal adapter, signed app identity, unknown app engine.

    This never substitutes an app's display version for an engine version and
    cannot produce the legacy two-binary profile.
    """
    store=_store(store);app_identity.check_application(desktop_app)
    value={'id':secrets.token_hex(16),'observed_at':time.time(),'profile':app_identity.TRIAL,
           'binaries':{'cli':_observe_binary(cli_path)},'desktop_app':copy.deepcopy(desktop_app),
           'desktop_compatibility_verified':False,'parser':_parser_binding(),'package':_sources()}
    app_identity.check_application(desktop_app)
    return _save(store,'versions',value)


def config_target(codex_home):
    """Bind one validated local home, without reading its contents or any app."""
    from .global_config import known_home
    home=Path(os.path.normpath(str(known_home(codex_home))));info=home.stat()
    return {'codex_home':str(home),'config_path':str(home/'config.toml'),
            'home_identity':[info.st_dev,info.st_ino,info.st_uid,info.st_mode]}


def catalog_adapter():
    return {'cli_version':CODEX_VERSION,'source_commit':CODEX_SOURCE_COMMIT,
            'schema':'codex-models-response/0.159.2'}


def observe_config_client(store,cli_path,codex_home):
    """Config-first trial, independent of app presence, identity or naming.

    The exact supported CLI remains adapter/catalog evidence only. This does
    not certify another consumer's engine, startup catalog or config precedence.
    """
    store=_store(store);target=config_target(codex_home)
    value={'id':secrets.token_hex(16),'observed_at':time.time(),'profile':CONFIG_TRIAL,
           'binaries':{'cli':_observe_binary(cli_path)},'config_target':target,
           'catalog_adapter':catalog_adapter(),'client_compatibility_verified':False,
           'desktop_compatibility_verified':False,'parser':_parser_binding(),'package':_sources()}
    require(config_target(codex_home)==target,'pilot_config_target_changed')
    return _save(store,'versions',value)


def _versions(store,evidence):
    value=_unseal(store,'versions',evidence)
    profile=value.get('profile','strict-client-binaries/1')
    require(isinstance(profile,str) and profile in ('strict-client-binaries/1',app_identity.TRIAL,CONFIG_TRIAL),
            'pilot_unknown_evidence_profile')
    now=time.time()
    require(type(value.get('observed_at')) in (int,float) and 0<=now-value['observed_at']<VERSION_SECONDS,
            'pilot_version_evidence_expired')
    require(value.get('package')==_sources(),'pilot_package_source_changed')
    require(value.get('parser')==_parser_binding(),'pilot_parser_evidence_changed')
    names=('cli','desktop') if profile=='strict-client-binaries/1' else ('cli',)
    require(set(value.get('binaries',{}))==set(names),'pilot_evidence_profile_mismatch')
    for name in names:
        record=value['binaries'][name]
        require(record['version']==CODEX_VERSION and _binary_file(record['invocation_path'])==
                {k:record[k] for k in ('path','sha256','identity','invocation_path')},'pilot_binary_evidence_changed')
    if profile==CONFIG_TRIAL:
        require('desktop_app' not in value and 'desktop' not in value
                and value.get('client_compatibility_verified') is False
                and value.get('desktop_compatibility_verified') is False,'pilot_evidence_profile_mismatch')
        target=value.get('config_target')
        require(isinstance(target,dict) and target==config_target(target.get('codex_home')),
                'pilot_config_target_changed')
        require(value.get('catalog_adapter')==catalog_adapter(),'pilot_catalog_adapter_changed')
    elif profile==app_identity.TRIAL:
        require('config_target' not in value and 'catalog_adapter' not in value,'pilot_evidence_profile_mismatch')
        require(value.get('desktop_compatibility_verified') is False,'pilot_evidence_profile_mismatch')
        app_identity.check_application(value.get('desktop_app'))
    else:
        require('desktop_app' not in value and 'config_target' not in value and 'catalog_adapter' not in value,
                'pilot_evidence_profile_mismatch')
    require(_load(store,'versions',value['id'])==value,'pilot_version_evidence_changed')
    return value


def _live(store):
    info=probe(store.root);cfg=store.config();activation=store.activation();now=time.time()
    require(info.get('protocol')==PROTOCOL and info.get('bound') is True and info.get('controller_active') is True
            and info.get('controller_mode')=='native_google_v1' and info.get('activation_enabled') is True,
            'pilot_live_native_controller_required')
    require(info.get('generation')==activation['id'] and activation['enabled']==1 and activation['created']<=now<activation['expires']
            and info.get('catalog_path')==activation['catalog'] and info.get('selection')==strict_json(activation['selection'])
            and info.get('base_url')==f"http://127.0.0.1:{cfg['port']}/activations/{activation['id']}/v1",
            'pilot_activation_mismatch')
    require(read_private_file(activation['catalog'],2*1024*1024)==canonical(global_catalog(info['selection'])),
            'pilot_catalog_changed')
    with store.transaction() as db:
        controller=dict(store._live_controller(db))
        bound=db.execute("SELECT value FROM meta WHERE key='native_activation'").fetchone()
    require(controller['mode']=='native_google_v1' and bound is not None and strict_json(bound['value'])==activation['id'],
            'pilot_native_activation_mismatch')
    binding={'activation':activation,'controller':{k:controller[k] for k in ('controller_id','epoch','mode','expires')},
             'catalog_sha256':hash_bytes(canonical(global_catalog(info['selection']))),'base_url':info['base_url']}
    return info,binding,controller


def prepare_preflight(store,*,version_evidence,previous_plan_id=None):
    """Create a challenge; explicit refresh reuses only a known completed route.

    A refresh never submits, retries, reserves, or spawns. Its cumulative input
    extends the exact completed prior turn. Unknown prior outcomes block it.
    """
    store=_store(store);_versions(store,version_evidence);info,binding,controller=_live(store)
    now=time.time();identifier=secrets.token_hex(16);request_nonce=secrets.token_hex(24);expected_nonce=secrets.token_hex(24)
    require(request_nonce!=expected_nonce,'pilot_nonce_collision')
    identity={'session-id':'dots-pilot-'+identifier,'thread-id':str(uuid.uuid4())};history=[];route_created=None
    if previous_plan_id is not None:
        previous=_load(store,'plan',previous_plan_id)
        require(previous['binding']==binding,'pilot_activation_or_controller_changed')
        old_versions=_unseal(store,'versions',previous['version_evidence'])
        new_versions=_unseal(store,'versions',version_evidence)
        require(all(old_versions.get(k)==new_versions.get(k) for k in ('profile','binaries','desktop_app','config_target','catalog_adapter','package','parser')),
                'pilot_refresh_evidence_changed')
        route,_,prior=_request(store,previous,controller);cfg=store.config()
        with store.transaction() as db:
            require(db.execute("SELECT 1 FROM requests WHERE route=? AND state!='text_complete'",(route['id'],)).fetchone() is None,
                    'pilot_refresh_unknown_prior_outcome')
            latest=db.execute('SELECT digest FROM requests WHERE route=? ORDER BY created DESC,digest DESC LIMIT 1',
                              (route['id'],)).fetchone()
            require(latest is not None and latest['digest']==previous['request_digest'],'pilot_refresh_latest_completed_plan_required')
        limits=strict_json(route['admission'])['body']['payload']
        require(route['used']<min(cfg['max_requests'],limits['max_requests']),'pilot_refresh_request_budget_exhausted')
        identity=previous['identity'];route_created=route['created']
        history=copy.deepcopy(previous['body']['input'])+copy.deepcopy(prior['output'])
    body={'model':info['selection']['model'],'reasoning':{'effort':info['selection']['reasoning_effort']},
          'stream':True,'tools':[], 'input':history+[{'type':'message','role':'user','content':[{'type':'input_text',
          'text':'PILOT preflight challenge '+request_nonce+'. Reply with exactly this text and nothing else: '+expected_nonce}]}]}
    plan={'id':identifier,'created':now,'expires':min(now+PLAN_SECONDS,binding['activation']['expires'],controller['expires']),
          'binding':binding,'identity':identity,'body':body,'request_nonce':request_nonce,'expected_nonce':expected_nonce,
          'route_id':route_key(info['generation'],identity),'request_digest':hash_bytes(canonical(body)),
          'version_evidence':copy.deepcopy(version_evidence),'previous_plan_id':previous_plan_id,'route_created':route_created}
    if previous_plan_id is None:
        with store.transaction() as db:
            require(db.execute('SELECT 1 FROM routes WHERE id=?',(plan['route_id'],)).fetchone() is None,'pilot_route_already_exists')
    _save(store,'plan',plan)
    return {k:copy.deepcopy(plan[k]) for k in ('id','identity','body','request_nonce','expected_nonce','route_id','request_digest','expires')} | {'plan_id':identifier,'generation':info['generation']}


def _fresh_plan(store,identifier):
    plan=_load(store,'plan',identifier);now=time.time()
    require(plan['created']<=now<plan['expires'],'pilot_preflight_expired')
    _versions(store,plan['version_evidence']);info,binding,controller=_live(store)
    require(binding==plan['binding'],'pilot_activation_or_controller_changed')
    require(plan['request_digest']==hash_bytes(canonical(plan['body']))
            and plan['route_id']==route_key(info['generation'],plan['identity'])
            and plan['request_nonce']!=plan['expected_nonce'],'pilot_preflight_binding_mismatch')
    return plan,info,controller


def _request(store,plan,controller):
    now=time.time();route=store.route(plan['route_id']);cfg=store.config()
    require(route['state']=='ready' and route['generation']==plan['binding']['activation']['id']
            and route['controller_epoch']==controller['epoch'] and strict_json(route['identity'])==plan['identity']
            and strict_json(route['selection'])==strict_json(plan['binding']['activation']['selection']),
            'pilot_ready_route_binding_mismatch')
    route_created=plan.get('route_created')
    require((plan['created']<=route['created'] if route_created is None else route_created==route['created'])
            and route['created']<=now<route['expires'] and route['last_used']+cfg['idle_seconds']>now,
            'pilot_route_expired')
    endpoint(route['endpoint'])
    require(endpoint(route['endpoint'])!=cfg['port'],'pilot_gateway_recursion_rejected')
    pin=Object.parse(canonical(strict_json(route['admission'])));payload=pin.body['payload'];task=route['native_task']
    require(pin.body['kind']=='deployment' and pin.oid==route['pin'] and pin.body['identity']['session_id']==route['id']
            and pin.body['identity']['native_task_id']==task and pin_selection(pin)==strict_json(route['selection'])
            and payload['scope']=='responses_tools' and payload['created']<=now<payload['expires']
            and payload['expires']==route['expires'],'pilot_pin_binding_or_lifetime_mismatch')
    validate_admission(payload['inference']['admission'],strict_json(route['selection']),task)
    with store.transaction() as db:
        row=db.execute('SELECT * FROM requests WHERE route=? AND digest=?',(route['id'],plan['request_digest'])).fetchone()
    require(row is not None and row['state']=='text_complete' and row['response'] is not None,
            'pilot_successful_preflight_required')
    require(plan['created']<=row['created']<=now and row['created']<plan['expires'],'pilot_stale_preflight_response')
    require(isinstance(row['backend_response_id'],str) and re.fullmatch(r'resp_[A-Za-z0-9_-]{1,128}',row['backend_response_id']),
            'pilot_backend_response_identity_required')
    raw=bytes(row['response']);events=[]
    try:
        for frame in raw.split(b'\n\n'):
            if not frame:continue
            names=[line[7:] for line in frame.splitlines() if line.startswith(b'event: ')]
            data=[line[6:] for line in frame.splitlines() if line.startswith(b'data: ')]
            require(len(names)==len(data)==1,'pilot_invalid_response_stream')
            event=strict_json(data[0]);require(names[0]==event['type'].encode(),'pilot_invalid_response_stream');events.append(event)
        response_id='resp_gw_'+hash_bytes(canonical({'route':route['id'],'request':plan['request_digest']}))
        terminal=events[-1];response=terminal['response'];output=response['output']
        require(terminal['type']=='response.completed' and response['id']==response_id and response['status']=='completed'
                and len(output)==1 and output[0]['type']=='message' and output[0]['role']=='assistant'
                and len(output[0]['content'])==1 and output[0]['content'][0]['type']=='output_text',
                'pilot_text_completion_required')
        require(all(e.get('type')!='response.failed' and e.get('item',{}).get('type') not in ('function_call','custom_tool_call')
                    and ('response' not in e or e['response'].get('id')==response_id) for e in events),
                'pilot_invalid_response_stream')
        require(output[0]['content'][0]['text']==plan['expected_nonce'],'pilot_nonce_response_mismatch')
    except (KeyError,IndexError,TypeError,AttributeError):raise ProtocolError('pilot_invalid_response_stream') from None
    return route,pin,{'created':row['created'],'response_sha256':hash_bytes(raw),'backend_response_id':row['backend_response_id'],
                      'output':copy.deepcopy(output)}


def verify_preflight(store,plan_id,*,queue_state,join_code):
    """Seal proof only for the exact persisted completed challenge and signed JOIN."""
    store=_store(store);plan,info,controller=_fresh_plan(store,plan_id)
    state=queue.verify(queue_state,join_code);now=time.time();native=state['logical']['controller']
    require(not state['logical']['closed'] and native is not None and state['activation_id']==info['generation']
            and native['controller_epoch']==controller['epoch']
            and hash_bytes(native['native_task_id'].encode())[:32]==controller['controller_id']
            and 0<=now-native['heartbeat_at']<=30 and native['lease_expires']==controller['expires'],
            'pilot_signed_controller_mismatch')
    route,pin,request=_request(store,plan,controller);demand=state['logical']['demands'].get(route['id'])
    require(demand is not None and demand['state']=='ready' and demand['controller_epoch']==controller['epoch']
            and demand['identity_sha256']==hash_bytes(canonical(plan['identity']))
            and demand['selection']==strict_json(route['selection']) and demand['claim_id']==route['claim']
            and demand['admission']==pin.body['payload']['inference']['admission']
            and demand['ready']['deployment']==pin.oid,'pilot_signed_ready_route_mismatch')
    arguments=strict_json(route['spawn_arguments'])
    require(arguments.get('source')=='signed_global_queue' and arguments.get('task_name')=='global_'+route['id']
            and arguments.get('arguments_sha256')==demand['spawn_arguments_sha256'],'pilot_native_arguments_binding_mismatch')
    proof={'id':secrets.token_hex(16),'plan_id':plan_id,'created':now,
           'expires':min(now+PROOF_SECONDS,plan['expires'],state['expires'],route['expires'],controller['expires']),
           'binding':plan['binding'],'route':{k:route[k] for k in ('id','generation','identity','selection','version','controller_epoch','claim','native_task','pin','endpoint','admission','spawn_arguments','expires')},
           'request':request,'queue_root':queue.root_of(state),'queue_root_sha256':queue.root_hash(state),
           'queue_state_sha256':hash_bytes(canonical(state)),'ready':copy.deepcopy(demand['ready']),
           'ready_sha256':hash_bytes(canonical(demand['ready'])),'production_ready':False}
    _save(store,'proof',proof)
    return {'proof_id':proof['id'],'expires':proof['expires'],'route_id':route['id'],'generation':info['generation'],
            'native_task_id':route['native_task'],'request_digest':plan['request_digest'],'production_ready':False}


def require_pilot(state_dir,proof_id,cli_version,desktop_version,*,desktop_app=None,client_profile=None,codex_home=None):
    store=_store(state_dir);proof=_load(store,'proof',proof_id);now=time.time()
    require(proof['created']<=now<proof['expires'],'pilot_proof_expired')
    plan,info,controller=_fresh_plan(store,proof['plan_id'])
    versions=_versions(store,plan['version_evidence'])
    require(cli_version==CODEX_VERSION,'pilot_verified_cli_version_required')
    profile=versions.get('profile','strict-client-binaries/1')
    require(client_profile is None or (isinstance(client_profile,str) and client_profile==profile),
            'pilot_requested_profile_mismatch')
    if profile==CONFIG_TRIAL:
        require(client_profile==CONFIG_TRIAL and desktop_version is None and desktop_app is None,
                'pilot_config_trial_profile_required')
        require(codex_home is not None and config_target(codex_home)==versions['config_target'],
                'pilot_config_target_mismatch')
    elif profile==app_identity.TRIAL:
        require(desktop_version is None and desktop_app is not None
                and desktop_app==versions['desktop_app'],'pilot_trial_app_evidence_required')
    else:
        require(desktop_version==CODEX_VERSION and desktop_app is None,'pilot_both_verified_versions_required')
    require(proof['binding']==plan['binding'],'pilot_proof_binding_mismatch')
    queue.validate_root(proof['queue_root'])
    require(proof['queue_root_sha256']==hash_bytes(canonical(proof['queue_root']))
            and now<proof['queue_root']['expires'] and proof['ready_sha256']==hash_bytes(canonical(proof['ready'])),
            'pilot_queue_evidence_changed')
    route,pin,request=_request(store,plan,controller)
    require({k:route[k] for k in proof['route']}==proof['route'] and request==proof['request'],
            'pilot_completed_route_evidence_changed')
    return {**info,'pilot_proof_id':proof_id,'pilot_ready':True,'production_ready':False,'ready_for_config':False,
            'client_evidence_profile':profile,'desktop_compatibility_verified':False,
            'client_compatibility_verified':False,'config_target':versions.get('config_target'),
            'preflight_route_id':plan['route_id']}
