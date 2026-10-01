"""Bounded, loopback-only multi-thread routing prototype.

This process never invokes native inference. A trusted controller must claim an
admission, durably reserve its one spawn attempt, observe actual platform
admission and attach a DIFFERENT verified long-session facade for each route.
The native Google queue adapter has offline integration tests. Production Mac,
connector and native-tool acceptance are still unverified; no auto-wake exists.
"""
import argparse
import contextlib
import fcntl
import hashlib
import hmac
import http.client
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import socket
import select as socket_select
import sqlite3
import stat
import threading
import time
from urllib.parse import urlsplit

from .backend import fsync_dir, read_private_file
from .codex_catalog import catalog_for_selection
from .global_timing import controller_active, TIMING
from .model import Object, ProtocolError, canonical, hash_bytes, require, MAX_WIRE_BYTES
from .selection import (load_catalog, select, validate_selection, validate_request_selection,
                        validate_admission, spawn_arguments, pin_selection)

PROTOCOL = 'dots-global-gateway/1'
CONTROL_PROTOCOL = 'dots-global-controller/2'
DEFAULT_PORT = 43187
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
UUID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
HEX = r'[0-9a-f]{32}'
ACTIVE = ('claimed', 'spawn_intent', 'admitted', 'ready', 'unknown', 'idle_expired')


def private_dir(path, create=False):
    path = Path(path).expanduser().absolute()
    # Reject symlink traversal, including parents. Never silently resolve a link.
    require(not any(p.is_symlink() for p in [path, *path.parents]), 'symlink_path_rejected')
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077, 'private_directory_required')
    return path


def private_write(path, data):
    path = Path(path)
    private_dir(path.parent)
    temp = path.parent / ('.write-' + secrets.token_hex(12))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data); out.flush(); os.fsync(out.fileno())
        require(not path.is_symlink(), 'symlink_path_rejected')
        os.replace(temp, path); fsync_dir(path.parent)
    finally:
        if temp.exists(): temp.unlink()


def strict_json(raw):
    def pairs(items):
        value = {}
        for k, v in items:
            require(k not in value, 'duplicate_json_key'); value[k] = v
        return value
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError('invalid_json')))
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError('invalid_json') from None


def route_key(generation, identity):
    return hash_bytes(canonical({'generation': generation, 'identity': identity}))[:32]


def global_catalog(default):
    """All reviewed native pairs; metadata never authorizes a backend route."""
    default = validate_selection(default); models = []
    for model, capabilities in load_catalog()['models'].items():
        efforts = capabilities['bridge_efforts']
        preferred = default['reasoning_effort'] if default['reasoning_effort'] in efforts else efforts[0]
        row = catalog_for_selection(select(load_catalog(), model, preferred))['models'][0]
        row['description'] = 'New threads only; first request pins an isolated native admission. No hot switching.'
        row['supported_reasoning_levels'] = [{'effort': e, 'description': 'Requires exact native admission'} for e in efforts]
        models.append(row)
    return {'models': models}


def endpoint(value):
    require(isinstance(value, str), 'invalid_loopback_endpoint')
    parsed = urlsplit(value)
    try: port = parsed.port
    except ValueError: raise ProtocolError('invalid_loopback_endpoint') from None
    require(parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'
            and parsed.netloc == f'127.0.0.1:{port}' and type(port) is int and 1 <= port <= 65535
            and parsed.path == '/v1' and not parsed.query and not parsed.fragment,
            'invalid_loopback_endpoint')
    return port


