"""Mac-side Google queue / existing child-JOIN adapter.

All remote effects require explicit construction with authorized Docs/Drive ports.
This module does not find credentials, log in, install, spawn native tasks, change
Codex config, or claim native readiness from a listening port. Defaults retain the
production readiness gate; real Mac/connector acceptance is a separate step.
"""
import argparse
import copy
import fcntl
import os
import json
from pathlib import Path
import secrets
import sqlite3
import time

from . import global_control as queue
from . import router_bootstrap as child
from . import router_mac as mac
from .backend import GoogleDriveBackend, read_private_file
from .control import GoogleDocsCASControlStore, SessionCoordinator
from .controlled import CASController
from .facade import RemoteResponsesFacade
from .global_gateway import private_dir, private_write, strict_json
from .model import Object, ProtocolError, canonical, deployment, hash_bytes, require
from .selection import pin_selection
from .session import Journal


def mirror_verified_queue(store,state,code,pins=None):
    """Authenticated native heartbeat + admission mirror, not a second authority.

    The signed Docs event history is the distributed CAS authority. Local SQLite
    preserves the gateway's independent one-dispatch journal and endpoint binding.
    Stale heartbeats cannot be refreshed by repeatedly reading the same snapshot.
    """
    queue.verify(state,code);cfg=store.config();activation=store.activation(state['activation_id'])
    require(state['limits']['max_routes']<=cfg['max_routes'] and state['limits']['max_pending']<=cfg['max_pending']
            and state['limits']['max_children']<=cfg['max_children'] and state['expires']<=activation['expires'],
            'global_queue_exceeds_local_limits')
    c=state['logical']['controller'];require(c is not None,'global_controller_join_required')
    controller_id=hash_bytes(c['native_task_id'].encode())[:32]
    credentials={'controller_id':controller_id,'epoch':c['controller_epoch'] if 'controller_epoch' in c else c['epoch']}
    # Queue stores controller_epoch in root admission arguments; normalize only the
    # local column name, never mutate or reinterpret the signed queue bytes.
    epoch=credentials['epoch'];pins=pins or {}
    with store.transaction() as db:
        old=db.execute('SELECT * FROM controller WHERE id=1').fetchone()
        require(old is None or (old['epoch']==epoch and old['mode']=='native_google_v1')
                or old['expires']<=time.time(),'local_controller_session_conflict')
        expires=0 if state['logical']['closed'] else c['lease_expires']
        heartbeat=0 if state['logical']['closed'] else c['heartbeat_at']
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('native_activation',json.dumps(state['activation_id'])))
        db.execute('INSERT OR REPLACE INTO controller VALUES(1,?,?,?,?,?,?)',
                   (controller_id,epoch,'native_google_v1',expires,heartbeat,c['capacity']))
        for rid,pin in pins.items():
            demand=state['logical']['demands'].get(rid);row=db.execute('SELECT * FROM routes WHERE id=?',(rid,)).fetchone()
            require(demand is not None and demand['state'] in ('admitted','ready') and row is not None,
                    'global_mirror_route_not_admitted')
            pin=pin if isinstance(pin,Object) else Object.parse(canonical(pin))
            require(row['generation']==state['activation_id'] and json.loads(row['selection'])==demand['selection']
                    and hash_bytes(canonical(json.loads(row['identity'])))==demand['identity_sha256']
                    and pin.body['identity']['session_id']==rid and pin_selection(pin)==demand['selection']
                    and pin.body['payload']['inference']['admission']==demand['admission']
                    and pin.body['payload']['expires']<=row['expires'] and pin.body['payload']['scope']=='responses_tools',
                    'global_mirror_pin_or_identity_mismatch')
            if demand['state']=='ready':require(demand['ready']['deployment']==pin.oid,'global_mirror_ready_pin_mismatch')
            task=pin.body['identity']['native_task_id']
            if row['state'] in ('admitted','ready'):
                require(row['pin']==pin.oid and row['native_task']==task and row['controller_epoch']==epoch,
                        'global_mirror_existing_binding_conflict');continue
            require(row['state']=='pending','global_mirror_local_state_conflict')
            try:
                db.execute("UPDATE routes SET state='admitted',version=version+1,controller_epoch=?,claim=?,native_task=?,pin=?,admission=?,expires=?,spawn_arguments=? WHERE id=?",
                    (epoch,demand['claim_id'],task,pin.oid,json.dumps(pin.value),pin.body['payload']['expires'],
                     json.dumps({'task_name':'global_'+rid,'source':'signed_global_queue','arguments_sha256':demand['spawn_arguments_sha256']}),rid))
            except sqlite3.IntegrityError:raise ProtocolError('global_mirror_child_or_pin_reused') from None
    return credentials


