"""OFFLINE packet preparation/validation for a bounded direct-connector smoke.

No connector/network calls, credentials, or native execution. Actual writes must
be direct authorized tools. Saved snapshots/revisions are private runtime inputs.
"""
import argparse
import copy
import json
from pathlib import Path
from .backend import filename, read_private_file, validate_reference
from .cli import write_new
from .control import GoogleDocsCASControlStore, SessionCoordinator, initial_state, block_for, _ref
from .model import Object, canonical, deployment, hash_bytes, require


def structured(value):
    return value['structuredContent'] if isinstance(value,dict) and 'structuredContent' in value else value


def read_json(path):return json.loads(read_private_file(Path(path),2097152))


class ConnectorEvidenceMessages:
    """Known-ID evidence only. No exhaustive listing or raw-REST conformance claim.

    entries: [{reference:{object_id,locator}, file:<canonical byte file>,
               metadata:<saved get_file_metadata structuredContent file>}].
    Normalized provider schema lacks trash state; this is explicitly reported.
    """
    def __init__(self,folder_id,entries):
        self.folder_id,self.entries=folder_id,entries

    def fetch(self,reference):
        validate_reference(reference)
        loc=reference['locator']
        require(loc['backend']=='drive' and loc['folder_id']==self.folder_id,'message_backend_or_folder_mismatch')
        matches=[x for x in self.entries if x['reference']==reference]
        require(len(matches)==1,'exact_message_evidence_required')
        entry=matches[0];meta=structured(read_json(entry['metadata']))
        require(isinstance(meta,dict) and meta.get('id')==loc['file_id'] and
                isinstance(meta.get('title'),str) and meta.get('mime_type')=='application/json' and
                type(meta.get('parent_ids')) is list and all(isinstance(x,str) for x in meta['parent_ids']) and
                self.folder_id in meta['parent_ids'],'normalized_metadata_scope_mismatch')
        obj=Object.parse(read_private_file(Path(entry['file']),131072))
        require(obj.oid==reference['object_id'] and meta['title']==filename(obj),'message_reference_mismatch')
        return obj


def prepare(snapshot_resource,pin,config,messages,kind,args,operation_id):
    store=GoogleDocsCASControlStore(None,config['document_id'],config['tab_id'],config['control_id'],
                                   pin.body['identity']['session_id'],config['writer_identity'])
    snapshot=store.snapshot_from_document(structured(snapshot_resource))
    plan=SessionCoordinator(store,messages).plan(kind,args,operation_id,snapshot=snapshot)
    require(plan['status']=='planned' and plan['permit'] is None,'fresh_transition_plan_required')
    return {'kind':kind,'operation_id':operation_id,'candidate':plan['state'],
            'tool_arguments':store.prepare_update(snapshot,plan['state']),
            'config':config,'pin':pin.value,'native_permit':None,
            'scope':'offline_direct_connector_plan','trash_state_verified':False}


def verify(plan,response,readback):
    response=structured(response)
    require(isinstance(response,dict) and response.get('documentId')==plan['config']['document_id'],
            'cas_response_document_mismatch')
    replies=response.get('replies')
    require(type(replies) is list and len(replies)==1 and isinstance(replies[0],dict) and
            isinstance(replies[0].get('replaceAllText'),dict) and
            type(replies[0]['replaceAllText'].get('occurrencesChanged')) is int and
            replies[0]['replaceAllText']['occurrencesChanged']==1,'cas_fresh_exact_match_required')
    pin=Object.parse(canonical(plan['pin']));config=plan['config']
    store=GoogleDocsCASControlStore(None,config['document_id'],config['tab_id'],config['control_id'],
                                   pin.body['identity']['session_id'],config['writer_identity'])
    snapshot=store.snapshot_from_document(structured(readback));state=snapshot.state
    write_control=response.get('writeControl')
    require(isinstance(write_control,dict) and isinstance(write_control.get('requiredRevisionId'),str) and
            write_control['requiredRevisionId'] and not write_control.get('targetRevisionId') and
            write_control['requiredRevisionId']==snapshot.revision_id and
            write_control['requiredRevisionId']!=plan['tool_arguments']['write_control']['requiredRevisionId'],
            'cas_response_readback_revision_mismatch')
    expected=plan['candidate']['operations'][-1]
    require(expected in state['operations'] and state['binding']==plan['candidate']['binding'],
            'cas_operation_readback_mismatch')
    return state


