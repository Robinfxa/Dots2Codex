"""Experimental session control separated from immutable message storage.

Google Docs requiredRevisionId is the compare-and-swap authority here. Neither
Drive filename uniqueness nor the local MessageStore supplies this authority.
No native execution occurs in this module. No resources are created automatically.
"""
import copy
import json
import re
import time
from dataclasses import dataclass
from typing import Protocol
from .model import Object, ProtocolError, canonical, hash_bytes, require, valid_hash, MAX_REQUESTS, MAX_SESSION_SECONDS
from .backend import validate_reference

BEGIN = 'DOTS2CODEX_CONTROL_BEGIN_V1\n'
END = '\nDOTS2CODEX_CONTROL_END_V1\n'
VERSION = 'dots-session-control/0'
MAX_CONTROL_BYTES = 1024 * 1024
MAX_OPERATIONS = MAX_REQUESTS * 7 + 16


class CASConflict(ProtocolError):
    """Server definitely rejected the required revision; nothing in the batch ran."""


class CASUnknown(ProtocolError):
    """Commit may have happened. Read back operation history; never infer absence."""


class MessageStore(Protocol):
    def reserve(self): ...
    def publish(self, obj: Object, reservation): ...
    def scan(self, deployment_id: str): ...
    def reference(self, obj, publication): ...
    def fetch(self, reference): ...


class SessionControlStore(Protocol):
    def read(self): ...
    def compare_and_swap(self, snapshot, new_state): ...


@dataclass(frozen=True)
class ControlSnapshot:
    document_id: str
    tab_id: str
    writer_identity: str
    revision_id: str
    block: str
    acquired_at: float

    @property
    def state(self):
        return decode_block(self.block)


def block_for(state):
    validate_state(state)
    block = BEGIN + canonical(state, max_bytes=MAX_CONTROL_BYTES).decode('utf-8') + END
    require(len(block.encode()) <= MAX_CONTROL_BYTES, 'control_block_too_large')
    return block


def decode_block(block):
    require(isinstance(block,str) and block.startswith(BEGIN) and block.endswith(END) and
            block.count(BEGIN)==1 and block.count(END)==1,'invalid_control_block')
    try:
        state=json.loads(block[len(BEGIN):-len(END)])
    except (ValueError,UnicodeError,RecursionError):
        raise ProtocolError('invalid_control_json') from None
    require(block_for(state)==block,'noncanonical_control_block')
    return state


def binding_for(pin):
    pin.validate();require(pin.body['kind']=='deployment','deployment_pin_required')
    i=pin.body['identity']
    value = dict(deployment_hash=pin.oid,identity=copy.deepcopy(i),created=pin.body['payload']['created'],expires=pin.body['payload']['expires'],worker_id=i['worker_id'],generation=i['generation'],
                native_task_id=i['native_task_id'],journal_id=i['worker_journal_id'])
    from .selection import pin_selection
    selection = pin_selection(pin)
    if selection is not None: value['selection'] = selection
    return value


def initial_state(pin, control_id):
    require(isinstance(control_id,str) and re.fullmatch('[A-Za-z0-9_-]{1,64}',control_id),'invalid_control_id')
    state=dict(contract=VERSION,control_id=control_id,session_id=pin.body['identity']['session_id'],
               control_epoch=0,binding=binding_for(pin),phase='IDLE',admissions=0,
               max_requests=pin.body['payload']['max_requests'],request=None,claim=None,
               dispatch=None,result=None,receipt=None,history=[],operations=[])
    validate_state(state)
    return state


