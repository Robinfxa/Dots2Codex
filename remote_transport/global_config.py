"""Explicit, one-CODEX_HOME config preview/apply/restore transactions.

No auth.json, OAuth, keychain, system proxy, safety policy, profile or project
file is read or written. Default apply requires a freshly authenticated bound
gateway whose production controller reports ready. A separate explicit PILOT
proof permits an evidence-bound preview; it never changes production readiness.
Locks are cooperative; arbitrary external editors cannot be made atomic by an
os.replace compare-before-write sequence. Unknown races are never auto-retried.
"""
import argparse
import contextlib
import copy
import difflib
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import time
import tomllib

from .codex_catalog import CODEX_VERSION
from .global_gateway import (PROTOCOL, Store, global_catalog, private_dir,
                             private_write, probe, strict_json)
from .backend import fsync_dir, read_private_file
from .model import ProtocolError, canonical, hash_bytes, require
from .selection import validate_selection

CONTRACT='dots-global-config-transaction/1'
PROVIDER='dots2codex_global'
MAX_CONFIG_BYTES=1024*1024
TOP=('model_provider','model','model_reasoning_effort','model_catalog_json')
OWNED=tuple((k,) for k in TOP)+(('model_providers',PROVIDER),)
MISSING={'__dots_missing__':True}
WARNINGS=[
    'Only the explicit CODEX_HOME/config.toml is managed; auth.json and login are untouched.',
    'Restart each target client. Existing/resumed threads may retain their old provider.',
    'CLI -m/-c, selected profile files, permitted project model/effort and managed policy can override this file.',
    'The catalog is pinned to codex-cli 0.159.2; other local consumers require separate compatibility testing.',
    'Only clients reading this selected file may be affected; this does not establish ChatGPT Work or Cloud support.',
    'Terminal adapter compatibility and client compatibility are separate; neither app identity nor traffic is attestation.',
    'Gateway fail-closed behavior covers requests reaching it, not arbitrary client startup fallback.',
    'Locks are cooperative. A noncooperating editor can race the final compare and atomic replacement.',
]


def parser():
    try:
        import tomlkit
    except ImportError:
        raise ProtocolError('global_config_requires_optional_tomlkit_dependency') from None
    require(tomlkit.__version__=='0.13.3','unsupported_tomlkit_version')
    return tomlkit


def known_home(value):
    require(isinstance(value,(str,Path)) and str(value),'explicit_codex_home_required')
    path=Path(value).expanduser().absolute()
    require(not any(p.is_symlink() for p in [path,*path.parents]),'symlink_path_rejected')
    info=path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.getuid() and not info.st_mode&0o022,
            'unsafe_codex_home')
    # Validate the original traversal before collapsing aliases. In particular,
    # a symlink followed by '..' must not disappear before the safety check.
    return Path(os.path.normpath(str(path)))


def snapshot(path):
    path=Path(path)
    try:before=path.lstat()
    except FileNotFoundError:return {'exists':False,'raw':b'','hash':hash_bytes(b''),'identity':None,'mode':0o600}
    require(stat.S_ISREG(before.st_mode) and before.st_uid==os.getuid()
            and not before.st_mode&0o022 and before.st_nlink==1 and before.st_size<=MAX_CONFIG_BYTES,
            'unsafe_config_file')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        require((before.st_dev,before.st_ino)==(info.st_dev,info.st_ino),'config_changed_during_read')
        raw=os.read(fd,MAX_CONFIG_BYTES+1)
        after=os.fstat(fd)
        require(info.st_mtime_ns==after.st_mtime_ns and info.st_size==after.st_size
                and len(raw)==info.st_size,'config_changed_during_read')
    finally:os.close(fd)
    identity=[info.st_dev,info.st_ino,info.st_uid,info.st_mode,info.st_nlink,info.st_mtime_ns,info.st_ctime_ns]
    return {'exists':True,'raw':raw,'hash':hash_bytes(raw),'identity':identity,'mode':stat.S_IMODE(info.st_mode)}


def compare(path,expected):
    current=snapshot(path)
    require(current['exists']==expected['exists'] and current['hash']==expected['hash']
            and current['identity']==expected['identity'],'config_concurrent_edit')
    return current