def consume_begin(plan,response,readback,journal_root):
    """Persist local one-use consumption BEFORE returning a native input permit."""
    import time
    state=verify(plan,response,readback)
    require(plan['kind']=='begin' and state['phase']=='DISPATCH_INTENT' and
            state['dispatch']==plan['candidate']['dispatch'],'fresh_begin_required')
    require(time.time()<state['binding']['expires'],'control_deployment_expired')
    root=Path(journal_root)
    pin=Object.parse(read_private_file(root/'pin.json',131072))
    require(pin.oid==state['binding']['deployment_hash'],'worker_runtime_pin_mismatch')
    permit={'control_id':state['control_id'],'request_reference':_ref(state['request']),
            'dispatch_id':state['dispatch']['id'],'native_task_id':state['binding']['native_task_id'],
            'operation_id':plan['operation_id']}
    write_new(root/('consumed-'+plan['operation_id']+'.json'),canonical(permit))
    return permit


def save_object(root,obj):
    path=Path(root)/filename(obj);write_new(path,obj.raw)
    return {'object_id':obj.oid,'path':str(path),'name':path.name}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation',choices=['init','request','claim-started','result','receipt','plan','verify','consume-begin'])
    for name in ['root','session','native-task-id','control-id','text-file','request-id','request-file','claim-file',
                 'started-file','result-file','evidence','snapshot','config','manifest','arguments','operation-id',
                 'plan-file','response','readback','save']:
        p.add_argument('--'+name)
    p.add_argument('--kind',choices=['admit','claim','begin','result','receipt','ambiguous','rebind'])
    a=p.parse_args();root=Path(a.root)
    if a.operation=='init':
        root.mkdir(mode=0o700,parents=True,exist_ok=False)
        pin=deployment(a.session,a.native_task_id,seconds=900)
        write_new(root/'pin.json',pin.raw)
        text=block_for(initial_state(pin,a.control_id))
        write_new(root/'control-block.txt',text.encode())
        # Existing empty Doc contributes final newline; only this string is inserted.
        write_new(root/'control-insert-text.json',canonical({'text':text[:-1]}))
        return {'pin':str(root/'pin.json'),'control_block':str(root/'control-block.txt'),
                'insert_text':str(root/'control-insert-text.json'),'native_admission_verified':False}
    pin=Object.parse(read_private_file(root/'pin.json',131072))
    if a.operation=='request':
        text=read_private_file(Path(a.text_file),65536).decode()
        return save_object(root,Object.make(pin.body['identity'],'request',1,pin.oid,{'text':text}))
    if a.operation=='claim-started':
        claim=Object.make(pin.body['identity'],'claim',1,pin.oid,{'status':'claimed'},{'request':a.request_id})
        dispatch=hash_bytes(canonical({'request':a.request_id,'incarnation':pin.body['identity']['worker_journal_id']}))
        started=Object.make(pin.body['identity'],'started',1,pin.oid,{'status':'dispatch_intent','dispatch_id':dispatch},
                            {'request':a.request_id,'claim':claim.oid})
        return {'claim':save_object(root,claim),'started':save_object(root,started),'dispatch_id':dispatch}
    if a.operation=='result':
        req=Object.parse(read_private_file(Path(a.request_file),131072))
        started=Object.parse(read_private_file(Path(a.started_file),131072))
        text=read_private_file(Path(a.text_file),65536).decode()
        return save_object(root,Object.make(pin.body['identity'],'result',1,pin.oid,{'text':text},
                                            {'request':req.oid,'started':started.oid}))
    if a.operation=='receipt':
        result=Object.parse(read_private_file(Path(a.result_file),131072))
        return save_object(root,Object.make(pin.body['identity'],'receipt',1,pin.oid,
                           {'status':'delivered','evidence':a.evidence},{'request':a.request_id,'result':result.oid}))
    if a.operation=='plan':
        config=read_json(a.config);entries=read_json(a.manifest)
        plan=prepare(read_json(a.snapshot),pin,config,ConnectorEvidenceMessages(config['folder_id'],entries),
                     a.kind,read_json(a.arguments),a.operation_id)
        write_new(a.save,canonical(plan));return {'plan_saved':a.save,'native_permit':None,'phase':plan['candidate']['phase']}
    plan=read_json(a.plan_file);response=read_json(a.response);readback=read_json(a.readback)
    if a.operation=='verify':
        state=verify(plan,response,readback)
        return {'verified':True,'phase':state['phase'],'native_permit':None}
    permit=consume_begin(plan,response,readback,root)
    return {'fresh_one_use_permit':permit,'native_invoked_by_python':False}


if __name__=='__main__':
    print(json.dumps(main(),indent=2))