def validate_state(s):
    require(isinstance(s,dict) and set(s)=={'contract','control_id','session_id','control_epoch',
            'binding','phase','admissions','max_requests','request','claim','dispatch','result',
            'receipt','history','operations'} | ({'closed'} if 'closed' in s else set()),'invalid_control_state')
    require(type(s.get('closed', False)) is bool, 'invalid_control_closed')
    require(s['contract']==VERSION and isinstance(s['control_id'],str) and
            re.fullmatch('[A-Za-z0-9_-]{1,64}',s['control_id']) and isinstance(s['session_id'],str),
            'invalid_control_identity')
    require(type(s['control_epoch']) is int and 0<=s['control_epoch']<=MAX_OPERATIONS and
            type(s['max_requests']) is int and 1<=s['max_requests']<=MAX_REQUESTS and
            type(s['admissions']) is int and 0<=s['admissions']<=s['max_requests'],'invalid_control_budget')
    b=s['binding']
    require(isinstance(b,dict) and set(b)=={'deployment_hash','identity','created','expires','worker_id','generation','native_task_id','journal_id'} | ({'selection'} if 'selection' in b else set()) and
            valid_hash(b['deployment_hash']) and type(b['generation']) is int and b['generation']>=1 and
            all(isinstance(b[k],str) and re.fullmatch('[A-Za-z0-9_:/.-]{1,256}',b[k])
                for k in ('worker_id','native_task_id','journal_id')) and isinstance(b['identity'],dict) and
            b['identity'].get('worker_id')==b['worker_id'] and b['identity'].get('generation')==b['generation'] and
            b['identity'].get('native_task_id')==b['native_task_id'] and b['identity'].get('worker_journal_id')==b['journal_id'] and
            b['identity'].get('session_id')==s['session_id'],'invalid_control_binding')
    if 'selection' in b:
        from .selection import validate_selection
        validate_selection(b['selection'])
    require(type(b['created']) is int and type(b['expires']) is int and 1<=b['expires']-b['created']<=MAX_SESSION_SECONDS,'invalid_control_lifetime')
    # Validate the complete identity using the common envelope schema.
    Object.make(b['identity'],'deployment',0,None,{'created':0,'expires':1,'max_requests':s['max_requests'],
                'scope':'text_only','ownership':'externally_pinned_single_writer'})
    phase=s['phase']
    require(phase in {'IDLE','REQUESTED','CLAIMED','DISPATCH_INTENT','AMBIGUOUS','RESULT_COMMITTED','DELIVERED'},'invalid_control_phase')
    if phase=='IDLE':
        require(all(s[k] is None for k in ('request','claim','dispatch','result','receipt')),'invalid_idle_state')
    else:
        require(isinstance(s['request'],dict) and set(s['request'])=={'object_id','locator','message_seq'} and
                valid_hash(s['request']['object_id']) and type(s['request']['message_seq']) is int and
                1<=s['request']['message_seq']<=s['max_requests'],'invalid_control_request')
        validate_reference(_ref(s['request']))
        if phase=='REQUESTED':
            require(all(s[k] is None for k in ('claim','dispatch','result','receipt')),'invalid_requested_state')
        else:
            c=s['claim']
            require(isinstance(c,dict) and set(c)=={'id','worker_id','generation'} and
                    isinstance(c['id'],str) and re.fullmatch('[A-Za-z0-9_-]{1,64}',c['id']) and
                    c['worker_id']==b['worker_id'] and c['generation']==b['generation'],'invalid_control_claim')
            if phase=='CLAIMED':
                require(all(s[k] is None for k in ('dispatch','result','receipt')),'invalid_claimed_state')
            else:
                d=s['dispatch']
                require(isinstance(d,dict) and set(d)=={'id','worker_id','generation'} and
                        isinstance(d['id'],str) and re.fullmatch('[A-Za-z0-9_-]{1,64}',d['id']) and
                        d['worker_id']==b['worker_id'] and d['generation']==b['generation'],'invalid_control_dispatch')
                if phase in {'RESULT_COMMITTED','DELIVERED'}:_validate_result_record(s['result'])
                else:require(s['result'] is None,'invalid_control_result')
                if phase=='DELIVERED':validate_reference(s['receipt'])
                else:require(s['receipt'] is None,'invalid_control_receipt')
    require(type(s['operations']) is list and len(s['operations'])==s['control_epoch'] and len(s['operations'])<=MAX_OPERATIONS,
            'invalid_control_operations')
    ids=set()
    for op in s['operations']:
        require(isinstance(op,dict) and set(op)=={'id','kind','arguments_hash'} and
                isinstance(op['id'],str) and re.fullmatch('[A-Za-z0-9_-]{1,64}',op['id']) and
                op['id'] not in ids and op['kind'] in {'admit','claim','begin','result','receipt','ambiguous','rebind','close'} and
                valid_hash(op['arguments_hash']),'invalid_control_operation')
        ids.add(op['id'])
    require(type(s['history']) is list and len(s['history'])<=s['max_requests'] and
            s['admissions']==len(s['history'])+(s['request'] is not None),'invalid_control_history')
    for h in s['history']:
        require(isinstance(h,dict) and set(h)=={'phase','request','binding','result','receipt'} and
                h['phase'] in {'RETIRED_BEFORE_DISPATCH','DELIVERED'} and
                isinstance(h['request'],dict) and valid_hash(h['request'].get('object_id')),
                'invalid_control_history')
        require(set(h['request'])=={'object_id','locator','message_seq'} and
                type(h['request']['message_seq']) is int and 1<=h['request']['message_seq']<=s['max_requests'],'invalid_control_history')
        validate_reference(_ref(h['request']))
        # Reuse full binding validation without recursively retaining history.
        archived=dict(s);archived.update(binding=h['binding'],phase='IDLE',admissions=0,request=None,claim=None,
            dispatch=None,result=None,receipt=None,history=[],operations=[],control_epoch=0)
        validate_state(archived)
        if h['phase']=='DELIVERED':
            _validate_result_record(h['result']);validate_reference(h['receipt'])
        else:require(h['result'] is None and h['receipt'] is None,'invalid_control_history')
    return s