def decoded(raw):
    try:
        text=raw.decode('utf-8');plain=tomllib.loads(text)
        doc=parser().parse(text)
    except ProtocolError:raise
    except Exception:raise ProtocolError('invalid_or_unsupported_config_toml') from None
    require(parser().dumps(doc)==text,'format_preserving_roundtrip_required')
    return doc,plain


def value_at(doc,path):
    node=doc
    for part in path:
        if not isinstance(node,dict) or part not in node:return copy.deepcopy(MISSING)
        node=node[part]
    return copy.deepcopy(node)


def set_at(doc,path,value):
    if len(path)==1:
        if value==MISSING:
            if path[0] in doc:del doc[path[0]]
        else:doc[path[0]]=copy.deepcopy(value)
    else:
        first,last=path
        if value==MISSING:
            if first in doc and last in doc[first]:del doc[first][last]
        else:
            if first not in doc:doc[first]=parser().table()
            doc[first][last]=copy.deepcopy(value)


def new_values(info):
    selected=validate_selection(info['selection'])
    return {'model_provider':PROVIDER,'model':selected['model'],'model_reasoning_effort':selected['reasoning_effort'],
            'model_catalog_json':info['catalog_path'],
            'model_providers':{PROVIDER:{'name':'Dots2Codex isolated global gateway','base_url':info['base_url'],
                'wire_api':'responses','requires_openai_auth':False,'supports_websockets':False,
                'request_max_retries':0,'stream_max_retries':0}}}


def readiness(state_dir,live=False):
    store=Store(state_dir);activation=store.activation();cfg=store.config()
    info=probe(state_dir) if live else store.status()
    require(info.get('protocol')==PROTOCOL and info.get('generation')==activation['id']
            and info.get('base_url')==f"http://127.0.0.1:{cfg['port']}/activations/{activation['id']}/v1"
            and info.get('catalog_path')==activation['catalog']
            and info.get('selection')==json.loads(activation['selection']), 'gateway_activation_mismatch')
    expected=canonical(global_catalog(info['selection']))
    require(read_private_file(info['catalog_path'],2*1024*1024)==expected,'global_catalog_changed')
    if live:
        require(info.get('bound') is True and info.get('production_ready') is True
                and info.get('ready_for_config') is True and info.get('controller_active') is True
                and info.get('controller_mode')=='native_google_v2' and info.get('activation_enabled') is True,
                'production_gateway_not_ready')
    return info


def versions(cli_version,desktop_version=None,desktop_app=None,client_profile=None):
    from .global_pilot import CONFIG_TRIAL
    require(client_profile is None or (isinstance(client_profile,str) and client_profile==CONFIG_TRIAL),
            'unsupported_client_evidence_profile')
    if client_profile==CONFIG_TRIAL:
        require(desktop_version is None and desktop_app is None,'config_trial_cannot_assert_app_evidence')
    require(cli_version==CODEX_VERSION,'unsupported_or_unverified_cli_version')
    require(desktop_version is None or desktop_version==CODEX_VERSION,'unsupported_or_unverified_desktop_bundled_version')
    if desktop_app is not None:
        from .codex_desktop import check_application
        require(desktop_version is None,'desktop_app_trial_cannot_assert_engine_version')
        check_application(desktop_app)


def render_patch(raw,info):
    doc,plain=decoded(raw);desired=new_values(info)
    providers=plain.get('model_providers',{})
    require(isinstance(providers,dict),'unsupported_provider_table')
    existing=providers.get(PROVIDER)
    require(existing is None or isinstance(existing,dict),'unsupported_provider_table')
    # Never replace auth or unknown extensions hidden in an existing same-ID table.
    require(existing is None or set(existing)<=set(desired['model_providers'][PROVIDER]),'existing_owned_provider_has_unmanaged_fields')
    for path in OWNED:set_at(doc,path,value_at(desired,path))
    after=parser().dumps(doc)
    if '\r\n' in raw.decode('utf-8'):
        require('\n' not in raw.decode('utf-8').replace('\r\n',''),'mixed_config_newlines_not_supported')
        after=after.replace('\r\n','\n').replace('\n','\r\n')
    result=after.encode('utf-8');parsed=tomllib.loads(after)
    expected=copy.deepcopy(plain)
    for path in OWNED:
        if len(path)==1:expected[path[0]]=value_at(desired,path)
        else:expected.setdefault(path[0],{})[path[1]]=value_at(desired,path)
    require(parsed==expected,'config_semantics_changed_outside_owned_patch')
    require(len(result)<=MAX_CONFIG_BYTES,'config_too_large')
    return result


