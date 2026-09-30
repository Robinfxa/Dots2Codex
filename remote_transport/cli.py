"""Runnable remote facade/worker commands; never invokes a model or extracts OAuth."""
import argparse
import importlib
import json
import os
from pathlib import Path
import tempfile
import time
from . import deployment, Object, ProtocolError, Journal, Controller, Worker, LocalFSBackend, GoogleDriveBackend
from .backend import read_private_file, fsync_dir
from .model import canonical, require, MAX_BYTES
from .facade import RemoteResponsesFacade


def write_new(path, raw):
    path=Path(path)
    fd,tmp=tempfile.mkstemp(prefix='.new-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as out:
            out.write(raw);out.flush();os.fsync(out.fileno())
        os.link(tmp,path,follow_symlinks=False)
        fsync_dir(path.parent)
    finally:os.unlink(tmp)


def backend(args):
    require(args.transport in {'localfs','drive'},'explicit_transport_required')
    if args.transport=='localfs':
        require(args.object_root is not None and args.client_factory is None and args.folder_id is None,
                'localfs_options_required')
        return LocalFSBackend(args.object_root)
    require(args.folder_id is not None and args.client_factory is not None and args.object_root is None,
            'drive_client_and_folder_required')
    # Caller-selected, separately reviewed local client factory; no token discovery.
    module,separator,name=args.client_factory.partition(':')
    require(separator and module and name and not name.startswith('_'),'invalid_client_factory')
    factory=getattr(importlib.import_module(module),name)
    require(callable(factory),'invalid_client_factory')
    return GoogleDriveBackend(factory(),args.folder_id,mode=args.drive_mode,
        discovery='control_refs' if args.control_document_id else 'list')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation',choices=['new-deployment','provision-journal','init-local-store',
        'publish-deployment','serve','worker-next','worker-complete','controller-result',
        'ack-delivery','recover-publication','status','close-session'])
    p.add_argument('--pin');p.add_argument('--journal');p.add_argument('--role',choices=['controller','worker'])
    p.add_argument('--session');p.add_argument('--native-task-id');p.add_argument('--save')
    p.add_argument('--transport',choices=['localfs','drive']);p.add_argument('--object-root')
    p.add_argument('--folder-id');p.add_argument('--client-factory',help='reviewed module:function returning authenticated Drive API client')
    p.add_argument('--docs-client-factory',help='reviewed module:function returning independently authorized Docs client')
    p.add_argument('--control-document-id');p.add_argument('--control-tab-id');p.add_argument('--control-id')
    p.add_argument('--control-writer-identity')
    p.add_argument('--drive-mode',choices=['strict_ids','duplicate_tolerant'],default='strict_ids')
    p.add_argument('--port',type=int,default=0);p.add_argument('--deadline',type=float,default=60)
    p.add_argument('--seconds',type=int,default=600);p.add_argument('--max-requests',type=int,default=3)
    p.add_argument('--scope',choices=['text_only','responses_tools'],default='text_only')
    p.add_argument('--long-session',action='store_true')
    p.add_argument('--poll-interval',type=float,default=5);p.add_argument('--heartbeat-interval',type=float,default=15)
    p.add_argument('--wait',type=float,default=10)
    p.add_argument('--permit');p.add_argument('--result');p.add_argument('--request-id');p.add_argument('--object-id')
    p.add_argument('--evidence');p.add_argument('--ready')
    a=p.parse_args();os.umask(0o077)
    if a.operation=='init-local-store':
        require(a.object_root is not None,'object_root_required')
        LocalFSBackend(a.object_root,create=True)
        return {'created':'offline_local_object_store','drive_calls':0}
    require(a.pin is not None,'pin_path_required')
    if a.operation=='new-deployment':
        require(a.session and a.native_task_id,'session_and_native_task_identity_required')
        pin=deployment(a.session,a.native_task_id,seconds=a.seconds,max_requests=a.max_requests,scope=a.scope)
        write_new(a.pin,pin.raw)
        return {'deployment':pin.oid,'native_admission_verified':False,'next':'provision each role journal once with this trusted pin'}
    pin=Object.parse(read_private_file(a.pin,131072))
    require(a.journal is not None,'journal_path_required')
    if a.operation=='provision-journal':
        require(a.role is not None,'role_required')
        Journal.provision(a.journal,pin,a.role)
        return {'provisioned':a.role,'fresh_session_only':True}
    worker_operation=a.operation in {'worker-next','worker-complete'}
    role=('worker' if worker_operation else 'controller') if a.operation not in {'recover-publication','status'} else a.role
    require(role is not None,'role_required')
    journal=Journal(a.journal,pin,role)
    messages=backend(a)
    controls=[a.docs_client_factory,a.control_document_id,a.control_tab_id,a.control_id,a.control_writer_identity]
    if any(controls):
        require(all(controls),'complete_control_configuration_required')
        module,separator,name=a.docs_client_factory.partition(':')
        require(separator and module and name and not name.startswith('_'),'invalid_docs_client_factory')
        from .control import GoogleDocsCASControlStore,SessionCoordinator
        from .controlled import CASController,CASWorker
        client=getattr(importlib.import_module(module),name)()
        control=GoogleDocsCASControlStore(client,a.control_document_id,a.control_tab_id,a.control_id,
            pin.body['identity']['session_id'],a.control_writer_identity)
        coordinator=SessionCoordinator(control,messages)
        actor=(CASWorker if role=='worker' else CASController)(journal,messages,coordinator)
    else:
        actor=(Worker if role=='worker' else Controller)(journal,messages)
    if a.operation=='publish-deployment':return {'publication':actor.publish_deployment()}
    if a.operation=='worker-next':
        require(a.save is not None and not Path(a.save).exists(),'new_permit_path_required')
        require(0 <= a.wait <= 20,'invalid_wait')
        end=time.monotonic()+a.wait
        while True:
            permit=actor.start_next()
            if permit is not None:
                write_new(a.save,canonical(permit))
                return {'request_id':permit['request_id'],'permit_saved':True,
                        'one_use_native_boundary':True,'native_invoked_by_python':False}
            if time.monotonic()>=end:return {'status':'pending','proves_unstarted':False}
            time.sleep(min(1,max(0,end-time.monotonic())))
    if a.operation=='worker-complete':
        require(a.permit is not None and a.result is not None,'permit_and_result_required')
        permit=json.loads(read_private_file(a.permit,MAX_BYTES))
        result=json.loads(read_private_file(a.result,MAX_BYTES))
        require(isinstance(result,dict),'result_required')
        return {'result_id':actor.complete(permit,result['text'] if set(result)=={'text'} else result)}
    if a.operation=='controller-result':
        require(a.request_id is not None,'request_id_required')
        result=actor.result(a.request_id)
        return result.value if result else {'status':'pending','proves_unstarted':False}
    if a.operation=='recover-publication':
        require(a.object_id is not None,'object_id_required')
        return {'publication':actor.recover_publication(a.object_id),'native_retry':False}
    if a.operation=='status':
        objects=actor.reconcile()
        return {'deployment':pin.oid,'objects':len(objects),'kinds':sorted(o.body['kind'] for o in objects.values())}
    require(pin.body['payload']['scope']=='text_only' or a.long_session,'tools_require_long_session_handler')
    facade=RemoteResponsesFacade(actor,port=a.port,request_deadline=a.deadline,long_session=a.long_session,
        poll_interval=a.poll_interval,heartbeat_interval=a.heartbeat_interval)
    try:
        if a.operation=='close-session':return facade.close_session()
        if a.operation=='ack-delivery':
            require(a.request_id and a.evidence,'request_and_observation_evidence_required')
            return {'receipt':facade.confirm_delivery(a.request_id,a.evidence)}
        require(a.operation=='serve','unsupported_operation')
        facade.start()
        ready={'base_url':facade.base_url,'pid':os.getpid(),'deployment':pin.oid,
               'transport':a.transport,'session_control':'docs_cas' if a.control_document_id else 'single_writer',
               'native_invoked_by_python':False,'scope':pin.body['payload']['scope'],
               'expires':pin.body['payload']['expires'],'max_requests':pin.body['payload']['max_requests'],
               'request_bytes_limit':1048576,'journal_bytes_limit':100663296,'session_transcript_bytes_limit':67108864,
               'late_result_recovery':a.long_session,'automatic_wake':False}
        if a.ready:write_new(a.ready,canonical(ready))
        print(json.dumps(ready),flush=True)
        try:
            while not facade.stop.wait(.25):
                # Long mode remains available for read-only late-result recovery.
                # Expired pins never authorize a new admission or begin.
                if not a.long_session and time.time()>=pin.body['payload']['expires']:break
        except KeyboardInterrupt:pass
        return {'status':'stopped','unconfirmed_delivery_requires_reconciliation':True}
    finally:facade.close()


if __name__=='__main__':
    try:print(json.dumps(main()),flush=True)
    except (ProtocolError,OSError,ValueError,TypeError,KeyError,AttributeError,ImportError) as exc:
        print(json.dumps({'error':str(exc) if isinstance(exc,ProtocolError) else type(exc).__name__}),flush=True)
        raise SystemExit(1)