def _ref(request):
    return {'object_id':request['object_id'],'locator':copy.deepcopy(request['locator'])}


def _validate_result_record(result):
    require(isinstance(result,dict) and set(result)=={'reference','dependencies'} and
            isinstance(result['dependencies'],dict) and set(result['dependencies'])=={'request','claim','started'},
            'invalid_control_result')
    validate_reference(result['reference'])
    for reference in result['dependencies'].values():validate_reference(reference)


def _document_text(document, tab_id):
    require(isinstance(document,dict),'invalid_document_snapshot')
    def no_suggestions(value):
        if isinstance(value,dict):
            for key,child in value.items():
                require(not (key.startswith('suggested') and child),'suggested_control_edit')
                no_suggestions(child)
        elif isinstance(value,list):
            for child in value:no_suggestions(child)
    no_suggestions(document)
    require(document.get('suggestionsViewMode')=='SUGGESTIONS_INLINE','inline_suggestions_view_required')
    tabs=document.get('tabs')
    require(type(tabs) is list and len(tabs)==1,'dedicated_single_tab_required')
    tab=tabs[0]
    require(isinstance(tab,dict) and not tab.get('childTabs'),'control_tab_mismatch')
    if 'tabProperties' in tab or 'documentTab' in tab:
        require('tabId' not in tab and tab.get('tabProperties',{}).get('tabId')==tab_id and
                isinstance(tab.get('documentTab'),dict),'control_tab_mismatch')
        dt=tab['documentTab']
    else:
        # Verified current connector normalized shape, not guessed raw API fields.
        require(tab.get('tabId')==tab_id and tab.get('parentTabId') is None,'control_tab_mismatch')
        dt=tab
    require(not dt.get('headers') and not dt.get('footers'),'dedicated_document_required')
    body=dt.get('body',{}).get('content')
    require(type(body) is list,'complete_document_body_required')
    pieces=[]
    for element in body:
        require(isinstance(element,dict),'invalid_document_element')
        if 'sectionBreak' in element:
            require(not pieces,'unexpected_section_break')
            continue
        require('paragraph' in element and 'table' not in element and 'tableOfContents' not in element,
                'plain_control_document_required')
        for run in element['paragraph'].get('elements',[]):
            require(isinstance(run,dict) and 'textRun' in run and
                    isinstance(run['textRun'].get('content'),str),'plain_control_document_required')
            require(not run.get('suggestedInsertionIds') and not run.get('suggestedDeletionIds'),
                    'suggested_control_edit')
            pieces.append(run['textRun']['content'])
    return ''.join(pieces)