class GoogleQueueBridge:
    def __init__(self,store,root,docs,drive,initial_state,join_code,*,mac_writer='global-mac',worker_writer='global-native',
                 bootstrap_seconds=600,request_seconds=14400):
        queue.verify(initial_state,join_code)
        require(store.activation(initial_state['activation_id']) is not None,'global_activation_mismatch')
        require(type(bootstrap_seconds) is int and 60<=bootstrap_seconds<=1800 and type(request_seconds) is int
                and 30<=request_seconds<=28800,'invalid_global_bridge_limits')
        self.store=store;self.root=private_dir(root,create=True);self.docs=docs;self.drive=drive
        self.initial=copy.deepcopy(initial_state);self.code=join_code;self.source_root=queue.root_of(initial_state)
        self.document_id=initial_state['document_id'];self.tab_id=initial_state['tab_id']
        self.config={'folder_id':initial_state['folder_id'],'mac_writer_identity':mac_writer,'worker_writer_identity':worker_writer}
        self.bootstrap_seconds=bootstrap_seconds;self.request_seconds=request_seconds;self.facades={}
        self.lease=os.open(self.root/'bridge.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:fcntl.flock(self.lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lease);self.lease=None;raise ProtocolError('global_google_bridge_already_running') from None
    def _check_running(self):
        require(not (self.root/'stop-requested.json').exists(),'global_session_stopped_new_activation_required')
    def read(self):
        value=queue.snapshot(self.docs.get_document(self.document_id),self.document_id,self.tab_id)
        queue.verify(value.state,self.code,expected_root=self.source_root)
        # Durable anti-rollback observation, including across process restarts.
        path=self.root/'queue-observation.json'
        hashes=[hash_bytes(canonical(e)) for e in value.state['events']]
        if path.exists():
            old=strict_json(read_private_file(path,262144))
            require(old['root_hash']==queue.root_hash(value.state) and hashes[:len(old['event_hashes'])]==old['event_hashes'],
                    'global_mac_observation_rollback_or_fork')
        private_write(path,canonical({'root_hash':queue.root_hash(value.state),'event_hashes':hashes}))
        return value
    def initialize_blank_queue(self):
        doc=self.docs.get_document(self.document_id)
        from .control import _document_text
        require(_document_text(doc,self.tab_id)=='\n','global_queue_document_not_blank')
        revision=doc.get('revisionId');require(isinstance(revision,str) and revision,'global_revision_required')
        args={'document_id':self.document_id,'requests':[{'insertText':{'location':{'index':1,'tabId':self.tab_id},
              'text':queue.block(self.initial)[:-1]}}],'write_control':{'requiredRevisionId':revision}}
        path,record=mac._operation(self.root,'initialize-queue',args)
        try:self.docs.batch_update_document(**args)
        except Exception:pass  # Exact read-only reconciliation, never reissue.
        try:
            fresh=self.read();require(fresh.state==self.initial and fresh.revision_id!=revision,'global_queue_initialization_mismatch')
        except Exception:
            mac._finish_operation(path,record,status='unknown');raise ProtocolError('global_queue_initialization_unknown_no_retry') from None
        mac._finish_operation(path,record,status='verified',revision=fresh.revision_id);return fresh
    def event(self,kind,args):
        source=self.read();new=queue.transition(source.state,self.code,kind,'mac',args)
        packet=queue.plan(source,new,self.code)
        label='queue-'+kind+'-'+str(args.get('route_id','controller'))
        path,record=mac._operation(self.root,label,packet)
        response=None
        try:response=self.docs.batch_update_document(**packet['tool_arguments'])
        except Exception:pass
        try:
            readback=self.docs.get_document(self.document_id)
            fresh=queue.verify_update(packet,response,readback,self.code)
            # Anti-rollback read observes the actual latest document, not invented state.
            self.read()
        except Exception:
            mac._finish_operation(path,record,status='unknown');raise ProtocolError('global_mac_cas_unknown_no_retry') from None
        mac._finish_operation(path,record,status='verified',revision=fresh.revision_id);return fresh
    def join_message(self):
        return ('DOTS2CODEX_GLOBAL_JOIN_V1\nactivation_id='+self.initial['activation_id']+
                '\nqueue_document_id='+self.document_id+'\nqueue_tab_id='+self.tab_id+'\njoin_code='+self.code+
                '\nRun one bounded active native Router controller using docs/GLOBAL_NATIVE_CONTROLLER.md. '
                'Only this signed queue, folder, deadline and reviewed per-thread model pairs are authorized. '
                'No background Python spawning or automatic wake. Never post this code in logs or third-party messages.')
    def prepare_child(self,route_id):
        self._check_running()
        source=self.read();route=self.store.route(route_id)
        require(route['state']=='pending' and route['generation']==source.state['activation_id'],'global_local_demand_not_pending')
        require(route_id not in source.state['logical']['demands'],'global_demand_already_published')
        require(min(int(route['expires']),source.state['expires'])-int(time.time())>=60,'global_bootstrap_window_too_short')
        runtime=self.root/'routes'/route_id
        require(not runtime.exists(),'global_child_runtime_exists_reconcile_no_retry')
        private_dir(runtime,create=True);active_path=runtime/'active.json';now=int(time.time())
        active={'contract':'dots-router-active/2','stage':'PREPARING','closed':False,'session_id':route_id,
                'runtime':str(runtime),'bootstrap_id':secrets.token_hex(16),'lifecycle_intent':secrets.token_hex(16),
                'stop_intent_file':str(active_path)+'.stop-intent.json','bootstrap_document_id':None,'bootstrap_tab_id':None,
                'control_document_id':None,'control_tab_id':None,'control_id':secrets.token_hex(16),
                'bootstrap_root':None,'model_selection':json.loads(route['selection']),'created':now,
                'pin_file':str(runtime/'pin.json'),'controller_journal':str(runtime/'controller'),
                'worker_config_file':str(runtime/'worker-config.json'),'control_initialization_attempted':False}
        mac._private_json(active_path,active);cc=queue.child_code(self.code,source.state['activation_id'],route_id)
        mac._private_json(runtime/'private-pairing.json',{'join_code':cc})
        active=mac._create_doc_once(self.drive,self.docs,active_path,active,self.config,'Control')
        active=mac._create_doc_once(self.drive,self.docs,active_path,active,self.config,'Bootstrap')
        forward=mac._create_forward_probe(self.drive,active,self.config)
        expires=min(int(route['expires']),source.state['expires'])
        initial=child.initial_state(bootstrap_id=active['bootstrap_id'],session_id=route_id,created=now,
                    expires=min(expires,now+self.bootstrap_seconds),join_code=cc,folder_id=self.config['folder_id'],
                    control_document_id=active['control_document_id'],control_tab_id=active['control_tab_id'],
                    control_id=active['control_id'],mac_writer_identity=self.config['mac_writer_identity'],
                    worker_writer_identity=self.config['worker_writer_identity'],bootstrap_document_id=active['bootstrap_document_id'],
                    bootstrap_tab_id=active['bootstrap_tab_id'],forward_probe=forward,required_selection=json.loads(route['selection']))
        active=mac._write_active(active_path,active,bootstrap_root=child.root_context(initial))
        mac._initialize_bootstrap(self.docs,active['bootstrap_document_id'],active['bootstrap_tab_id'],initial,runtime)
        mac._write_active(active_path,active,stage='WAITING_FOR_WORKER')
        return self.event('demand',{'route_id':route_id,'generation':route['generation'],
                    'identity_sha256':hash_bytes(canonical(json.loads(route['identity']))),'selection':json.loads(route['selection']),
                    'expires':expires,'child_bootstrap':initial})
    def _child(self,route_id,*,require_fresh=True):
        runtime=private_dir(self.root/'routes'/route_id);active_path=runtime/'active.json'
        active=mac._load_private_json(active_path);cc=queue.child_code(self.code,self.initial['activation_id'],route_id)
        require(active['session_id']==route_id and Path(active['runtime'])==runtime,'global_child_runtime_mismatch')
        snap=child.snapshot_from_document(self.docs.get_document(active['bootstrap_document_id']),
                                          active['bootstrap_document_id'],active['bootstrap_tab_id'])
        child.verify_context(snap.state,cc,snap.document_id,snap.tab_id,expected_root=active['bootstrap_root'],require_fresh=require_fresh)
        return runtime,active_path,active,cc,snap
    def advance_child(self,route_id):
        self._check_running()
        source=self.read();d=source.state['logical']['demands'].get(route_id)
        require(d is not None and d['state'] in ('admitted','ready'),'global_child_native_admission_required')
        runtime,active_path,active,cc,snap=self._child(route_id,require_fresh=d['state']!='ready')
        if d['state']=='ready':
            # This is transport recovery for a historically verified admission,
            # never a fresh native spawn. Verify the signed terminal bootstrap,
            # current control and unexpired pin; do not pretend an old handshake
            # timestamp is fresh worker telemetry.
            require(snap.state['stage'] in ('WORKER_POLLING','CONSUMED')
                    and snap.state['worker_ack'] is not None
                    and snap.state['worker_ack']['runtime_hash']==d['ready']['runtime_hash'],
                    'global_historical_worker_binding_mismatch')
            native=d['admission']['native_task_id']
            pin=Object.parse(read_private_file(runtime/'pin.json',2*1024*1024))
            require(pin.oid==d['ready']['deployment'] and pin.body['payload']['expires']>time.time(),
                    'global_recovery_pin_expired_or_changed')
            if route_id in self.facades:return {'route_id':route_id,'state':'ready','production_ready':False}
        else:native=child.verify_worker_admission(snap.state,cc)
        require(snap.state['worker']['admission']==d['admission'],'global_child_native_receipt_mismatch')
        if snap.state['stage']=='WORKER_ADMITTED':
            mac._verify_reverse_probe(self.drive,snap.state)
            path=runtime/'pin.json'
            if path.exists():pin=Object.parse(read_private_file(path,2*1024*1024))
            else:
                seconds=min(self.request_seconds,int(d['expires'])-int(time.time()))
                require(seconds>=1,'global_route_expired')
                pin=deployment(route_id,native,seconds=seconds,max_requests=self.store.config()['max_requests'],scope='responses_tools',
                               inference={'selection':d['selection'],'admission':d['admission']})
                from .cli import write_new
                write_new(path,pin.raw)
            active=mac._write_active(active_path,active,deployment=pin.oid,native_task_id=native,pin_expires=pin.body['payload']['expires'])
            if (runtime/'controller').exists():Journal(runtime/'controller',pin,'controller')
            else:Journal.provision(runtime/'controller',pin,'controller')
            active=mac._write_active(active_path,active,control_initialization_attempted=True)
            arguments={'document_id':active['control_document_id'],'pin':pin.oid}
            op=runtime/'initialize-control.json'
            if op.exists():
                record=mac._load_private_json(op)
                require(record['arguments']==arguments,'global_control_initialization_binding_mismatch')
            else:
                op,record=mac._operation(runtime,'initialize-control',arguments)
                from examples.remote_setup import initialize_blank
                try:initialize_blank(self.docs,pin,active['control_document_id'],active['control_tab_id'],active['control_id'],self.config['mac_writer_identity'])
                except Exception:pass  # Read-only exact reconciliation only.
            from .control import initial_state
            check=GoogleDocsCASControlStore(self.docs,active['control_document_id'],active['control_tab_id'],active['control_id'],route_id,self.config['mac_writer_identity'])
            require(check.read().state==initial_state(pin,active['control_id']),'global_control_initialization_unknown')
            mac._finish_operation(op,record,status='verified')
            config={'document_id':active['control_document_id'],'tab_id':active['control_tab_id'],'control_id':active['control_id'],
                    'writer_identity':self.config['worker_writer_identity'],'folder_id':self.config['folder_id']}
            raw=canonical(config);private_write(runtime/'worker-config.json',raw)
            fresh=child.snapshot_from_document(self.docs.get_document(snap.document_id),snap.document_id,snap.tab_id)
            nxt=child.bundle_ready(fresh.state,join_code=cc,pin_raw=pin.raw,config_raw=raw,deployment_hash=pin.oid)
            mac._bootstrap_cas(self.docs,fresh,nxt,join_code=cc,runtime=runtime)
            mac._write_active(active_path,active,stage='BUNDLE_READY')
            return {'route_id':route_id,'state':'await_child_polling'}
        require(snap.state['stage'] in ('BUNDLE_READY','WORKER_POLLING','CONSUMED'),'global_child_stage_not_ready')
        if snap.state['stage']=='BUNDLE_READY':return {'route_id':route_id,'state':'await_child_polling'}
        if d['state']!='ready':child.verify_worker_polling(snap.state,cc)
        pin=Object.parse(read_private_file(runtime/'pin.json',2*1024*1024))
        if d['state']!='ready':self.event('ready',{'route_id':route_id,'pin':pin.value,'child_bootstrap':snap.state})
        if snap.state['stage']=='WORKER_POLLING' and time.time()<snap.state['expires']:
            mac._bootstrap_cas(self.docs,snap,child.consume_bundle(snap.state,join_code=cc),join_code=cc,runtime=runtime)
        source=self.read();credentials=mirror_verified_queue(self.store,source.state,self.code,{route_id:pin})
        backend=GoogleDriveBackend(self.drive,self.config['folder_id'],discovery='control_refs')
        control=GoogleDocsCASControlStore(self.docs,active['control_document_id'],active['control_tab_id'],active['control_id'],route_id,self.config['mac_writer_identity'])
        controller=CASController(Journal(runtime/'controller',pin,'controller'),backend,SessionCoordinator(control,backend))
        facade=RemoteResponsesFacade(controller,long_session=True,request_deadline=180,poll_interval=1,heartbeat_interval=15).start()
        self.facades[route_id]=facade
        row=self.store.route(route_id)
        if row['state']=='ready':
            # Real journal lease above proves another facade for this same journal
            # is not active. Rebind transport only; preserve pin, native task,
            # request intents, receipts and all unknown-delivery fences.
            with self.store.transaction() as db:
                count=db.execute("UPDATE routes SET state='admitted',version=version+1,endpoint=NULL WHERE id=? AND version=? AND pin=? AND native_task=?",
                    (route_id,row['version'],pin.oid,native)).rowcount
                require(count==1,'global_transport_reattach_conflict')
            row=self.store.route(route_id)
        try:self.store.attach({**credentials,'route_id':route_id,'claim':row['claim'],'version':row['version'],
                              'endpoint':facade.base_url,'worker_state':'WORKER_POLLING'})
        except Exception:facade.close();self.facades.pop(route_id,None);raise
        return {'route_id':route_id,'state':'ready','production_ready':False}
    def sync_heartbeat(self):
        self._check_running()
        return mirror_verified_queue(self.store,self.read().state,self.code)
    def close_local_facades(self):
        # Local transport shutdown does NOT assert that native children stopped.
        for facade in self.facades.values():facade.close()
        self.facades.clear()

    def step(self):
        if (self.root/'stop-requested.json').exists():return {'state':'closed','native_children_stopped':False}
        source=self.read();state=source.state;c=state['logical']['controller']
        if c is None:return {'state':'await_native_join','automatic_wake':False}
        self.sync_heartbeat()
        if state['logical']['closed']:return {'state':'closed','native_children_stopped':False}
        if c['lease_expires']<=time.time() or c['heartbeat_at']+30<=time.time():
            return {'state':'native_controller_not_ready','automatic_wake':False}
        with self.store.transaction() as db:
            pending=[r['id'] for r in db.execute("SELECT id FROM routes WHERE state='pending' AND generation=? ORDER BY created",(state['activation_id'],))]
        for rid in pending:
            if rid not in state['logical']['demands']:
                self.prepare_child(rid);return {'state':'demand_published','route_id':rid}
        for rid,d in state['logical']['demands'].items():
            if d['state'] in ('admitted','ready') and rid not in self.facades:
                runtime,active_path,active,cc,snap=self._child(rid,require_fresh=d['state']!='ready')
                # Queue receipt may arrive before the actual child has performed
                # its v3 probe/admission. Wait; do not manufacture readiness.
                if snap.state['stage']=='WAITING_FOR_WORKER':continue
                result=self.advance_child(rid)
                if result['state']!='await_child_polling':return result
        return {'state':'controller_active','ready_routes':len(self.facades),'production_ready':False}
    def stop(self):
        # Stop local admission immediately, even if the following remote close is
        # ambiguous. Never claim that a local socket close stopped a native task.
        private_write(self.root/'stop-requested.json',canonical({'queue_root_hash':queue.root_hash(self.initial),'at':time.time()}))
        with self.store.transaction() as db:db.execute('UPDATE controller SET heartbeat=0,expires=0')
        self.store.disable(self.initial['activation_id'])
        outcome={'queue_closed':False,'controls':{},'native_children_stopped':False}
        try:
            if self.read().state['logical']['closed']:outcome['queue_closed']=True
            else:self.event('close',{'confirm':True});outcome['queue_closed']=True
        except Exception:outcome['queue_close_outcome']='unknown_read_only_reconciliation_required'
        for active_path in (self.root/'routes').glob('*/active.json'):
            rid=active_path.parent.name
            try:
                active=mac._load_private_json(active_path)
                require(active['session_id']==rid,'global_stop_route_binding_mismatch')
                outcome['controls'][rid]=mac._close_control(self.docs,active,self.config)
            except Exception:outcome['controls'][rid]={'authoritative_close':'unknown'}
        self.close_local_facades();return outcome
    def close(self):
        self.close_local_facades()
        if self.lease is not None:os.close(self.lease);self.lease=None


def prepare_session(store,root,docs,drive,folder_id,*,mac_writer='global-mac',worker_writer='global-native'):
    """Explicitly authorized control-resource creation; every effect is one-attempt.

    Caller must bind/verify the gateway before invoking this function. An unknown
    document create preserves its journal and cannot be retried by this helper.
    """
    from .global_gateway import probe
    require(probe(store.root)['bound'] is True,'bound_gateway_required')
    queue.safe_id(folder_id);root=private_dir(root,create=True)
    initial_path=root/'activation.json';code_path=root/'activation-code.txt'
    if initial_path.exists():
        initial=strict_json(read_private_file(initial_path,queue.MAX_BYTES));code=read_private_file(code_path,256).decode()
        require(initial['folder_id']==folder_id,'global_authorized_folder_changed')
        bridge=GoogleQueueBridge(store,root,docs,drive,initial,code,mac_writer=mac_writer,worker_writer=worker_writer)
        try:bridge.read()
        except Exception:bridge.close();raise
        return bridge
    require(not code_path.exists(),'global_activation_preparation_requires_reconciliation')
    code=secrets.token_hex(32);private_write(code_path,code.encode())
    activation=store.activation();now=int(time.time());expires=min(int(activation['expires']),now+14400)
    require(expires-now>=60,'global_activation_window_too_short')
    op,record=mac._operation(root,'create-queue',{'folder_id':folder_id,'activation_id':activation['id']})
    try:did=drive.create_document_once(folder_id,'Dots2Codex Global Router '+activation['id'])
    except Exception:
        mac._finish_operation(op,record,status='unknown');raise ProtocolError('global_queue_create_unknown_no_retry') from None
    mac._finish_operation(op,record,status='returned',document_id=did)
    tab,_=mac._blank_doc_info(docs,did)
    from .router_join import _source_hashes
    initial=queue.initial(activation_id=activation['id'],queue_id=secrets.token_hex(16),folder_id=folder_id,
        document_id=did,tab_id=tab,join_code=code,created=now,expires=expires,runtime_source_hashes=_source_hashes(),
        max_routes=min(16,store.config()['max_routes']),max_pending=min(8,store.config()['max_pending']),
        max_children=store.config()['max_children'])
    private_write(initial_path,canonical(initial))
    bridge=GoogleQueueBridge(store,root,docs,drive,initial,code,mac_writer=mac_writer,worker_writer=worker_writer)
    try:bridge.initialize_blank_queue()
    except Exception:bridge.close();raise
    private_write(root/'global-join.txt',bridge.join_message().encode())
    return bridge


def main():
    p=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    p.add_argument('--state-dir',required=True);p.add_argument('--bridge-dir',required=True)
    p.add_argument('--router-config',required=True);p.add_argument('--confirm-google-control',action='store_true')
    p.add_argument('--seconds',type=int,default=14400)
    a=p.parse_args();require(a.confirm_google_control,'explicit_google_control_authorization_required')
    require(30<=a.seconds<=28800,'invalid_global_runtime_lifetime');os.umask(0o077)
    from .global_gateway import Store,Gateway
    config=mac._load_config(a.router_config)
    require(config['scope']=='responses_tools','global_requires_responses_tools_scope')
    store=Store(a.state_dir)
    # An occupied port fails before creating resources or refreshing credentials.
    with Gateway(store,admission_wait=180):
        mac._set_google_env(config)
        from examples.google_clients import create_docs_client,create_drive_client
        docs=create_docs_client();drive=create_drive_client()
        bridge=prepare_session(store,a.bridge_dir,docs,drive,config['folder_id'],
                               mac_writer=config['mac_writer_identity'],worker_writer=config['worker_writer_identity'])
        print(json.dumps({'state':'await_native_join','private_join_file':str(bridge.root/'global-join.txt'),
                          'production_ready':False,'global_config_changed':False}),flush=True)
        until=min(time.time()+a.seconds,bridge.initial['expires']);last=None
        try:
            while time.time()<until:
                status=bridge.step();safe={k:v for k,v in status.items() if k!='route_id'}
                if safe!=last:print(json.dumps(safe),flush=True);last=safe
                if status['state']=='closed':break
                time.sleep(2)
        except KeyboardInterrupt:pass
        finally:
            print(json.dumps(bridge.stop()),flush=True);bridge.close()

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'error':str(exc) if isinstance(exc,ProtocolError) else type(exc).__name__}));raise SystemExit(1)