class Store:
    """Private SQLite transactions fence admission and forwarding before effects.

    SQLite CAS protects our own participants. It is not a distributed Google CAS
    and cannot authenticate external connector payloads; that future adapter must
    verify the signed JOIN and exact Docs revision before invoking this interface.
    """
    def __init__(self, root):
        self.root = private_dir(root)
        self.db = self.root / 'gateway.sqlite3'
        read_private_file(self.db, 64 * 1024 * 1024)
        self.key = read_private_file(self.root / 'controller.key', 128).decode('ascii')
        require(re.fullmatch(r'[0-9a-f]{64}', self.key), 'invalid_controller_key')

    @classmethod
    def initialize(cls, root, selection, *, port=DEFAULT_PORT, seconds=14400,
                   max_routes=16, max_pending=4, max_children=2, max_requests=128, idle_seconds=1800):
        validate_selection(selection)
        require(type(port) is int and 1024 <= port <= 65535, 'stable_unprivileged_port_required')
        require(type(seconds) is int and 30 <= seconds <= 28800 and type(idle_seconds) is int
                and 1 <= idle_seconds <= seconds, 'invalid_gateway_lifetime')
        require(all(type(v) is int and 1 <= v <= n for v,n in
                    [(max_routes,64),(max_pending,16),(max_children,6),(max_requests,128)]), 'invalid_gateway_limits')
        root = private_dir(root, create=True)
        # A complete initialized state is never silently replaced or re-keyed.
        lock = os.open(root/'initialize.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            require(not (root/'gateway.sqlite3').exists() and not (root/'controller.key').exists(),
                    'gateway_state_already_exists_or_incomplete')
            private_write(root/'controller.key', secrets.token_hex(32).encode('ascii'))
            fd = os.open(root/'gateway.sqlite3', os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600); os.close(fd)
            db = sqlite3.connect(root/'gateway.sqlite3')
            db.executescript('''
            PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE activations(id TEXT PRIMARY KEY, selection TEXT NOT NULL, catalog TEXT NOT NULL,
              created REAL NOT NULL, expires REAL NOT NULL, enabled INTEGER NOT NULL);
            CREATE TABLE controller(id INTEGER PRIMARY KEY CHECK(id=1), controller_id TEXT NOT NULL,
              epoch TEXT NOT NULL, mode TEXT NOT NULL, expires REAL NOT NULL, heartbeat REAL NOT NULL,
              capacity INTEGER NOT NULL);
            CREATE TABLE routes(id TEXT PRIMARY KEY, generation TEXT NOT NULL, identity TEXT NOT NULL,
              selection TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL, created REAL NOT NULL,
              last_used REAL NOT NULL, expires REAL NOT NULL, controller_epoch TEXT, claim TEXT,
              native_task TEXT UNIQUE, pin TEXT UNIQUE, endpoint TEXT UNIQUE, admission TEXT, spawn_arguments TEXT,
              used INTEGER NOT NULL DEFAULT 0, stopped_evidence TEXT);
            CREATE TABLE requests(route TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL,
              created REAL NOT NULL, response BLOB, backend_response_id TEXT, PRIMARY KEY(route,digest));
            ''')
            config = {'protocol':PROTOCOL,'port':port,'max_routes':max_routes,'max_pending':max_pending,
                      'max_children':max_children,'max_requests':max_requests,'idle_seconds':idle_seconds}
            db.executemany('INSERT INTO meta VALUES(?,?)', [(k,json.dumps(v)) for k,v in config.items()])
            db.commit(); db.close(); fsync_dir(root)
        finally: os.close(lock)
        result = cls(root); generation = result.activate(selection, seconds=seconds)
        return result, generation

    @contextlib.contextmanager
    def transaction(self):
        require(not self.db.is_symlink(), 'symlink_path_rejected')
        conn = sqlite3.connect(self.db, timeout=3, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA synchronous=FULL'); conn.execute('BEGIN IMMEDIATE')
        try:
            yield conn; conn.execute('COMMIT')
        except BaseException:
            conn.execute('ROLLBACK'); raise
        finally: conn.close()

    def config(self):
        with self.transaction() as db: return {r['key']:json.loads(r['value']) for r in db.execute('SELECT * FROM meta')}

    def activate(self, selection, *, seconds=14400):
        validate_selection(selection)
        require(type(seconds) is int and 30 <= seconds <= 28800, 'invalid_gateway_lifetime')
        generation = secrets.token_hex(16)
        directory = private_dir(self.root/'catalogs', create=True)
        path = directory / (generation + '.json')
        private_write(path, canonical(global_catalog(selection)))
        with self.transaction() as db:
            require(db.execute('SELECT COUNT(*) FROM activations').fetchone()[0] < 16, 'activation_archive_limit')
            now = time.time()
            db.execute('INSERT INTO activations VALUES(?,?,?,?,?,1)',
                       (generation,json.dumps(selection),str(path),now,now+seconds))
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('current_generation',json.dumps(generation)))
        return generation

    def activation(self, generation=None):
        generation = generation or self.config()['current_generation']
        with self.transaction() as db:
            row = db.execute('SELECT * FROM activations WHERE id=?',(generation,)).fetchone()
            require(row is not None, 'unknown_activation')
            return dict(row)

    def disable(self, generation):
        with self.transaction() as db:
            require(db.execute('UPDATE activations SET enabled=0 WHERE id=?',(generation,)).rowcount == 1,
                    'unknown_activation')
        return {'disabled':True,'existing_routes_retained':True,'clients_restarted':False}

    def pause_docs_reads(self, generation, token):
        """Local admission fence only; never renew or erase signed liveness."""
        with self.transaction() as db:
            require(db.execute('SELECT id FROM activations WHERE id=?',(generation,)).fetchone() is not None,
                    'unknown_activation')
            prior=db.execute("SELECT value FROM meta WHERE key='docs_read_pause'").fetchone()
            value={'generation':generation,'token':token}
            require(prior is None or json.loads(prior['value'])==value,'global_docs_read_pause_owner_mismatch')
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('docs_read_pause',json.dumps(value)))

    def resume_docs_reads(self, generation, token):
        with self.transaction() as db:
            prior=db.execute("SELECT value FROM meta WHERE key='docs_read_pause'").fetchone()
            require(prior is not None and json.loads(prior['value'])=={'generation':generation,'token':token},
                    'global_docs_read_pause_owner_mismatch')
            db.execute("DELETE FROM meta WHERE key='docs_read_pause'")

    def _docs_reads_paused(self, db):
        return db.execute("SELECT 1 FROM meta WHERE key='docs_read_pause'").fetchone() is not None

    def require_read_recovery_current(self):
        """Latch expired/rollback native evidence even while transport is paused.

        An initial JOIN may still await its first heartbeat; that is not expiry.
        Nothing here changes a heartbeat, lease, or the activation deadline.
        """
        with self.transaction() as db:
            row=db.execute('SELECT * FROM controller WHERE id=1').fetchone()
            if row is None or row['mode']!='native_google_v2':return
            self._controller_alive(db,row,time.time())
            checkpoint=json.loads(db.execute("SELECT value FROM meta WHERE key='native_controller_checkpoint'").fetchone()['value'])
            fence=self.controller_fence(checkpoint['root_hash'],checkpoint['epoch'])
            require(fence is None,(fence or {}).get('reason','global_controller_not_active_restart_required'))
            require(not checkpoint.get('terminal_reason'),checkpoint.get('terminal_reason') or 'global_controller_not_active_restart_required')

    def controller_fence(self,root_hash,epoch):
        path=self.root/'native-controller-fence.json'
        if not path.exists():return None
        value=strict_json(read_private_file(path,8192))
        require(value.get('root_hash')==root_hash and value.get('epoch')==epoch,
                'global_controller_fence_binding_mismatch')
        return value

    def controller_observed_at(self,root_hash,epoch):
        path=self.root/'native-controller-clock.json'
        if not path.exists():return None
        value=strict_json(read_private_file(path,8192))
        require(value.get('root_hash')==root_hash and value.get('epoch')==epoch,
                'global_controller_clock_binding_mismatch')
        return value['at']

    def _controller_alive(self, db, row, now):
        if row is None:return False
        if row['mode']=='offline_fixture':
            return row['expires']>now and 0<=now-row['heartbeat']<30
        if row['mode']!='native_google_v2':return False
        item=db.execute("SELECT value FROM meta WHERE key='native_controller_checkpoint'").fetchone()
        if item is None:return False
        checkpoint=json.loads(item['value'])
        if self.controller_fence(checkpoint['root_hash'],checkpoint['epoch']):return False
        reason=None
        highwater=self.controller_observed_at(checkpoint['root_hash'],checkpoint['epoch'])
        if highwater is not None and now<highwater:reason='global_controller_clock_rollback'
        if now<checkpoint.get('observed_at',now+1):reason='global_controller_clock_rollback'
        elif not controller_active(checkpoint['heartbeat'],checkpoint['expires'],checkpoint['timing'],now):
            reason='global_controller_not_active_restart_required'
        if reason:
            # Separate durable fence survives a caller's rejected SQLite transaction.
            private_write(self.root/'native-controller-fence.json',canonical({
                'root_hash':checkpoint['root_hash'],'epoch':checkpoint['epoch'],'reason':reason,'at':now}))
            return False
        private_write(self.root/'native-controller-clock.json',canonical({
            'root_hash':checkpoint['root_hash'],'epoch':checkpoint['epoch'],'at':now}))
        if (checkpoint.get('terminal_reason')
                or checkpoint.get('controller_id')!=row['controller_id']
                or checkpoint.get('epoch')!=row['epoch'] or checkpoint.get('heartbeat')!=row['heartbeat']
                or checkpoint.get('expires')!=row['expires'] or checkpoint.get('timing')!=TIMING
                or row['heartbeat']<=checkpoint.get('joined_at',row['heartbeat'])):return False
        return controller_active(row['heartbeat'],row['expires'],checkpoint['timing'],now)

    def _live_controller(self, db):
        row = db.execute('SELECT * FROM controller WHERE id=1').fetchone()
        require(self._controller_alive(db,row,time.time()),'router_controller_not_ready')
        return row

    def join(self, payload):
        require(set(payload)=={'controller_id','mode','seconds','capacity'}
                and isinstance(payload['controller_id'],str) and re.fullmatch(HEX,payload['controller_id']), 'invalid_controller_join')
        # Deliberately fail closed until a reviewed, authenticated Google adapter exists.
        require(payload['mode']=='offline_fixture', 'native_controller_adapter_not_implemented')
        cfg=self.config(); seconds=payload['seconds']; capacity=payload['capacity']
        require(type(seconds) is int and 30<=seconds<=28800 and type(capacity) is int
                and 1<=capacity<=cfg['max_children'], 'invalid_controller_limits')
        with self.transaction() as db:
            old=db.execute('SELECT * FROM controller WHERE id=1').fetchone()
            require(old is None or old['expires']<=time.time(), 'controller_session_already_exists')
            # An expired controller does not release possibly-running child slots.
            now=time.time(); epoch=secrets.token_hex(16)
            db.execute('INSERT OR REPLACE INTO controller VALUES(1,?,?,?,?,?,?)',
                       (payload['controller_id'],epoch,payload['mode'],now+seconds,now,capacity))
        return {'protocol':CONTROL_PROTOCOL,'epoch':epoch,'expires':now+seconds,'automatic_wake':False,
                'mode':'offline_fixture','native_inference_verified':False}

    def _controller(self, db, payload):
        row=self._live_controller(db)
        require(payload.get('controller_id')==row['controller_id'] and payload.get('epoch')==row['epoch'],
                'controller_identity_mismatch')
        return row

    def heartbeat(self, payload):
        require(set(payload)=={'controller_id','epoch'}, 'invalid_control_payload')
        with self.transaction() as db:
            row=self._controller(db,payload)
            require(row['mode']=='offline_fixture','signed_google_heartbeat_required')
            db.execute('UPDATE controller SET heartbeat=? WHERE id=1',(time.time(),))
        return {'active':True,'expires':row['expires'],'automatic_wake':False}

    def admission(self, generation, identity, selection):
        validate_selection(selection); rid=route_key(generation,identity); cfg=self.config()
        with self.transaction() as db:
            activation=db.execute('SELECT * FROM activations WHERE id=?',(generation,)).fetchone()
            require(activation is not None, 'unknown_activation')
            old=db.execute('SELECT * FROM routes WHERE id=?',(rid,)).fetchone()
            if old:
                require(json.loads(old['selection'])==selection, 'thread_selection_immutable')
                return dict(old)
            require(activation['enabled'] and activation['expires']>time.time(), 'activation_closed')
            require(not self._docs_reads_paused(db),'global_docs_read_recovery_pending')
            controller=self._live_controller(db)
            if controller['mode']=='native_google_v2':
                bound=db.execute("SELECT value FROM meta WHERE key='native_activation'").fetchone()
                require(bound is not None and json.loads(bound['value'])==generation,'activation_requires_new_native_join')
            require(db.execute('SELECT COUNT(*) FROM routes').fetchone()[0]<cfg['max_routes'], 'route_budget_exhausted')
            require(db.execute("SELECT COUNT(*) FROM routes WHERE state='pending'").fetchone()[0]<cfg['max_pending'],
                    'admission_queue_full')
            now=time.time()
            db.execute('INSERT INTO routes(id,generation,identity,selection,state,version,created,last_used,expires) '
                       'VALUES(?,?,?,?,?,1,?,?,?)',(rid,generation,json.dumps(identity),json.dumps(selection),
                                                'pending',now,now,activation['expires']))
            return dict(db.execute('SELECT * FROM routes WHERE id=?',(rid,)).fetchone())

    def claim(self, payload):
        require(set(payload)=={'controller_id','epoch'}, 'invalid_control_payload')
        with self.transaction() as db:
            controller=self._controller(db,payload)
            count=db.execute('SELECT COUNT(*) FROM routes WHERE state IN (%s)' % ','.join('?'*len(ACTIVE)), ACTIVE).fetchone()[0]
            require(count<controller['capacity'], 'native_child_capacity_exhausted')
            row=db.execute("SELECT * FROM routes WHERE state='pending' AND expires>? ORDER BY created,id LIMIT 1",(time.time(),)).fetchone()
            if row is None:return {'pending':False}
            claim=secrets.token_hex(16)
            db.execute("UPDATE routes SET state='claimed',version=version+1,claim=?,controller_epoch=? WHERE id=? AND version=?",
                       (claim,controller['epoch'],row['id'],row['version']))
            return {'pending':True,'route_id':row['id'],'generation':row['generation'],'claim':claim,
                    'version':row['version']+1,'selection':json.loads(row['selection']),
                    'identity':json.loads(row['identity']),'expires':row['expires']}

    def _owned(self, db, payload, state):
        controller=self._controller(db,payload)
        row=db.execute('SELECT * FROM routes WHERE id=?',(payload.get('route_id'),)).fetchone()
        require(row is not None and row['controller_epoch']==controller['epoch']
                and row['claim']==payload.get('claim') and row['version']==payload.get('version')
                and row['state']==state, 'admission_cas_conflict')
        require(row['expires']>time.time(), 'route_expired')
        return row

    def begin(self, payload):
        require(set(payload)=={'controller_id','epoch','route_id','claim','version'}, 'invalid_control_payload')
        with self.transaction() as db:
            row=self._owned(db,payload,'claimed'); selected=json.loads(row['selection'])
            arguments=spawn_arguments(selected,'global_'+row['id'],
                'Admit only route '+row['id']+' under dots-global-controller/1. Use its verified signed bootstrap, '
                'isolated deployment pin and immutable selected model/effort. Do not read another route, '
                'run user prompts before admission, or treat this protocol description as a working native connector.')
            db.execute("UPDATE routes SET state='spawn_intent',version=version+1,spawn_arguments=? WHERE id=?",
                       (json.dumps(arguments),row['id']))
            # Returning these args does NOT call the platform tool. The caller must
            # record actual success, or leave this route unknown forever (no retry).
            return {'route_id':row['id'],'version':row['version']+1,'spawn_arguments':arguments,
                    'attempt':1,'retry_allowed':False,'effect_performed_by_python':False}

    def unknown(self,payload):
        require(set(payload)=={'controller_id','epoch','route_id','claim','version'}, 'invalid_control_payload')
        with self.transaction() as db:
            row=self._owned(db,payload,'spawn_intent')
            db.execute("UPDATE routes SET state='unknown',version=version+1 WHERE id=?",(row['id'],))
        return {'state':'unknown','retry_allowed':False,'capacity_released':False}

    def admit(self,payload):
        require(set(payload)=={'controller_id','epoch','route_id','claim','version','pin'}, 'invalid_control_payload')
        pin=Object.parse(canonical(payload['pin'])); require(pin.body['kind']=='deployment', 'deployment_pin_required')
        with self.transaction() as db:
            row=self._owned(db,payload,'spawn_intent'); selected=json.loads(row['selection'])
            task=pin.body['identity']['native_task_id']
            require(pin.body['identity']['session_id']==row['id'] and pin_selection(pin)==selected,
                    'route_pin_binding_mismatch')
            receipt=pin.body['payload']['inference']['admission']
            validate_admission(receipt,selected,task)
            require(task.rsplit('/',1)[-1]==json.loads(row['spawn_arguments'])['task_name'], 'native_admission_result_mismatch')
            require(pin.body['payload']['scope']=='responses_tools' and pin.body['payload']['expires']<=row['expires']
                    and pin.body['payload']['expires']>time.time(), 'pin_limits_mismatch')
            try:
                db.execute("UPDATE routes SET state='admitted',version=version+1,native_task=?,pin=?,admission=?,expires=? WHERE id=?",
                           (task,pin.oid,json.dumps(payload['pin']),pin.body['payload']['expires'],row['id']))
            except sqlite3.IntegrityError:raise ProtocolError('native_child_or_pin_already_used') from None
            return {'state':'admitted','version':row['version']+1,'native_task_id':task,'deployment':pin.oid,
                    'evidence':'parent_recorded_platform_admission','underlying_model_verified':False}

    def attach(self,payload):
        require(set(payload)=={'controller_id','epoch','route_id','claim','version','endpoint','worker_state'},
                'invalid_control_payload')
        require(payload['worker_state']=='WORKER_POLLING','worker_polling_evidence_required')
        port=endpoint(payload['endpoint']); require(port!=self.config()['port'],'gateway_recursion_rejected')
        with self.transaction() as db:
            row=dict(self._owned(db,payload,'admitted'))
        conn=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
        try:
            conn.request('GET','/v1/bridge/status',headers=json.loads(row['identity']))
            response=conn.getresponse(); raw=response.read(MAX_RESPONSE_BYTES+1)
            require(response.status==200 and len(raw)<=MAX_RESPONSE_BYTES,'downstream_not_ready')
            value=strict_json(raw)
            require(value.get('deployment')==row['pin'] and value.get('native_task_id')==row['native_task']
                    and value.get('selection')==json.loads(row['selection']) and value.get('scope')=='responses_tools'
                    and value.get('closed') is False and value.get('automatic_wake') is False,
                    'downstream_pin_or_identity_mismatch')
        finally:conn.close()
        with self.transaction() as db:
            current=self._owned(db,payload,'admitted')
            try:db.execute("UPDATE routes SET state='ready',version=version+1,endpoint=? WHERE id=?",(payload['endpoint'],current['id']))
            except sqlite3.IntegrityError:raise ProtocolError('facade_already_attached_to_another_route') from None
        return {'state':'ready','version':row['version']+1,'production_ready':False,
                'mode':self.status()['controller_mode']}

    def route(self,rid):
        with self.transaction() as db:
            row=db.execute('SELECT * FROM routes WHERE id=?',(rid,)).fetchone()
            require(row is not None,'unknown_route'); return dict(row)

    def request_start(self, rid, digest):
        cfg=self.config()
        with self.transaction() as db:
            prior=db.execute('SELECT * FROM requests WHERE route=? AND digest=?',(rid,digest)).fetchone()
            if prior:
                if prior['state']=='text_complete':return {'replay':bytes(prior['response'])}
                raise ProtocolError('delivery_outcome_unknown_no_replay' if prior['state']=='dispatch_intent' else 'tool_or_unknown_delivery_no_replay')
            row=db.execute('SELECT * FROM routes WHERE id=?',(rid,)).fetchone()
            require(row is not None and row['state']=='ready','route_not_ready')
            require(not self._docs_reads_paused(db),'global_docs_read_recovery_pending')
            controller=self._live_controller(db)
            require(row['controller_epoch']==controller['epoch'],'route_controller_epoch_retired')
            require(row['expires']>time.time(),'route_expired')
            require(row['last_used']+cfg['idle_seconds']>time.time(),'route_idle_expired')
            require(row['used']<cfg['max_requests'],'route_request_budget_exhausted')
            pin=json.loads(row['admission'])['body']['payload']
            require(row['used']<pin['max_requests'],'pin_request_budget_exhausted')
            db.execute('INSERT INTO requests VALUES(?,?,?,?,NULL,NULL)',(rid,digest,'dispatch_intent',time.time()))
            db.execute('UPDATE routes SET used=used+1,last_used=? WHERE id=?',(time.time(),rid))
            return {'endpoint':row['endpoint']}

    def record_backend_response(self,rid,digest,response_id):
        require(isinstance(response_id,str) and re.fullmatch(r'resp_[A-Za-z0-9_-]{1,128}',response_id),'invalid_backend_response_id')
        with self.transaction() as db:
            row=db.execute('SELECT backend_response_id FROM requests WHERE route=? AND digest=?',(rid,digest)).fetchone()
            require(row is not None and row['backend_response_id'] in (None,response_id),'backend_response_identity_changed')
            db.execute('UPDATE requests SET backend_response_id=? WHERE route=? AND digest=?',(response_id,rid,digest))

    def request_finish(self,rid,digest,raw):
        state='unknown'; replay=None
        try:
            events=[strict_json(line[6:]) for line in raw.splitlines() if line.startswith(b'data: ')]
            terminal=events[-1]
            require(terminal['type']=='response.completed' and terminal['response']['status']=='completed','incomplete_response')
            output=terminal['response']['output']
            require(len(output)==1,'unsupported_response_output')
            # A cached text response must contain no earlier tool event either.
            has_tool=any(e.get('item',{}).get('type') in ('function_call','custom_tool_call') for e in events)
            if output[0]['type']=='message' and not has_tool:state='text_complete'; replay=raw
            else:state='tool_complete'
        except (ProtocolError,KeyError,TypeError,IndexError):pass
        with self.transaction() as db:
            require(db.execute("UPDATE requests SET state=?,response=? WHERE route=? AND digest=? AND state='dispatch_intent'",
                               (state,replay,rid,digest)).rowcount==1,'request_state_conflict')
        return state

    def status(self, *, bound=False, challenge=''):
        cfg=self.config(); activation=self.activation(); now=time.time()
        with self.transaction() as db:
            counts={row[0]:row[1] for row in db.execute('SELECT state,COUNT(*) FROM routes GROUP BY state')}
            c=db.execute('SELECT * FROM controller WHERE id=1').fetchone()
            alive=self._controller_alive(db,c,now)
            paused=self._docs_reads_paused(db)
        value={'protocol':PROTOCOL,'control_protocol':CONTROL_PROTOCOL,'bound':bound,'bound_pid':os.getpid() if bound else None,
               'generation':activation['id'],'base_url':f"http://127.0.0.1:{cfg['port']}/activations/{activation['id']}/v1",
               'catalog_path':activation['catalog'],'selection':json.loads(activation['selection']),
               'controller_active':alive and not paused,'controller_mode':c['mode'] if c else None,
               'docs_read_paused':paused,
               'activation_enabled':bool(activation['enabled'] and activation['expires']>now),
               'route_counts':counts,'automatic_wake':False,'production_ready':False,
               'ready_for_config':False,'missing':'live_native_google_and_mac_acceptance',
               'challenge':challenge}
        value['signature']=hmac.new(self.key.encode(),canonical(value),hashlib.sha256).hexdigest()
        return value