class GoogleDocsCASControlStore:
    """Pinned dedicated Docs control block. Client executes direct public Docs calls.

    get_document(document_id) -> full resource with tabs, revisionId, documentId.
    batch_update_document(document_id, requests, write_control) -> batch response.
    Client must distinguish proven stale-revision rejection via CASConflict; every
    timeout/unknown/malformed outcome is CASUnknown. No retries happen in this port.
    """
    def __init__(self, client, document_id, tab_id, control_id, session_id, writer_identity, *, snapshot_ttl=30):
        for value in (document_id,tab_id,control_id,session_id,writer_identity):
            require(isinstance(value,str) and 1<=len(value)<=256,'invalid_control_configuration')
        require(type(snapshot_ttl) in (int,float) and 0<snapshot_ttl<=60,'invalid_snapshot_ttl')
        self.client,self.document_id,self.tab_id=client,document_id,tab_id
        self.control_id,self.session_id,self.writer_identity=control_id,session_id,writer_identity
        self.snapshot_ttl=snapshot_ttl

    def read(self):
        return self.snapshot_from_document(self.client.get_document(self.document_id))

    def snapshot_from_document(self,document):
        # Pure parsing entrypoint for a directly obtained connector structuredContent.
        require(isinstance(document,dict) and document.get('documentId')==self.document_id,'control_document_mismatch')
        revision=document.get('revisionId')
        require(isinstance(revision,str) and 1<=len(revision)<=1024,'editable_revision_required')
        # State and revision are extracted from this SAME full get under this writer.
        block=_document_text(document,self.tab_id)
        state=decode_block(block)
        require(state['control_id']==self.control_id and state['session_id']==self.session_id,'control_pin_mismatch')
        return ControlSnapshot(self.document_id,self.tab_id,self.writer_identity,revision,block,time.monotonic())

    def prepare_update(self,snapshot,new_state):
        require(isinstance(snapshot,ControlSnapshot) and snapshot.document_id==self.document_id and
                snapshot.tab_id==self.tab_id and snapshot.writer_identity==self.writer_identity,'snapshot_writer_mismatch')
        require(0<=time.monotonic()-snapshot.acquired_at<=self.snapshot_ttl,'control_snapshot_expired')
        old=snapshot.state;validate_state(new_state)
        require(new_state['control_id']==self.control_id and new_state['session_id']==self.session_id and
                new_state['control_epoch']==old['control_epoch']+1 and
                new_state['operations'][:-1]==old['operations'],'invalid_control_transition')
        replacement=block_for(new_state)
        # Docs preserves its mandatory final paragraph newline even when a match
        # includes it. Exclude exactly that newline from both API strings; the
        # canonical full-document parser still requires precisely one newline.
        request={'replaceAllText':{'containsText':{'text':snapshot.block[:-1],'matchCase':True,'searchByRegex':False},
                                  'replaceText':replacement[:-1],'tabsCriteria':{'tabIds':[self.tab_id]}}}
        return {'document_id':self.document_id,'requests':[request],
                'write_control':{'requiredRevisionId':snapshot.revision_id}}

    def compare_and_swap(self,snapshot,new_state):
        prepared=self.prepare_update(snapshot,new_state)
        try:
            response=self.client.batch_update_document(prepared['document_id'],prepared['requests'],prepared['write_control'])
        except CASConflict:raise
        except Exception as exc:
            raise CASUnknown('control_write_outcome_unknown') from exc
        try:
            replies=response['replies']
            require(type(replies) is list and len(replies)==1 and
                    type(replies[0]['replaceAllText']['occurrencesChanged']) is int and
                    replies[0]['replaceAllText']['occurrencesChanged']==1,'control_replace_not_exactly_one')
            require(response.get('documentId')==self.document_id,'control_response_document_mismatch')
        except (KeyError,TypeError,ProtocolError) as exc:
            raise CASUnknown('control_write_unverified') from exc
        return new_state


