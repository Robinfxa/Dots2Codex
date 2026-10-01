"""Explicit, one-CODEX_HOME config preview/apply/restore transactions.

No auth.json, OAuth, keychain, system proxy, safety policy, profile or project
file is read or written. apply requires a freshly authenticated bound gateway
whose production controller reports ready. The prototype cannot meet that gate.
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
    'Desktop bundled CLI and terminal CLI versions require separate live verification.',
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
    return path


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
                and info.get('controller_mode')=='native_google_v1' and info.get('activation_enabled') is True,
                'production_gateway_not_ready')
    return info


def versions(cli_version,desktop_version=None):
    require(cli_version==CODEX_VERSION,'unsupported_or_unverified_cli_version')
    require(desktop_version is None or desktop_version==CODEX_VERSION,'unsupported_or_unverified_desktop_bundled_version')


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


def preview(state_dir,codex_home,*,cli_version,desktop_version=None,profile=None):
    versions(cli_version,desktop_version);home=known_home(codex_home);info=readiness(state_dir)
    before=snapshot(home/'config.toml');after=render_patch(before['raw'],info)
    warnings=list(WARNINGS)
    if desktop_version is None:warnings.append('Desktop bundled version not supplied: desktop coverage remains unverified.')
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
            'ready_for_config':False,'write_performed':False}


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
          before_commit=None,after_replace=None):
    require(confirm is True,'explicit_global_config_confirmation_required')
    versions(cli_version,desktop_version);home=known_home(codex_home)
    info=readiness(state_dir,live=True)  # Before any target-home mutation, including the lock.
    with home_lock(home):
        path=home/'config.toml';before=snapshot(path)
        require(before['hash']==expected_before_hash,'preview_outdated')
        after=render_patch(before['raw'],info)
        require(before['raw']!=after,'config_already_matches_no_transaction')
        directory=private_dir(Path(state_dir)/'config-transactions',create=True)
        # Outstanding transactions for this same target must be restored/reconciled first.
        for prior in directory.glob('*.json'):
            old=strict_json(read_private_file(prior,MAX_CONFIG_BYTES*3))
            require(old.get('config_path')!=str(path) or old.get('phase') in ('restored','aborted'),
                    'existing_config_transaction_requires_reconciliation')
        tid=secrets.token_hex(16);backup=directory/(tid+'.before');postimage=directory/(tid+'.after')
        private_write(backup,before['raw']);private_write(postimage,after)
        manifest={'contract':CONTRACT,'id':tid,'phase':'prepared','config_path':str(path),'codex_home':str(home),
                  'generation':info['generation'],'created':time.time(),'before_hash':before['hash'],
                  'after_hash':hash_bytes(after),'before_exists':before['exists'],'before_identity':before['identity'],
                  'before_mode':before['mode'],'backup':str(backup),'postimage':str(postimage),
                  'cli_version_evidence':cli_version,'desktop_version_evidence':desktop_version,
                  'restart_required':True,'live_route_observed':False}
        journal=directory/(tid+'.json');save_manifest(journal,manifest)
        written=commit(path,before,after,before_commit=before_commit,after_replace=after_replace)
        manifest.update(phase='committed',after_identity=written['identity']);save_manifest(journal,manifest)
        return {'transaction_id':tid,'phase':'committed','config_path':str(path),'restart_required':True,
                'live_route_observed':False,'auth_file_touched':False}


def load_transaction(state_dir,tid):
    path=manifest_path(state_dir,tid);value=strict_json(read_private_file(path,MAX_CONFIG_BYTES*3))
    require(value.get('contract')==CONTRACT and value.get('id')==tid,'invalid_config_transaction')
    directory=path.parent
    require(value.get('backup')==str(directory/(tid+'.before')) and value.get('postimage')==str(directory/(tid+'.after')),
            'transaction_file_binding_mismatch')
    home=known_home(value['codex_home']);require(value['config_path']==str(home/'config.toml'),'transaction_target_mismatch')
    before=read_private_file(value['backup'],MAX_CONFIG_BYTES);after=read_private_file(value['postimage'],MAX_CONFIG_BYTES)
    require(hash_bytes(before)==value['before_hash'] and hash_bytes(after)==value['after_hash'],'transaction_backup_hash_mismatch')
    return path,value,before,after


def reconcile(state_dir,tid):
    journal,value,before,after=load_transaction(state_dir,tid)
    with home_lock(Path(value['codex_home'])):
        current=snapshot(value['config_path'])
        if value['phase'] in ('restored','aborted'):return {'phase':value['phase'],'write_performed':False}
        require(value['phase'] in ('prepared','committed','restore_prepared'),'unknown_transaction_phase')
        if value['phase']=='restore_prepared':
            expected=value['restore_hash'];exists=value['restore_exists']
            require(current['exists']==exists and current['hash']==expected,'restore_outcome_requires_manual_reconciliation')
            value['phase']='restored';save_manifest(journal,value)
        elif current['exists'] and current['hash']==value['after_hash']:
            value.update(phase='committed',after_identity=current['identity']);save_manifest(journal,value)
        elif value['phase']=='prepared' and current['exists']==value['before_exists'] and current['hash']==value['before_hash']:
            value['phase']='aborted';save_manifest(journal,value)
        else:raise ProtocolError('config_outcome_requires_manual_reconciliation')
        return {'phase':value['phase'],'write_performed':False}


def restore_bytes(before,after,current):
    original_doc,original=decoded(before);_,ours=decoded(after);doc,present=decoded(current)
    for path in OWNED:
        require(value_at(present,path)==value_at(ours,path),'restore_owned_value_conflict')
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
        if value['phase']=='restored':return {'phase':'restored','write_performed':False,'restart_required':True}
        require(value['phase']=='committed','reconcile_transaction_before_restore')
        path=Path(value['config_path']);current=snapshot(path)
        require(current['exists'],'config_deleted_after_takeover')
        exact=current['raw']==after
        restored=before if exact else restore_bytes(before,after,current['raw'])
        delete=exact and not value['before_exists']
        value.update(phase='restore_prepared',restore_hash=hash_bytes(restored),restore_exists=not delete)
        save_manifest(journal,value)
        commit(path,current,restored,delete=delete,before_commit=before_commit,after_replace=after_replace)
        value['phase']='restored';save_manifest(journal,value)
        return {'phase':'restored','write_performed':True,'mode':'exact' if exact else 'three_way_owned_values',
                'restart_required':True,'running_clients_stopped':False,'catalogs_deleted':False}


def main():
    parser_cli=argparse.ArgumentParser(description=__doc__);sub=parser_cli.add_subparsers(dest='command',required=True)
    for name in ('preview','apply'):
        p=sub.add_parser(name);p.add_argument('--state-dir',required=True);p.add_argument('--codex-home',required=True)
        p.add_argument('--cli-version',required=True);p.add_argument('--desktop-version')
        if name=='preview':p.add_argument('--profile')
        else:p.add_argument('--expected-before-hash',required=True);p.add_argument('--confirm',action='store_true')
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