class Gateway:
    def __init__(self, store, *, request_deadline=180, max_connections=16, admission_wait=0):
        self.store=store; self.closed=False
        require(type(max_connections) is int and 1<=max_connections<=32,'invalid_connection_limit')
        require(type(request_deadline) in (int,float) and .1<=request_deadline<=300,'invalid_request_deadline')
        require(type(admission_wait) in (int,float) and 0<=admission_wait<=300,'invalid_admission_wait')
        self.admission_wait=admission_wait
        self.request_deadline=request_deadline; self.route_locks={};self.route_completions={}; self.route_lock_guard=threading.Lock()
        self.lease=os.open(store.root/'gateway.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(self.lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lease);raise ProtocolError('gateway_already_running') from None
        owner=self
        class Server(http.server.ThreadingHTTPServer):
            daemon_threads=True; block_on_close=False; allow_reuse_address=True
            def __init__(self,address,handler):
                self.slots=threading.BoundedSemaphore(max_connections)
                super().__init__(address,handler)
            def process_request(self,request,address):
                if not self.slots.acquire(False):
                    request.close();return
                try:super().process_request(request,address)
                except BaseException:self.slots.release();raise
            def process_request_thread(self,request,address):
                try:super().process_request_thread(request,address)
                finally:self.slots.release()
        try:
            self.server=Server(('127.0.0.1',store.config()['port']),Handler);self.server.owner=owner
        except BaseException:os.close(self.lease);raise
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.05},daemon=True)

    def start(self):self.thread.start();return self
    def lock_for(self,rid):
        with self.route_lock_guard:return self.route_locks.setdefault(rid,threading.Lock())
    def completion_for(self,rid):
        with self.route_lock_guard:return self.route_completions.setdefault(rid,threading.Event())
    def close(self):
        if self.closed:return
        self.closed=True
        if self.thread.is_alive():self.server.shutdown();self.thread.join(3)
        self.server.server_close();os.close(self.lease)
    def __enter__(self):return self.start()
    def __exit__(self,*_):self.close()