def preview(state_dir,codex_home,*,cli_version,desktop_version=None,desktop_app=None,profile=None,client_profile=None):
    versions(cli_version,desktop_version,desktop_app,client_profile);home=known_home(codex_home);info=readiness(state_dir)
    before=snapshot(home/'config.toml');after=render_patch(before['raw'],info)
    warnings=list(WARNINGS)
    if client_profile is not None:
        warnings.append('Config-first local client trial: no installed application is required or verified. '
                        'Fully quit/reopen the actual consumer and its retained app-server or managed daemon when applicable; '
                        'start a new thread and restore if unsupported.')
    elif desktop_app is not None:
        warnings.append('Desktop config compatibility trial for '+desktop_app['path']+' (app '+desktop_app['app_version']+'). '
                        'Engine/catalog compatibility is unverified. Restart, create a new thread, and inspect its route; restore if unsupported.')
    elif desktop_version is None:warnings.append('Desktop bundled version not supplied: desktop coverage remains unverified.')
    if profile is not None:
        require(isinstance(profile,str) and profile and '/' not in profile and '\\' not in profile and profile not in ('.','..'),
                'invalid_profile_name')
        profile_path=home/(profile+'.config.toml')
        warnings.append('Selected profile may override this patch: '+str(profile_path))
    # Do not print raw TOML lines: an inline table may also contain unrelated
    # authentication headers. Show only owned paths and our new non-secret values.
    _,plain=decoded(before['raw']);desired=new_values(info)
    changes=[]
    for path in OWNED:
        current=value_at(plain,path);new=value_at(desired,path)
        if current!=new:
            label='.'.join(path)
            prior='<absent>' if current==MISSING else '<existing value hidden>'
            changes.append(label+': '+prior+' -> '+json.dumps(new,ensure_ascii=False))
    diff='\n'.join(changes)+'\n'
    return {'contract':CONTRACT,'config_path':str(home/'config.toml'),'generation':info['generation'],
            'before_hash':before['hash'],'before_exists':before['exists'],'after_hash':hash_bytes(after),
            'diff':diff,'diff_kind':'owned_values_redacted','warnings':warnings,'changes':bool(before['raw']!=after),
            'ready_for_config':False,'write_performed':False,'client_evidence_profile':client_profile,
            'client_compatibility_verified':False}


@contextlib.contextmanager
def home_lock(home):
    path=home/'.dots2codex-global.lock'
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and not info.st_mode&0o077
                and info.st_nlink==1,'unsafe_config_lock')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);yield
    except BlockingIOError:raise ProtocolError('config_transaction_in_progress') from None
    finally:os.close(fd)


def manifest_path(state_dir,transaction_id):
    require(isinstance(transaction_id,str) and len(transaction_id)==32 and all(c in '0123456789abcdef' for c in transaction_id),
            'invalid_transaction_id')
    return private_dir(Path(state_dir)/'config-transactions')/(transaction_id+'.json')


def save_manifest(path,value):private_write(path,canonical(value))


def commit(path,expected,raw,*,delete=False,before_commit=None,after_replace=None):
    path=Path(path);tmp=path.parent/('.dots2codex-config-'+secrets.token_hex(12))
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,expected['mode'])
    try:
        with os.fdopen(fd,'wb') as output:
            os.fchmod(output.fileno(),expected['mode']);output.write(raw);output.flush();os.fsync(output.fileno())
        if before_commit:before_commit()
        compare(path,expected)
        if delete:
            path.unlink();tmp.unlink()
        else:os.replace(tmp,path)
        fsync_dir(path.parent)
        if after_replace:after_replace()
        current=snapshot(path)
        require(current['exists']==(not delete) and (delete or current['raw']==raw),'config_write_outcome_unknown')
        return current
    finally:
        if tmp.exists():tmp.unlink()