class SessionCoordinator:
    """Legal transitions; successful fresh begin alone returns a one-use permit."""
    def __init__(self,store,message_store):self.store,self.messages=store,message_store

    def _fetch(self,reference):
        validate_reference(reference)
        obj=self.messages.fetch(reference)
        require(isinstance(obj,Object) and obj.oid==reference['object_id'],'message_reference_mismatch')
        return obj

    def transition(self,kind,args,operation_id,*,snapshot=None):
        return self._transition(kind,args,operation_id,snapshot=snapshot,commit=True)

    def plan(self,kind,args,operation_id,*,snapshot):
        return self._transition(kind,args,operation_id,snapshot=snapshot,commit=False)

    def _transition(self,kind,args,operation_id,*,snapshot,commit):
        require(isinstance(operation_id,str) and re.fullmatch('[A-Za-z0-9_-]{1,64}',operation_id),'invalid_operation_id')
        snapshot=self.store.read() if snapshot is None else snapshot
        shapes={'admit':{'request'},'rebind':{'deployment'},'claim':{'binding','claim_id'},
                'begin':{'binding','claim_id','dispatch_id'},'ambiguous':{'binding','dispatch_id'},
                'result':{'binding','dispatch_id','request','claim','started','result'},
                'receipt':{'binding','receipt'},'close':{'binding'}}
        require(kind in shapes and isinstance(args,dict) and set(args)==shapes[kind],'invalid_control_arguments')
        old=snapshot.state;s=copy.deepcopy(old);ah=hash_bytes(canonical(args))
        for op in old['operations']:
            if op['id']==operation_id:
                require(op['kind']==kind and op['arguments_hash']==ah,'control_operation_conflict')
                return {'status':'already_applied','permit':None,'state':old}
        require(len(s['operations'])<MAX_OPERATIONS,'control_operation_budget_exceeded')
        phase=s['phase'];binding=s['binding']
        if kind in {'admit','claim','begin','rebind'}:
            require(not s.get('closed',False), 'control_session_closed')
            require(time.time()<binding['expires'],'control_deployment_expired')
        if kind=='admit':
            require(phase in {'IDLE','DELIVERED'},'control_request_inflight')
            require(s['admissions']<s['max_requests'],'control_request_budget_exceeded')
            req=self._fetch(args['request']);self._object_scope(req,s,'request')
            if 'responses_request' in req.body['payload']:
                from .selection import validate_request_selection
                validate_request_selection(req.body['payload']['responses_request'],binding.get('selection'))
            else:
                require('selection' not in binding,'selected_session_requires_explicit_responses_request')
            if phase=='DELIVERED':
                require(req.body['seq']==s['request']['message_seq']+1 and
                        req.body['links']=={'previous_receipt':s['receipt']['object_id']},'control_predecessor_mismatch')
                self._archive(s,'DELIVERED')
            else:
                require(req.body['seq']==1 and not req.body['links'],'control_first_request_required')
            require(req.oid not in {h['request']['object_id'] for h in s['history']},'control_request_replay')
            s.update(phase='REQUESTED',request={**copy.deepcopy(args['request']),'message_seq':req.body['seq']},
                     claim=None,dispatch=None,result=None,receipt=None,admissions=s['admissions']+1)
        elif kind=='rebind':
            require(phase in {'IDLE','REQUESTED','CLAIMED','DELIVERED'},'control_rebind_after_dispatch_forbidden')
            pin=Object.parse(canonical(args['deployment']));new=binding_for(pin)
            require('selection' not in binding and 'selection' not in new,
                    'selected_session_rebind_requires_new_session')
            require(time.time()<new['expires'],'control_rebind_deployment_expired')
            require(pin.body['identity']['session_id']==s['session_id'] and
                    new['generation']==binding['generation']+1 and new['journal_id']!=binding['journal_id'] and
                    new['native_task_id']!=binding['native_task_id'] and
                    pin.body['payload']['max_requests']==s['max_requests'],'invalid_rebind_deployment')
            if s['request'] is not None:self._archive(s,'DELIVERED' if phase=='DELIVERED' else 'RETIRED_BEFORE_DISPATCH')
            s.update(binding=new,phase='IDLE',request=None,claim=None,dispatch=None,result=None,receipt=None)
        else:
            require(args.get('binding')==binding,'control_stale_worker_binding')
            if kind=='close':
                require(not s.get('closed',False), 'control_session_closed')
                s['closed']=True
            elif kind=='claim':
                require(phase=='REQUESTED','control_not_requestable')
                s.update(phase='CLAIMED',claim={'id':args['claim_id'],'worker_id':binding['worker_id'],'generation':binding['generation']})
            elif kind=='begin':
                require(phase=='CLAIMED' and args['claim_id']==s['claim']['id'],'control_not_beginable')
                expected_dispatch=hash_bytes(canonical({'request':s['request']['object_id'],'incarnation':binding['journal_id']}))
                require(args['dispatch_id']==expected_dispatch,'control_dispatch_id_mismatch')
                s.update(phase='DISPATCH_INTENT',dispatch={'id':args['dispatch_id'],'worker_id':binding['worker_id'],'generation':binding['generation']})
            elif kind=='ambiguous':
                require(phase=='DISPATCH_INTENT' and args['dispatch_id']==s['dispatch']['id'],'control_unknown_dispatch')
                s['phase']='AMBIGUOUS'
            elif kind=='result':
                require(phase in {'DISPATCH_INTENT','AMBIGUOUS'} and args['dispatch_id']==s['dispatch']['id'],'control_unknown_dispatch')
                graph={k:self._fetch(args[k]) for k in ('request','claim','started','result')}
                for k,obj in graph.items():self._object_scope(obj,s,k)
                req,claim,started,result=(graph[k] for k in ('request','claim','started','result'))
                require(args['request']==_ref(s['request']) and req.oid==s['request']['object_id'] and all(obj.body['seq']==s['request']['message_seq'] for obj in graph.values()) and
                        claim.body['links']=={'request':req.oid} and
                        started.body['links']=={'request':req.oid,'claim':claim.oid} and
                        started.body['payload']['dispatch_id']==s['dispatch']['id'] and
                        result.body['links']=={'request':req.oid,'started':started.oid},'control_result_graph_mismatch')
                if 'response_result' in result.body['payload']:
                    from .wire import result_item
                    require('responses_request' in req.body['payload'],'tool_result_requires_responses_request')
                    result_item(result.body['payload'],req.body['payload']['responses_request'],req.oid,req.body['payload']['scope'])
                s.update(phase='RESULT_COMMITTED',result={'reference':copy.deepcopy(args['result']),
                    'dependencies':{k:copy.deepcopy(args[k]) for k in ('request','claim','started')}})
            elif kind=='receipt':
                require(phase=='RESULT_COMMITTED','control_result_uncommitted')
                receipt=self._fetch(args['receipt']);self._object_scope(receipt,s,'receipt')
                require(receipt.body['seq']==s['request']['message_seq'] and receipt.body['links']=={'request':s['request']['object_id'],'result':s['result']['reference']['object_id']},'control_receipt_mismatch')
                s.update(phase='DELIVERED',receipt=copy.deepcopy(args['receipt']))
            else:raise ProtocolError('unknown_control_operation')
        s['control_epoch']+=1
        s['operations'].append({'id':operation_id,'kind':kind,'arguments_hash':ah})
        validate_state(s)
        if not commit:
            return {'status':'planned','permit':None,'state':s,'operation_id':operation_id}
        self.store.compare_and_swap(snapshot,s)
        permit=None
        if kind=='begin':
            if time.time()>=s['binding']['expires']:
                raise CASUnknown('control_begin_committed_after_expiry')
            permit={'control_id':s['control_id'],'request_id':s['request']['object_id'],
                    'generation':s['binding']['generation'],'native_task_id':s['binding']['native_task_id'],
                    'dispatch_id':s['dispatch']['id'],'operation_id':operation_id}
        return {'status':'applied','permit':permit,'state':s}

    @staticmethod
    def _archive(s,phase):
        s['history'].append({k:copy.deepcopy(s[k]) for k in ('request','binding','result','receipt')}|{'phase':phase})

    @staticmethod
    def _object_scope(obj,state,kind):
        from .session import _check_payload
        _check_payload(obj)
        b=obj.body;binding=state['binding']
        require(b['kind']==kind and b['deployment']==binding['deployment_hash'] and
                b['identity']==binding['identity'],'control_message_scope_mismatch')