def rewrite_frame(frame,response_id,*,suppress_created=False):
    lines=frame.splitlines();data=[line[6:] for line in lines if line.startswith(b'data: ')]
    names=[line[7:] for line in lines if line.startswith(b'event: ')]
    require(len(data)==1 and len(names)==1,'invalid_downstream_sse_frame')
    event=strict_json(data[0]);require(isinstance(event,dict) and isinstance(event.get('type'),str)
        and names[0]==event['type'].encode(),'invalid_downstream_sse_event')
    backend_id=None
    if isinstance(event.get('response'),dict) and 'id' in event['response']:
        backend_id=event['response']['id'];event['response']['id']=response_id
    raw=b'event: '+event['type'].encode()+b'\ndata: '+canonical(event)+b'\n\n'
    return (b'' if suppress_created and event['type']=='response.created' else raw),backend_id


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'; server_version='DotsGlobalPrototype/1'; sys_version=''
    def log_message(self,*_):pass  # Never print URLs, prompts, control keys or payloads.
    def setup(self):
        super().setup();self.connection.settimeout(5)
    def headers_ok(self,control=False):
        require(self.client_address[0]=='127.0.0.1' and self.headers.get_all('Host')==[f'127.0.0.1:{self.server.server_port}'],
                'invalid_loopback_host')
        require(not any(self.headers.get_all(k) for k in ('Origin','Authorization','Cookie','Proxy-Authorization','Sec-Fetch-Site',
                                                        'Transfer-Encoding','Upgrade')), 'forbidden_local_header')
        require(not self.headers.get_all('Content-Encoding') or self.headers.get_all('Content-Encoding')==['identity'],
                'unsupported_encoding')
        if control:
            values=self.headers.get_all('X-Dots-Control') or []
            require(len(values)==1 and hmac.compare_digest(values[0],self.server.owner.store.key),'controller_authentication_required')
        else:require(not self.headers.get_all('X-Dots-Control'),'control_header_on_client_route')
    def body(self,limit=MAX_WIRE_BYTES):
        lengths=self.headers.get_all('Content-Length') or []
        require(len(lengths)==1 and re.fullmatch(r'[0-9]{1,10}',lengths[0]),'invalid_length')
        length=int(lengths[0]);require(0<length<=limit,'request_too_large_or_empty')
        require(len(self.headers.get_all('Content-Type') or [])==1 and self.headers.get_content_type()=='application/json',
                'unsupported_media_type')
        raw=self.rfile.read(length);require(len(raw)==length,'truncated_body');return strict_json(raw)
    def identity(self):
        require(not self.headers.get_all('session_id') and not self.headers.get_all('thread_id'),'ambiguous_legacy_session_header')
        value={}
        for name,pattern in [('session-id',r'[!-~]{1,256}'),('thread-id',UUID)]:
            values=self.headers.get_all(name) or [];require(len(values)==1 and re.fullmatch(pattern,values[0]),'canonical_runtime_session_required')
            value[name]=values[0]
        traces=self.headers.get_all('x-client-request-id') or []
        require(not traces or traces==[value['thread-id']],'conflicting_client_request_id')
        return value
    def json(self,value,status=200):
        raw=canonical(value);self.send_response(status);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)));self.send_header('Connection','close');self.end_headers()
        self.wfile.write(raw);self.close_connection=True
    def error(self,code,status=409):
        self.json({'error':{'code':code},'automatic_retry':False,'native_fallback':False},status)
    def do_GET(self):
        try:
            self.headers_ok()
            require(self.path=='/health','unsupported_endpoint')
            self.json({'protocol':PROTOCOL,'bound':True,'production_ready':False,'automatic_wake':False})
        except ProtocolError as exc:self.error(str(exc),400)
        finally:self.close_connection=True
    def do_OPTIONS(self):self.error('browser_access_not_supported',405)
    def do_POST(self):
        started=False;acquired=False;conn=None;lock=None;admission_waiting=False;response_id=None
        try:
            owner=self.server.owner;store=owner.store
            control=re.fullmatch(r'/control/v1/(join|heartbeat|claim|begin|admit|attach|unknown|status|route|disable)',self.path)
            self.headers_ok(bool(control))
            if control:
                payload=self.body(2*1024*1024);operation=control[1]
                require(isinstance(payload,dict),'invalid_control_payload')
                if operation=='status':
                    require(set(payload)=={'challenge'} and isinstance(payload['challenge'],str)
                            and re.fullmatch(HEX,payload['challenge']),'invalid_control_payload')
                    result=store.status(bound=True,challenge=payload['challenge'])
                elif operation=='route':
                    require(set(payload)=={'route_id'},'invalid_control_payload');row=store.route(payload['route_id'])
                    result={k:row[k] for k in ('id','state','version','native_task','pin','used','expires')}
                elif operation=='disable':
                    require(set(payload)=={'generation'},'invalid_control_payload');result=store.disable(payload['generation'])
                else:result=getattr(store,operation)(payload)
                return self.json(result)
            match=re.fullmatch(r'/activations/('+HEX+r')/v1/responses',self.path)
            require(match is not None,'unsupported_endpoint')
            identity=self.identity();body=self.body()
            # Select and validate the complete payload BEFORE binding a thread.
            require(isinstance(body,dict) and isinstance(body.get('reasoning'),dict),'explicit_reasoning_effort_required')
            selected=select(load_catalog(),body.get('model'),body['reasoning'].get('effort'))
            validate_request_selection(body,selected)
            from .wire import validate_remote_request
            validate_remote_request(body,'responses_tools')
            route=store.admission(match[1],identity,selected)
            rid=route['id'];digest=hash_bytes(canonical(body));response_id='resp_gw_'+hash_bytes(canonical({'route':rid,'request':digest}))
            lock=owner.lock_for(rid);acquired=lock.acquire(timeout=.1)
            if not acquired and owner.completion_for(rid).is_set():acquired=lock.acquire(timeout=5.9)
            require(acquired,'thread_request_inflight')
            if route['state']!='ready' and owner.admission_wait>0 and route['state'] in ('pending','claimed','spawn_intent','admitted'):
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Cache-Control','no-cache')
                self.send_header('Connection','close');self.end_headers();started=True;admission_waiting=True
                created={'type':'response.created','response':{'id':response_id,'status':'in_progress'}}
                self.wfile.write(b'event: response.created\ndata: '+canonical(created)+b'\n\n');self.wfile.flush()
                until=time.monotonic()+owner.admission_wait;heartbeat=0
                while route['state']!='ready':
                    require(not owner.closed,'gateway_stopping')
                    require(time.monotonic()<until,'admission_wait_expired_no_inference_dispatched')
                    require(route['expires']>time.time(),'admission_expired_no_inference_dispatched')
                    require(route['state'] in ('pending','claimed','spawn_intent','admitted'),'route_'+route['state'])
                    if socket_select.select([self.connection],[],[],0)[0] and not self.connection.recv(1,socket.MSG_PEEK):
                        raise ProtocolError('client_disconnected_before_dispatch')
                    if time.monotonic()>=heartbeat:
                        # Codex times parsed events, so comments are insufficient.
                        # A stable outer response ID spans admission + downstream;
                        # no tool item/call identifier is rewritten.
                        progress={'type':'response.in_progress','response':{'id':response_id,'status':'in_progress'}}
                        self.wfile.write(b'event: response.in_progress\ndata: '+canonical(progress)+b'\n\n');self.wfile.flush();heartbeat=time.monotonic()+.5
                    time.sleep(.05);route=store.route(rid)
                admission_waiting=False
            require(route['state']=='ready','admission_pending' if route['state'] in ('pending','claimed','spawn_intent','admitted') else 'route_'+route['state'])
            if socket_select.select([self.connection],[],[],0)[0] and not self.connection.recv(1,socket.MSG_PEEK):
                raise ProtocolError('client_disconnected_before_dispatch')
            dispatch=store.request_start(rid,digest)
            if 'replay' in dispatch:
                already_created=started
                if not started:
                    self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('X-Dots-Replay','text-only')
                    self.send_header('Connection','close');self.end_headers();started=True
                replay=dispatch['replay']
                if already_created:
                    replay=b''.join(rewrite_frame(frame,response_id,suppress_created=True)[0] for frame in replay.split(b'\n\n') if frame)
                owner.completion_for(rid).set()
                self.wfile.write(replay);self.wfile.flush();return
            # One durable dispatch intent. No reconnect/retry even if send fails.
            execution_until=time.monotonic()+owner.request_deadline
            port=endpoint(dispatch['endpoint']);conn=http.client.HTTPConnection('127.0.0.1',port,timeout=owner.request_deadline)
            conn.request('POST','/v1/responses',body=canonical(body),headers={**identity,'Content-Type':'application/json'})
            response_socket=conn.sock  # keep the actual socket before http.client detaches it for Connection: close
            response=conn.getresponse()
            require(response.status==200 and response.getheader('Content-Type','').split(';')[0]=='text/event-stream',
                    'downstream_response_unknown')
            already_created=started
            if not started:
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Cache-Control','no-cache')
                self.send_header('Connection','close');self.end_headers();started=True
            chunks=[];size=0;buffer=b'';backend_id=None;until=execution_until
            while True:
                require(time.monotonic()<until,'gateway_wait_budget_expired')
                if response_socket is not None:response_socket.settimeout(max(.01,until-time.monotonic()))
                chunk=response.read1(8192)
                if not chunk:break
                size+=len(chunk);require(size<=MAX_RESPONSE_BYTES,'response_too_large')
                buffer+=chunk
                while b'\n\n' in buffer:
                    frame,buffer=buffer.split(b'\n\n',1)
                    if not frame:continue
                    rewritten,seen_id=rewrite_frame(frame,response_id,suppress_created=already_created)
                    if seen_id is not None:
                        require(backend_id in (None,seen_id),'backend_response_identity_changed')
                        if backend_id is None:store.record_backend_response(rid,digest,seen_id);backend_id=seen_id
                    if rewritten:
                        if rewritten.startswith(b'event: response.completed\n'):owner.completion_for(rid).set()
                        chunks.append(rewritten);self.wfile.write(rewritten);self.wfile.flush()
            require(not buffer,'truncated_downstream_sse_frame')
            # Include a normalized created event in cached output even if it was
            # emitted during admission rather than forwarded from the backend.
            if already_created:
                created={'type':'response.created','response':{'id':response_id,'status':'in_progress'}}
                chunks.insert(0,b'event: response.created\ndata: '+canonical(created)+b'\n\n')
            store.request_finish(rid,digest,b''.join(chunks))
        except ProtocolError as exc:
            if started and admission_waiting:
                try:
                    failure={'type':'response.failed','response':{'id':response_id,
                        'error':{'code':str(exc),'message':'No inference was dispatched. The admission may still need reconciliation.'}}}
                    self.wfile.write(b'event: response.failed\ndata: '+canonical(failure)+b'\n\n');self.wfile.flush()
                except OSError:pass
            if not started:
                try:self.error(str(exc),503 if str(exc) in ('router_controller_not_ready','native_child_capacity_exhausted','admission_queue_full') else 409)
                except OSError:pass
        except (OSError,ValueError,http.client.HTTPException,sqlite3.Error):
            if not started:
                try:self.error('gateway_io_outcome_unknown',503)
                except OSError:pass
        finally:
            if conn:conn.close()
            if acquired:owner.completion_for(rid).clear();lock.release()
            self.close_connection=True