def apply(state_dir,codex_home,*,cli_version,expected_before_hash,confirm=False,desktop_version=None,
          pilot_proof_id=None,expected_after_hash=None,before_commit=None,after_replace=None,desktop_app=None,client_profile=None):
    require(confirm is True,'explicit_global_config_confirmation_required')
    versions(cli_version,desktop_version,desktop_app,client_profile);home=known_home(codex_home)
    # Production readiness is unchanged. The separate, short-lived PILOT proof
    # is evidence-bound, never a caller-supplied readiness boolean.
    def gate():
        if pilot_proof_id is None:
            require(desktop_app is None,'desktop_app_trial_requires_pilot_proof')
            require(client_profile is None,'config_trial_requires_pilot_proof')
            return readiness(state_dir,live=True)
        from .global_pilot import require_pilot
        require(isinstance(expected_after_hash,str) and len(expected_after_hash)==64,
                'pilot_exact_preview_confirmation_required')
        return require_pilot(state_dir,pilot_proof_id,cli_version,desktop_version,desktop_app=desktop_app,
                             client_profile=client_profile,codex_home=home)
    info=gate()  # Before any target-home mutation, including the lock.
    with home_lock(home):
        path=home/'config.toml';before=snapshot(path)
        require(before['hash']==expected_before_hash,'preview_outdated')
        after=render_patch(before['raw'],info)
        if pilot_proof_id is not None:
            require(hash_bytes(after)==expected_after_hash,'preview_outdated')
        require(before['raw']!=after,'config_already_matches_no_transaction')
        directory=private_dir(Path(state_dir)/'config-transactions',create=True)
        # Outstanding transactions for this same target must be restored/reconciled first.
        for prior in directory.glob('*.json'):
            old=strict_json(read_private_file(prior,MAX_CONFIG_BYTES*3))
            require(isinstance(old.get('config_path'),str),'invalid_config_transaction')
            recorded=Path(os.path.normpath(old['config_path'])).absolute()
            require(recorded!=path or old.get('phase') in ('restored','aborted'),
                    'existing_config_transaction_requires_reconciliation')
        tid=secrets.token_hex(16);backup=directory/(tid+'.before');postimage=directory/(tid+'.after')
        private_write(backup,before['raw']);private_write(postimage,after)
        manifest={'contract':CONTRACT,'id':tid,'phase':'prepared','config_path':str(path),'codex_home':str(home),
                  'generation':info['generation'],'created':time.time(),'before_hash':before['hash'],
                  'after_hash':hash_bytes(after),'before_exists':before['exists'],'before_identity':before['identity'],
                  'before_mode':before['mode'],'backup':str(backup),'postimage':str(postimage),
                  'cli_version_evidence':cli_version,'desktop_version_evidence':desktop_version,
                  'desktop_app_evidence':desktop_app,'client_evidence_profile':info.get('client_evidence_profile'),
                  'desktop_compatibility_verified':False,'client_compatibility_verified':False,
                  'config_target_evidence':info.get('config_target'),'preflight_route_id':info.get('preflight_route_id'),
                  'restart_required':True,'live_route_observed':pilot_proof_id is not None,
                  'live_route_observed_scope':'backend_preflight' if pilot_proof_id is not None else None,
                  'pilot_proof_id':pilot_proof_id,'production_ready':False}
        journal=directory/(tid+'.json');save_manifest(journal,manifest)
        def final_check():
            if before_commit:before_commit()
            fresh=gate()
            require(all(fresh.get(k)==info.get(k) for k in ('generation','base_url','catalog_path','selection')),
                    'gateway_activation_mismatch')
        written=commit(path,before,after,before_commit=final_check,after_replace=after_replace)
        manifest.update(phase='committed',after_identity=written['identity'],committed_at=time.time());save_manifest(journal,manifest)
        return {'transaction_id':tid,'phase':'committed','config_path':str(path),'restart_required':True,
                'live_route_observed':pilot_proof_id is not None,'auth_file_touched':False,
                'live_route_observed_scope':'backend_preflight' if pilot_proof_id is not None else None,
                'pilot_proof_id':pilot_proof_id,'production_ready':False,
                'client_evidence_profile':info.get('client_evidence_profile'),'desktop_compatibility_verified':False,
                'client_compatibility_verified':False}