def control_call(root,operation,payload,timeout=5):
    store=Store(root);cfg=store.config();conn=http.client.HTTPConnection('127.0.0.1',cfg['port'],timeout=timeout)
    try:
        conn.request('POST','/control/v1/'+operation,body=canonical(payload),headers={'Content-Type':'application/json','X-Dots-Control':store.key})
        response=conn.getresponse();raw=response.read(MAX_RESPONSE_BYTES+1)
        require(len(raw)<=MAX_RESPONSE_BYTES,'control_response_too_large');value=strict_json(raw)
        require(response.status==200,value.get('error',{}).get('code','control_failed'));return value
    finally:conn.close()


def probe(root):
    store=Store(root);challenge=secrets.token_hex(16);value=control_call(root,'status',{'challenge':challenge})
    signature=value.pop('signature',None)
    require(isinstance(signature,str) and hmac.compare_digest(signature,hmac.new(store.key.encode(),canonical(value),hashlib.sha256).hexdigest())
            and value.get('challenge')==challenge and value.get('protocol')==PROTOCOL and value.get('bound') is True,
            'gateway_probe_mismatch')
    return value


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    init=sub.add_parser('init');init.add_argument('--state-dir',required=True);init.add_argument('--port',type=int,default=DEFAULT_PORT)
    init.add_argument('--model',required=True);init.add_argument('--effort',required=True)
    for name in ('serve','status'):
        p=sub.add_parser(name);p.add_argument('--state-dir',required=True)
    ctl=sub.add_parser('controller');ctl.add_argument('--state-dir',required=True);ctl.add_argument('--operation',required=True)
    ctl.add_argument('--payload-file',required=True)
    args=parser.parse_args();os.umask(0o077)
    if args.command=='init':
        store,_=Store.initialize(args.state_dir,select(load_catalog(),args.model,args.effort),port=args.port)
        value=store.status();value.pop('signature',None);print(json.dumps(value));return
    if args.command=='status':print(json.dumps(probe(args.state_dir)));return
    if args.command=='controller':
        value=control_call(args.state_dir,args.operation,strict_json(read_private_file(args.payload_file,MAX_RESPONSE_BYTES)))
        print(json.dumps(value));return
    with Gateway(Store(args.state_dir)):
        try:
            while True:time.sleep(1)
        except KeyboardInterrupt:pass

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'error':str(exc) if isinstance(exc,ProtocolError) else type(exc).__name__}));raise SystemExit(1)