def check_transaction_target(value):
    """A config-first backup belongs to its original directory, not its name."""
    from .global_pilot import CONFIG_TRIAL, config_target
    if value.get('client_evidence_profile')==CONFIG_TRIAL:
        require(config_target(value['codex_home'])==value.get('config_target_evidence'),
                'config_transaction_target_changed')


def load_transaction(state_dir,tid):
    path=manifest_path(state_dir,tid);value=strict_json(read_private_file(path,MAX_CONFIG_BYTES*3))
    require(value.get('contract')==CONTRACT and value.get('id')==tid,'invalid_config_transaction')
    directory=path.parent
    require(value.get('backup')==str(directory/(tid+'.before')) and value.get('postimage')==str(directory/(tid+'.after')),
            'transaction_file_binding_mismatch')
    home=known_home(value['codex_home']);recorded=Path(value['config_path'])
    require(recorded.name=='config.toml' and known_home(recorded.parent)==home,'transaction_target_mismatch')
    # Old journals may contain a harmless lexical alias. Reconcile and restore
    # them using the same canonical target as new transactions, without changing
    # their proof, backup hashes or original-directory identity requirement.
    value.update(codex_home=str(home),config_path=str(home/'config.toml'))
    check_transaction_target(value)
    before=read_private_file(value['backup'],MAX_CONFIG_BYTES);after=read_private_file(value['postimage'],MAX_CONFIG_BYTES)
    require(hash_bytes(before)==value['before_hash'] and hash_bytes(after)==value['after_hash'],'transaction_backup_hash_mismatch')
    return path,value,before,after


def reconcile(state_dir,tid):
    journal,value,before,after=load_transaction(state_dir,tid)
    with home_lock(Path(value['codex_home'])):
        check_transaction_target(value)
        current=snapshot(value['config_path'])
        if value['phase'] in ('restored','aborted'):return {'phase':value['phase'],'write_performed':False}
        require(value['phase'] in ('prepared','committed','restore_prepared'),'unknown_transaction_phase')
        if value['phase']=='restore_prepared':
            expected=value['restore_hash'];exists=value['restore_exists']
            require(current['exists']==exists and current['hash']==expected,'restore_outcome_requires_manual_reconciliation')
            check_transaction_target(value)
            value['phase']='restored';save_manifest(journal,value)
        elif current['exists'] and current['hash']==value['after_hash']:
            check_transaction_target(value)
            value.update(phase='committed',after_identity=current['identity']);save_manifest(journal,value)
        elif value['phase']=='prepared' and current['exists']==value['before_exists'] and current['hash']==value['before_hash']:
            check_transaction_target(value)
            value['phase']='aborted';save_manifest(journal,value)
        else:raise ProtocolError('config_outcome_requires_manual_reconciliation')
        return {'phase':value['phase'],'write_performed':False}


def owned_node_bytes(doc,path):
    """Include syntax and trivia: equal TOML values do not imply safe removal."""
    node=doc;keys=[]
    for part in path:
        if part not in node:return None
        container=node if hasattr(node,'body') else getattr(node,'value',None)
        body=getattr(container,'body',())
        matches=[key for key,item in body if key is not None and getattr(key,'key',None)==part]
        # Ambiguous/out-of-order syntax must not silently discard user trivia.
        require(len(matches)<=1,'restore_owned_syntax_unsupported')
        keys.append((matches[0].as_string(),getattr(matches[0],'sep',None)) if matches else part)
        node=node[part]
    trivia=getattr(node,'trivia',None)
    return (keys,node.as_string(),tuple(getattr(trivia,k,None) for k in ('indent','comment_ws','comment','trail')))


def restore_bytes(before,after,current):
    original_doc,original=decoded(before);after_doc,ours=decoded(after);doc,present=decoded(current)
    for path in OWNED:
        require(value_at(present,path)==value_at(ours,path),'restore_owned_value_conflict')
        require(owned_node_bytes(doc,path)==owned_node_bytes(after_doc,path),'restore_owned_syntax_conflict')
    for path in OWNED:
        # Copy original TOML nodes rather than reconstructing strings or comments.
        node=original_doc
        for part in path:
            if part not in node:node=MISSING;break
            node=node[part]
        set_at(doc,path,node)
    # Remove a parent table created solely by us if it remains empty and contains
    # no comments. Otherwise retain it, preserving any unrelated user's text.
    if 'model_providers' not in original and 'model_providers' in doc:
        table=doc['model_providers']
        if not table and '#' not in table.as_string():del doc['model_providers']
    raw=parser().dumps(doc).encode('utf-8')
    expected=copy.deepcopy(present)
    for path in OWNED:
        old=value_at(original,path)
        if len(path)==1:
            if old==MISSING:expected.pop(path[0],None)
            else:expected[path[0]]=old
        elif old==MISSING:
            expected.get(path[0],{}).pop(path[1],None)
            if not expected.get(path[0]) and path[0] not in original:expected.pop(path[0],None)
        else:expected.setdefault(path[0],{})[path[1]]=old
    parsed=tomllib.loads(raw.decode('utf-8'))
    if parsed.get('model_providers')=={} and 'model_providers' not in expected:expected['model_providers']={}
    require(parsed==expected,'restore_semantics_changed_outside_owned_patch')
    return raw


def restore(state_dir,tid,*,confirm=False,before_commit=None,after_replace=None):
    require(confirm is True,'explicit_restore_confirmation_required')
    journal,value,before,after=load_transaction(state_dir,tid)
    with home_lock(Path(value['codex_home'])):
        check_transaction_target(value)
        if value['phase']=='restored':return {'phase':'restored','write_performed':False,'restart_required':True}
        require(value['phase']=='committed','reconcile_transaction_before_restore')
        path=Path(value['config_path']);current=snapshot(path)
        require(current['exists'],'config_deleted_after_takeover')
        exact=current['raw']==after
        restored=before if exact else restore_bytes(before,after,current['raw'])
        delete=exact and not value['before_exists']
        value.update(phase='restore_prepared',restore_hash=hash_bytes(restored),restore_exists=not delete)
        check_transaction_target(value)
        save_manifest(journal,value)
        def final_target_check():
            if before_commit:before_commit()
            check_transaction_target(value)
        commit(path,current,restored,delete=delete,before_commit=final_target_check,after_replace=after_replace)
        value['phase']='restored';save_manifest(journal,value)
        return {'phase':'restored','write_performed':True,'mode':'exact' if exact else 'three_way_owned_values',
                'restart_required':True,'running_clients_stopped':False,'catalogs_deleted':False}


def main():
    parser_cli=argparse.ArgumentParser(description=__doc__);sub=parser_cli.add_subparsers(dest='command',required=True)
    for name in ('preview','apply'):
        p=sub.add_parser(name);p.add_argument('--state-dir',required=True);p.add_argument('--codex-home',required=True)
        p.add_argument('--cli-version',required=True);p.add_argument('--desktop-version')
        p.add_argument('--client-profile',choices=['client-config-trial/1'])
        if name=='preview':p.add_argument('--profile')
        else:
            p.add_argument('--expected-before-hash',required=True);p.add_argument('--confirm',action='store_true')
            p.add_argument('--pilot-proof-id');p.add_argument('--expected-after-hash')
    for name in ('restore','reconcile'):
        p=sub.add_parser(name);p.add_argument('--state-dir',required=True);p.add_argument('--transaction-id',required=True)
        if name=='restore':p.add_argument('--confirm',action='store_true')
    args=vars(parser_cli.parse_args());command=args.pop('command')
    if command in ('restore','reconcile'):args['tid']=args.pop('transaction_id')
    print(json.dumps(globals()[command](**args),ensure_ascii=False))

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'error':str(exc) if isinstance(exc,ProtocolError) else type(exc).__name__}));raise SystemExit(1)
