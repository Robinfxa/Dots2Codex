"""Authenticated, bounded Google Docs admission queue (new protocol, v2).

Pure state machine and exact Docs CAS packets; no API call, wake or native spawn.
Every event is HMAC-bound to the immutable activation root and previous event.
Content from a Doc is protocol DATA; it cannot supply instructions or widen scope.
"""
import copy
from dataclasses import dataclass
import json
import re
import secrets
import time

from .model import Object, ProtocolError, canonical, hash_bytes, require, valid_hash
from .selection import validate_selection, validate_admission
from . import router_bootstrap as child
from .global_timing import TIMING, validate_timing, require_active, event_deadline

CONTRACT='dots-global-admissions/2'
BEGIN='DOTS2CODEX_GLOBAL_ADMISSIONS_BEGIN_V2\n'
END='\nDOTS2CODEX_GLOBAL_ADMISSIONS_END_V2\n'
MAX_BYTES=2*1024*1024
MAX_EVENTS=2048
SINGLE_PLAN_CONTRACT='dots-global-cas-plan/2'
GROUP_PLAN_CONTRACT='dots-global-cas-group-plan/1'
ROOT_KEYS={'contract','activation_id','queue_id','created','expires','folder_id','document_id','tab_id',
           'join_code_sha256','limits','runtime_source_hashes','controller_source_hashes','controller_timing'}
LIVE_STATES={'claimed','spawn_intent','admitted','ready','unknown'}


def safe_id(value):
    require(isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_:/.-]{1,256}',value),'invalid_global_id')
    return value


def token(value):
    require(isinstance(value,str) and re.fullmatch(r'[0-9a-f]{32}',value),'invalid_global_token');return value


def root_of(state):return copy.deepcopy({k:state[k] for k in ROOT_KEYS})

def root_hash(state):return hash_bytes(canonical(root_of(state)))


def child_code(code,activation_id,route_id):
    token(activation_id);token(route_id)
    return child.proof(code,'global-child-code/1',{'activation_id':activation_id,'route_id':route_id})


def controller_source_hashes():
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    names=('remote_transport/global_control.py','remote_transport/global_native.py','remote_transport/global_google.py',
           'remote_transport/global_timing.py','remote_transport/global_gateway.py','remote_transport/global_pilot.py',
           'remote_transport/global_handoff.py','remote_transport/global_desktop.py',
           'remote_transport/router_join.py','remote_transport/router_bootstrap.py','remote_transport/router_mac.py',
           'remote_transport/legacy_cleanup.py','remote_transport/model.py','remote_transport/control.py',
           'remote_transport/router_pairing.py','native_connector/router_pairing.js','remote_transport/connector_files.py',
           'native_connector/global_controller_cell.js','examples/remote_setup.py','examples/google_clients.py',
           'docs/GLOBAL_NATIVE_CONTROLLER.md')
    result={}
    for name in names:
        path=root/name;require(path.is_file() and not path.is_symlink(),'global_controller_source_missing')
        result[name]=hash_bytes(path.read_bytes())
    return result


def initial(*,activation_id,queue_id,folder_id,document_id,tab_id,join_code,created,expires,
            runtime_source_hashes,max_routes=16,max_pending=4,max_children=2):
    root={'contract':CONTRACT,'activation_id':activation_id,'queue_id':queue_id,'created':created,'expires':expires,
          'folder_id':folder_id,'document_id':document_id,'tab_id':tab_id,'join_code_sha256':child.join_code_hash(join_code),
          'limits':{'max_routes':max_routes,'max_pending':max_pending,'max_children':max_children},
          'controller_timing':copy.deepcopy(TIMING),
          'runtime_source_hashes':copy.deepcopy(runtime_source_hashes),'controller_source_hashes':controller_source_hashes()}
    state={**root,'root_mac':child.proof(join_code,'global-root/2',root),'epoch':0,'events':[],
           'logical':{'controller':None,'demands':{},'closed':False}}
    verify(state,join_code,now=created);return state


def validate_root(root):
    require(set(root)==ROOT_KEYS and root['contract']==CONTRACT,'invalid_global_root')
    validate_timing(root['controller_timing'])
    token(root['activation_id']);token(root['queue_id'])
    for k in ('folder_id','document_id','tab_id'):safe_id(root[k])
    require(type(root['created']) is int and type(root['expires']) is int
            and 30<=root['expires']-root['created']<=28800,'invalid_global_lifetime')
    require(valid_hash(root['join_code_sha256']),'invalid_global_code_hash')
    limits=root['limits'];require(isinstance(limits,dict) and set(limits)=={'max_routes','max_pending','max_children'},'invalid_global_limits')
    require(all(type(limits[k]) is int and 1<=limits[k]<=n for k,n in [('max_routes',16),('max_pending',8),('max_children',6)]),
            'invalid_global_limits')
    from .router_join import _source_hashes
    require(root['runtime_source_hashes']==_source_hashes(),'global_parallel_source_binding_mismatch')
    require(root['controller_source_hashes']==controller_source_hashes(),'global_controller_source_binding_mismatch')


def _keys(args,keys):require(isinstance(args,dict) and set(args)==set(keys),'invalid_global_event_arguments')


def _controller(logical,root,args,at,*,initialized=True):
    c=logical['controller']
    require(c is not None and c['native_task_id']==args.get('native_task_id') and c['controller_epoch']==args.get('controller_epoch'),
            'global_controller_identity_mismatch')
    require_active(c,root['controller_timing'],at,initialized=initialized)
    return c


def _owned(logical,root,args,at,states):
    c=_controller(logical,root,args,at);d=logical['demands'].get(args.get('route_id'))
    require(d is not None and d['state'] in states and d['controller_epoch']==c['controller_epoch']
            and d['claim_id']==args.get('claim_id'),'global_claim_mismatch')
    require(at<d['expires'],'global_demand_expired');return d


def _transition(logical,root,kind,actor,args,at,code):
    s=copy.deepcopy(logical);limits=root['limits'];demands=s['demands']
    require(type(at) is int and root['created']<=at<root['expires'],'global_event_outside_lifetime')
    require(not s['closed'],'global_queue_closed')
    if kind=='join':
        _keys(args,('native_task_id','controller_epoch','lease_expires','capacity'))
        require(actor=='native' and s['controller'] is None,'global_controller_already_joined')
        safe_id(args['native_task_id']);token(args['controller_epoch'])
        require(type(args['lease_expires']) is int and at+1<=args['lease_expires']<=root['expires']
                and type(args['capacity']) is int and 1<=args['capacity']<=limits['max_children'],'invalid_global_controller_limits')
        s['controller']={**args,'heartbeat_at':at,'joined_at':at}
    elif kind=='heartbeat':
        _keys(args,('native_task_id','controller_epoch'));require(actor=='native','invalid_global_actor')
        c=_controller(s,root,args,at,initialized=False);require(at>c['heartbeat_at'],'global_heartbeat_too_frequent');c['heartbeat_at']=at
    elif kind=='demand':
        require_active(s['controller'],root['controller_timing'],at,initialized=True)
        _keys(args,('route_id','generation','identity_sha256','selection','expires','child_bootstrap'))
        require(actor=='mac','invalid_global_actor');token(args['route_id']);token(args['generation'])
        require(args['generation']==root['activation_id'] and valid_hash(args['identity_sha256']),'global_route_binding_mismatch')
        require(args['route_id'] not in demands and len(demands)<limits['max_routes'],'global_route_budget_exhausted')
        require(sum(d['state']=='pending' for d in demands.values())<limits['max_pending'],'global_pending_queue_full')
        require(type(args['expires']) is int and at<args['expires']<=root['expires'],'invalid_global_demand_expiry')
        selection=validate_selection(args['selection']);bootstrap=args['child_bootstrap']
        cc=child_code(code,root['activation_id'],args['route_id'])
        child.verify_context(bootstrap,cc,bootstrap['bootstrap_document_id'],bootstrap['bootstrap_tab_id'],now=at)
        require(bootstrap['contract']==child.SELECTED_CONTRACT and bootstrap['stage']=='WAITING_FOR_WORKER'
                and not bootstrap['events'] and bootstrap['session_id']==args['route_id']
                and bootstrap['required_selection']==selection and bootstrap['folder_id']==root['folder_id']
                and bootstrap['expires']<=args['expires'],'global_child_bootstrap_binding_mismatch')
        ids={root['document_id']}
        for d in demands.values():ids.update((d['child_bootstrap']['bootstrap_document_id'],d['child_bootstrap']['control']['document_id']))
        pair=[bootstrap['bootstrap_document_id'],bootstrap['control']['document_id']]
        require(len(set(pair))==2 and not set(pair)&ids,'global_child_document_reuse')
        demands[args['route_id']]={**copy.deepcopy(args),'state':'pending','controller_epoch':None,'claim_id':None,
                                  'dispatch_id':None,'admission':None,'spawn_arguments_sha256':None,'ready':None}
    elif kind=='claim':
        _keys(args,('native_task_id','controller_epoch','route_id','claim_id'));require(actor=='native','invalid_global_actor')
        c=_controller(s,root,args,at);token(args['claim_id']);d=demands.get(args['route_id'])
        require(d is not None and d['state']=='pending' and at<d['expires'],'global_demand_not_claimable')
        require(sum(x['state'] in LIVE_STATES for x in demands.values())<c['capacity'],'global_child_slots_exhausted')
        d.update(state='claimed',controller_epoch=c['controller_epoch'],claim_id=args['claim_id'])
    elif kind=='begin':
        _keys(args,('native_task_id','controller_epoch','route_id','claim_id','dispatch_id'));require(actor=='native','invalid_global_actor')
        d=_owned(s,root,args,at,{'claimed'});require(valid_hash(args['dispatch_id']),'invalid_global_dispatch')
        expected=hash_bytes(canonical({'root':hash_bytes(canonical(root)),'route_id':args['route_id'],
                                      'controller_epoch':args['controller_epoch'],'claim_id':args['claim_id']}))
        require(args['dispatch_id']==expected,'global_dispatch_binding_mismatch');d.update(state='spawn_intent',dispatch_id=expected)
    elif kind=='admitted':
        _keys(args,('native_task_id','controller_epoch','route_id','claim_id','admission','spawn_arguments_sha256'))
        require(actor=='native','invalid_global_actor');d=_owned(s,root,args,at,{'spawn_intent'})
        receipt=args['admission'];task=receipt.get('native_task_id') if isinstance(receipt,dict) else None
        validate_admission(receipt,d['selection'],task)
        require(task.rsplit('/',1)[-1]=='global_'+d['route_id'],'global_native_task_name_mismatch')
        require(all(x['admission'] is None or x['admission']['native_task_id']!=task for x in demands.values()),'global_native_child_reuse')
        require(valid_hash(args['spawn_arguments_sha256']),'global_spawn_arguments_hash_required')
        d.update(state='admitted',admission=copy.deepcopy(receipt),spawn_arguments_sha256=args['spawn_arguments_sha256'])
    elif kind=='unknown':
        _keys(args,('native_task_id','controller_epoch','route_id','claim_id'));require(actor=='native','invalid_global_actor')
        d=_owned(s,root,args,at,{'spawn_intent'});d['state']='unknown'
    elif kind=='ready':
        require_active(s['controller'],root['controller_timing'],at,initialized=True)
        _keys(args,('route_id','pin','child_bootstrap'));require(actor=='mac','invalid_global_actor')
        d=demands.get(args['route_id']);require(d is not None and d['state']=='admitted','global_demand_not_admitted')
        pin=Object.parse(canonical(args['pin']));bootstrap=args['child_bootstrap'];cc=child_code(code,root['activation_id'],d['route_id'])
        child.verify_context(bootstrap,cc,bootstrap['bootstrap_document_id'],bootstrap['bootstrap_tab_id'],
                             expected_root=child.root_context(d['child_bootstrap']),now=at)
        child.verify_worker_polling(bootstrap,cc,now=at)
        require(bootstrap['worker']['admission']==d['admission'] and pin.body['kind']=='deployment'
                and pin.body['identity']['session_id']==d['route_id']
                and pin.body['payload']['inference']=={'selection':d['selection'],'admission':d['admission']}
                and pin.body['payload']['scope']=='responses_tools' and pin.body['payload']['expires']<=d['expires']
                and bootstrap['bundle_hashes']['deployment_hash']==pin.oid,'global_ready_pin_mismatch')
        d.update(state='ready',ready={'deployment':pin.oid,'runtime_hash':bootstrap['worker_ack']['runtime_hash'],
                                     'bootstrap_epoch':bootstrap['epoch'],'at':at})
    elif kind=='close':
        _keys(args,('confirm',));require(actor=='mac' and args['confirm'] is True,'explicit_global_close_required');s['closed']=True
    else:raise ProtocolError('unsupported_global_transition')
    return s


def verify(state,code,*,now=None,require_fresh=True,expected_root=None):
    require(isinstance(state,dict) and set(state)==ROOT_KEYS|{'root_mac','epoch','events','logical'},'invalid_global_state')
    root=root_of(state);validate_root(root)
    require(root['join_code_sha256']==child.join_code_hash(code),'global_join_code_mismatch')
    child.verify_proof(code,'global-root/2',root,state['root_mac'])
    if expected_root is not None:require(root==expected_root,'global_queue_root_mismatch')
    require(type(state['epoch']) is int and isinstance(state['events'],list)
            and state['epoch']==len(state['events'])<=MAX_EVENTS,'global_event_budget_or_epoch_invalid')
    logical={'controller':None,'demands':{},'closed':False};prev=root_hash(state);ops=set();last=root['created']
    for n,event in enumerate(state['events'],1):
        require(isinstance(event,dict) and set(event)=={'n','operation_id','kind','actor','at','arguments','previous','mac'},'invalid_global_event')
        token(event['operation_id']);require(event['operation_id'] not in ops,'global_operation_id_reuse');ops.add(event['operation_id'])
        require(event['n']==n and event['previous']==prev and type(event['at']) is int and event['at']>=last,'global_event_chain_mismatch')
        value={k:v for k,v in event.items() if k!='mac'}
        child.verify_proof(code,'global-event/2',{'root':root_hash(state),'event':value},event['mac'])
        logical=_transition(logical,root,event['kind'],event['actor'],event['arguments'],event['at'],code)
        last=event['at'];prev=hash_bytes(canonical(event))
    require(logical==state['logical'],'global_projection_mismatch')
    require(len(canonical(state))<=MAX_BYTES,'global_queue_too_large')
    if require_fresh:
        now=int(time.time()) if now is None else now
        require(type(now) is int and root['created']<=now<root['expires'] and last<=now,'global_queue_expired_or_future')
    return copy.deepcopy(state)


def transition(state,code,kind,actor,args,*,operation_id=None,now=None):
    now=int(time.time()) if now is None else now;verify(state,code,now=now)
    require(state['epoch']<MAX_EVENTS,'global_event_budget_exhausted')
    operation_id=operation_id or secrets.token_hex(16);token(operation_id)
    require(all(e['operation_id']!=operation_id for e in state['events']),'global_operation_already_issued_no_replay')
    nxt=copy.deepcopy(state);logical=_transition(nxt['logical'],root_of(nxt),kind,actor,args,now,code)
    event={'n':nxt['epoch']+1,'operation_id':operation_id,'kind':kind,'actor':actor,'at':now,'arguments':copy.deepcopy(args),
           'previous':hash_bytes(canonical(nxt['events'][-1])) if nxt['events'] else root_hash(nxt)}
    event['mac']=child.proof(code,'global-event/2',{'root':root_hash(nxt),'event':event})
    nxt['events'].append(event);nxt['epoch']+=1;nxt['logical']=logical
    verify(nxt,code,now=now);return nxt


def block(state):return BEGIN+canonical(state).decode()+END


@dataclass(frozen=True)
class Snapshot:
    document_id:str
    tab_id:str
    revision_id:str
    text:str
    @property
    def state(self):
        require(self.text.startswith(BEGIN) and self.text.endswith(END) and self.text.count(BEGIN)==1
                and self.text.count(END)==1,'invalid_global_block')
        from .global_gateway import strict_json
        value=strict_json(self.text[len(BEGIN):-len(END)])
        require(block(value)==self.text,'noncanonical_global_block');return value


def _utf16_length(text):
    """Google Docs offsets count UTF-16 units, not Python Unicode characters."""
    require(isinstance(text,str),'invalid_global_document_text')
    try:return len(text.encode('utf-16-le'))//2
    except UnicodeError:raise ProtocolError('invalid_global_document_text') from None


def _indexed_document_text(document,tab_id):
    """Accept only a complete, dedicated, indexed plain-text tab.

    This is intentionally Global-only. Child bootstrap/control and legacy cleanup
    keep their existing parser and write protocol. Never infer offsets from a
    lossy paragraph extraction or normalize provider text to fit the queue.
    """
    require(document.get('suggestionsViewMode')=='SUGGESTIONS_INLINE','inline_suggestions_view_required')
    def inspect(value):
        if isinstance(value,dict):
            for key,item in value.items():
                require(isinstance(key,str),'invalid_global_document_topology')
                if key.startswith('suggested') or key=='suggestions':
                    require(item is None or type(item) in (dict,list) and not item,'suggested_control_edit')
                if key in {'headers','footers','footnotes','inlineObjects','positionedObjects','lists'}:
                    require(item is None or type(item) is dict and not item,'dedicated_document_required')
                if key=='positionedObjectIds':
                    require(type(item) is list and not item,'dedicated_document_required')
                if key in {'defaultHeaderId','defaultFooterId','firstPageHeaderId','firstPageFooterId',
                           'evenPageHeaderId','evenPageFooterId'}:
                    require(item is None or item=='','dedicated_document_required')
                inspect(item)
        elif isinstance(value,list):
            for item in value:inspect(item)
    inspect(document)
    # The verified connector emits body:null beside flattened tabs. A content-
    # bearing legacy body is never accepted as a second source of truth.
    require(document.get('body') is None,'complete_document_body_required')
    tab_content_keys={'body','headers','footers','footnotes','documentStyle',
                      'suggestedDocumentStyleChanges','namedStyles','suggestedNamedStylesChanges',
                      'lists','namedRanges','inlineObjects','positionedObjects','commentAnchors'}
    property_keys={'tabId','title','parentTabId','index','nestingLevel','iconEmoji'}
    require(set(document)<=(tab_content_keys-{'commentAnchors'})|
            {'documentId','title','revisionId','suggestionsViewMode','commentsViewMode',
             'tabs','comments','suggestions','document_url','url'},'invalid_global_document_topology')
    tabs=document.get('tabs')
    require(type(tabs) is list and len(tabs)==1,'dedicated_single_tab_required')
    tab=tabs[0];require(isinstance(tab,dict),'control_tab_mismatch')
    if 'childTabs' in tab:
        require(type(tab['childTabs']) is list and not tab['childTabs'],'control_tab_mismatch')
    if 'tabProperties' in tab or 'documentTab' in tab:
        require(set(tab)<= {'tabProperties','documentTab','childTabs'}
                and isinstance(tab.get('tabProperties'),dict)
                and isinstance(tab.get('documentTab'),dict),'control_tab_mismatch')
        properties=tab['tabProperties'];dt=tab['documentTab']
        require(set(properties)<=property_keys and set(dt)<=tab_content_keys,'control_tab_mismatch')
    else:
        # The connected wrapper's verified normalized tab shape.
        properties=tab;dt=tab
        require(set(tab)<=tab_content_keys|property_keys|{'childTabs','documentId','document_url'},
                'control_tab_mismatch')
        if 'documentId' in tab:
            require(tab['documentId']==document['documentId'],'global_document_mismatch')
    require(properties.get('tabId')==tab_id and properties.get('parentTabId') in (None,''),
            'control_tab_mismatch')
    for key in ('index','nestingLevel'):
        if key in properties:
            if key=='nestingLevel' and properties is tab and properties[key] is None:continue
            require(type(properties[key]) is int and properties[key]==0,'control_tab_mismatch')
    body=dt.get('body')
    require(isinstance(body,dict) and set(body)=={'content'} and type(body['content']) is list
            and body['content'],'complete_document_body_required')
    pieces=[];cursor=1
    for number,element in enumerate(body['content']):
        require(isinstance(element,dict),'invalid_document_element')
        if 'sectionBreak' in element:
            # The initial zero startIndex is legitimately omitted by the API.
            require(number==0 and set(element)<={'startIndex','endIndex','sectionBreak'}
                    and type(element.get('startIndex',0)) is int and element.get('startIndex',0)==0
                    and type(element.get('endIndex')) is int and element['endIndex']==1
                    and isinstance(element['sectionBreak'],dict),'unexpected_section_break')
            section=element['sectionBreak']
            require(set(section)<={'sectionStyle','suggestedInsertionIds','suggestedDeletionIds',
                                   'suggestedSectionStyleChanges'},'unexpected_section_break')
            if 'sectionStyle' in section:
                require(isinstance(section['sectionStyle'],dict),'unexpected_section_break')
            continue
        require(set(element)=={'startIndex','endIndex','paragraph'}
                and isinstance(element['paragraph'],dict),'plain_control_document_required')
        require(type(element['startIndex']) is int and element['startIndex']==cursor
                and type(element['endIndex']) is int and element['endIndex']>cursor,
                'global_document_index_mismatch')
        paragraph=element['paragraph']
        require(set(paragraph)<={'elements','paragraphStyle','suggestedParagraphStyleChanges',
                                'suggestedBulletChanges','positionedObjectIds','suggestedPositionedObjectIds'}
                and type(paragraph.get('elements')) is list and paragraph['elements'],
                'plain_control_document_required')
        if 'paragraphStyle' in paragraph:
            require(isinstance(paragraph['paragraphStyle'],dict),'plain_control_document_required')
        runs=[]
        for run in paragraph['elements']:
            require(isinstance(run,dict) and set(run)=={'startIndex','endIndex','textRun'}
                    and isinstance(run['textRun'],dict),'plain_control_document_required')
            text_run=run['textRun']
            require(set(text_run)<={'content','textStyle','suggestedInsertionIds','suggestedDeletionIds',
                                   'suggestedTextStyleChanges'}
                    and isinstance(text_run.get('content'),str) and text_run['content'],
                    'plain_control_document_required')
            if 'textStyle' in text_run:
                require(isinstance(text_run['textStyle'],dict),'plain_control_document_required')
            text=text_run['content'];length=_utf16_length(text)
            require(type(run['startIndex']) is int and run['startIndex']==cursor
                    and type(run['endIndex']) is int and run['endIndex']==cursor+length,
                    'global_document_index_mismatch')
            cursor+=length;runs.append(text)
        text=''.join(runs)
        require(text.endswith('\n') and '\n' not in text[:-1],'global_paragraph_newline_required')
        require(element['endIndex']==cursor,'global_document_index_mismatch')
        pieces.append(text)
    text=''.join(pieces)
    require(text and text.endswith('\n') and cursor==1+_utf16_length(text),'global_document_index_mismatch')
    require(len(text.encode('utf-8'))<=MAX_BYTES+len(BEGIN)+len(END),'global_queue_too_large')
    return text


def snapshot(document,document_id,tab_id):
    require(isinstance(document,dict) and document.get('documentId')==document_id,'global_document_mismatch')
    safe_id(document_id);safe_id(tab_id)
    revision=document.get('revisionId');require(isinstance(revision,str) and 1<=len(revision)<=1024,'global_revision_required')
    return Snapshot(document_id,tab_id,revision,_indexed_document_text(document,tab_id))


def _indexed_requests(source,new):
    """One atomic pinned-tab delete/insert, preserving its mandatory final LF."""
    replacement=block(new)
    require(source.text.endswith('\n') and replacement.endswith('\n'),'invalid_global_block')
    return [{'deleteContentRange':{'range':{'startIndex':1,
                'endIndex':1+_utf16_length(source.text[:-1]),'tabId':source.tab_id}}},
            {'insertText':{'location':{'index':1,'tabId':source.tab_id},'text':replacement[:-1]}}]


def plan(source,new,code):
    old=source.state;verify(old,code,require_fresh=False);verify(new,code,expected_root=root_of(old),require_fresh=False)
    require(old['document_id']==source.document_id and old['tab_id']==source.tab_id and new['epoch']==old['epoch']+1
            and new['events'][:-1]==old['events'],'global_plan_not_one_transition')
    return {'contract':SINGLE_PLAN_CONTRACT,'source_block':source.text,'tab_id':source.tab_id,
            'expected_state':new,'operation_id':new['events'][-1]['operation_id'],
            'execute_before':event_deadline(old,new['events'][-1]['kind'],new['events'][-1]['at']),
            'tool_arguments':{'document_id':source.document_id,'requests':_indexed_requests(source,new),
                              'write_control':{'requiredRevisionId':source.revision_id}}}


def plan_claim_begin(source,new,code):
    """Only the native, same-route claim/begin pair may share one exact CAS.

    Both ordinary signed transitions must verify in order. This is a distinct
    contract, not a switch that relaxes the single-event plan's invariant.
    """
    old=source.state;verify(old,code,require_fresh=False)
    verify(new,code,expected_root=root_of(old),require_fresh=False)
    require(old['document_id']==source.document_id and old['tab_id']==source.tab_id
            and new['epoch']==old['epoch']+2 and new['events'][:-2]==old['events'],
            'global_group_not_claim_begin')
    claim,begin=new['events'][-2:]
    require(claim['kind']=='claim' and begin['kind']=='begin'
            and claim['actor']==begin['actor']=='native' and claim['at']==begin['at']
            and claim['arguments']=={k:v for k,v in begin['arguments'].items() if k!='dispatch_id'},
            'global_group_not_claim_begin')
    # Reconstruct the authenticated intermediate state rather than trusting a
    # caller-supplied projection when deriving either event's execution fence.
    middle=transition(old,code,'claim','native',claim['arguments'],
                      operation_id=claim['operation_id'],now=claim['at'])
    require(middle['events']==new['events'][:-1],'global_group_not_claim_begin')
    ids=[claim['operation_id'],begin['operation_id']]
    group_id=hash_bytes(canonical({'contract':GROUP_PLAN_CONTRACT,'root':root_hash(old),
                                  'group_kind':'claim-begin','operation_ids':ids}))
    return {'contract':GROUP_PLAN_CONTRACT,'group_kind':'claim-begin','group_id':group_id,
            'operation_ids':ids,'source_block':source.text,'tab_id':source.tab_id,'expected_state':new,
            'execute_before':min(event_deadline(old,'claim',claim['at']),
                                 event_deadline(middle,'begin',begin['at']),
                                 middle['logical']['demands'][claim['arguments']['route_id']]['expires']),
            'tool_arguments':{'document_id':source.document_id,'requests':_indexed_requests(source,new),
                              'write_control':{'requiredRevisionId':source.revision_id}}}


def plan_heartbeat_admitted(source,new,code):
    """Exact heartbeat/admitted pair, after the caller durably records native evidence.

    The original heartbeat must still be live. Its pre-refresh deadline is part
    of the combined fence; the second event cannot revive an expired controller.
    This explicit group is not permission to batch arbitrary transitions.
    """
    old=source.state;verify(old,code,require_fresh=False)
    verify(new,code,expected_root=root_of(old),require_fresh=False)
    require(old['document_id']==source.document_id and old['tab_id']==source.tab_id
            and new['epoch']==old['epoch']+2 and new['events'][:-2]==old['events'],
            'global_group_not_heartbeat_admitted')
    heartbeat,admitted=new['events'][-2:]
    require(heartbeat['kind']=='heartbeat' and admitted['kind']=='admitted'
            and heartbeat['actor']==admitted['actor']=='native' and heartbeat['at']==admitted['at']
            and heartbeat['arguments']=={k:admitted['arguments'][k]
                for k in ('native_task_id','controller_epoch')},
            'global_group_not_heartbeat_admitted')
    middle=transition(old,code,'heartbeat','native',heartbeat['arguments'],
                      operation_id=heartbeat['operation_id'],now=heartbeat['at'])
    require(middle['events']==new['events'][:-1],'global_group_not_heartbeat_admitted')
    ids=[heartbeat['operation_id'],admitted['operation_id']]
    group_id=hash_bytes(canonical({'contract':GROUP_PLAN_CONTRACT,'root':root_hash(old),
                                  'group_kind':'heartbeat-admitted','operation_ids':ids}))
    demand=middle['logical']['demands'][admitted['arguments']['route_id']]
    return {'contract':GROUP_PLAN_CONTRACT,'group_kind':'heartbeat-admitted','group_id':group_id,
            'operation_ids':ids,'source_block':source.text,'tab_id':source.tab_id,'expected_state':new,
            'execute_before':min(event_deadline(old,'heartbeat',heartbeat['at']),
                                 event_deadline(middle,'admitted',admitted['at']),
                                 demand['expires'],demand['child_bootstrap']['expires']),
            'tool_arguments':{'document_id':source.document_id,'requests':_indexed_requests(source,new),
                              'write_control':{'requiredRevisionId':source.revision_id}}}


def plan_join_heartbeat(source,new,code):
    """Only JOIN followed by its actual next-second first heartbeat is grouped."""
    old=source.state;verify(old,code,require_fresh=False)
    verify(new,code,expected_root=root_of(old),require_fresh=False)
    require(old['document_id']==source.document_id and old['tab_id']==source.tab_id
            and new['epoch']==old['epoch']+2 and new['events'][:-2]==old['events'],
            'global_group_not_join_heartbeat')
    joined,heartbeat=new['events'][-2:]
    require(joined['kind']=='join' and heartbeat['kind']=='heartbeat'
            and joined['actor']==heartbeat['actor']=='native' and heartbeat['at']==joined['at']+1
            and heartbeat['arguments']=={k:joined['arguments'][k]
                for k in ('native_task_id','controller_epoch')},
            'global_group_not_join_heartbeat')
    middle=transition(old,code,'join','native',joined['arguments'],
                      operation_id=joined['operation_id'],now=joined['at'])
    require(middle['events']==new['events'][:-1],'global_group_not_join_heartbeat')
    ids=[joined['operation_id'],heartbeat['operation_id']]
    group_id=hash_bytes(canonical({'contract':GROUP_PLAN_CONTRACT,'root':root_hash(old),
                                  'group_kind':'join-heartbeat','operation_ids':ids}))
    return {'contract':GROUP_PLAN_CONTRACT,'group_kind':'join-heartbeat','group_id':group_id,
            'operation_ids':ids,'source_block':source.text,'tab_id':source.tab_id,'expected_state':new,
            'execute_before':min(event_deadline(old,'join',joined['at']),
                                 event_deadline(middle,'heartbeat',heartbeat['at'])),
            'tool_arguments':{'document_id':source.document_id,'requests':_indexed_requests(source,new),
                              'write_control':{'requiredRevisionId':source.revision_id}}}


def plan_heartbeat_claim_begin(source,new,code):
    """Explicit due-heartbeat/claim/begin group; no arbitrary event batching."""
    old=source.state;verify(old,code,require_fresh=False)
    verify(new,code,expected_root=root_of(old),require_fresh=False)
    require(old['document_id']==source.document_id and old['tab_id']==source.tab_id
            and new['epoch']==old['epoch']+3 and new['events'][:-3]==old['events'],
            'global_group_not_heartbeat_claim_begin')
    heartbeat,claim,begin=new['events'][-3:]
    require(heartbeat['kind']=='heartbeat' and claim['kind']=='claim' and begin['kind']=='begin'
            and heartbeat['actor']==claim['actor']==begin['actor']=='native'
            and heartbeat['at']==claim['at']==begin['at']
            and heartbeat['arguments']=={k:claim['arguments'][k] for k in ('native_task_id','controller_epoch')}
            and claim['arguments']=={k:v for k,v in begin['arguments'].items() if k!='dispatch_id'},
            'global_group_not_heartbeat_claim_begin')
    ticked=transition(old,code,'heartbeat','native',heartbeat['arguments'],
                      operation_id=heartbeat['operation_id'],now=heartbeat['at'])
    claimed=transition(ticked,code,'claim','native',claim['arguments'],
                       operation_id=claim['operation_id'],now=claim['at'])
    require(claimed['events']==new['events'][:-1],'global_group_not_heartbeat_claim_begin')
    ids=[heartbeat['operation_id'],claim['operation_id'],begin['operation_id']]
    group_id=hash_bytes(canonical({'contract':GROUP_PLAN_CONTRACT,'root':root_hash(old),
                                  'group_kind':'heartbeat-claim-begin','operation_ids':ids}))
    return {'contract':GROUP_PLAN_CONTRACT,'group_kind':'heartbeat-claim-begin','group_id':group_id,
            'operation_ids':ids,'source_block':source.text,'tab_id':source.tab_id,'expected_state':new,
            'execute_before':min(event_deadline(old,'heartbeat',heartbeat['at']),
                                 event_deadline(ticked,'claim',claim['at']),
                                 event_deadline(claimed,'begin',begin['at']),
                                 claimed['logical']['demands'][claim['arguments']['route_id']]['expires']),
            'tool_arguments':{'document_id':source.document_id,'requests':_indexed_requests(source,new),
                              'write_control':{'requiredRevisionId':source.revision_id}}}


def validate_plan(packet,code):
    """Validate the complete exact schema and reconstruct its sole legal CAS."""
    require(isinstance(packet,dict),'invalid_global_cas_plan')
    common={'contract','source_block','tab_id','expected_state','tool_arguments','execute_before'}
    if packet.get('contract')==SINGLE_PLAN_CONTRACT:
        require(set(packet)==common|{'operation_id'},'invalid_global_cas_plan');planner=plan
    elif packet.get('contract')==GROUP_PLAN_CONTRACT:
        require(set(packet)==common|{'group_kind','group_id','operation_ids'},'invalid_global_cas_group_plan')
        planners={'claim-begin':plan_claim_begin,'heartbeat-admitted':plan_heartbeat_admitted,
                  'join-heartbeat':plan_join_heartbeat,'heartbeat-claim-begin':plan_heartbeat_claim_begin}
        require(isinstance(packet.get('group_kind'),str) and packet['group_kind'] in planners,
                'invalid_global_cas_group_plan')
        planner=planners[packet['group_kind']]
    else:raise ProtocolError('invalid_global_cas_plan')
    args=packet['tool_arguments']
    require(isinstance(args,dict) and set(args)=={'document_id','requests','write_control'}
            and isinstance(args['write_control'],dict) and set(args['write_control'])=={'requiredRevisionId'}
            and isinstance(args['write_control']['requiredRevisionId'],str)
            and 1<=len(args['write_control']['requiredRevisionId'])<=1024
            and isinstance(packet['source_block'],str),'invalid_global_cas_plan')
    safe_id(args['document_id']);safe_id(packet['tab_id'])
    source=Snapshot(args['document_id'],packet['tab_id'],args['write_control']['requiredRevisionId'],packet['source_block'])
    # JSON bytes distinguish true/1 and 1.0/1, unlike Python dict equality.
    require(canonical(packet)==canonical(planner(source,packet['expected_state'],code)),
            'global_cas_plan_changed')
    return source


def plan_identity(packet):
    if packet['contract']==GROUP_PLAN_CONTRACT:
        return {'group_id':packet['group_id'],'operation_ids':copy.deepcopy(packet['operation_ids'])}
    return {'operation_id':packet['operation_id']}


def verify_update(packet,response,readback,code,*,now=None):
    source=validate_plan(packet,code)
    if response is not None:
        require(isinstance(response,dict) and response.get('documentId')==source.document_id,'global_response_document_mismatch')
        replies=response.get('replies');wc=response.get('writeControl')
        require(type(replies) is list and len(replies)==2
                and all(type(reply) is dict and not reply for reply in replies),
                'global_exact_indexed_replies_required')
        require(isinstance(wc,dict) and set(wc)=={'requiredRevisionId'}
                and isinstance(wc['requiredRevisionId'],str) and 1<=len(wc['requiredRevisionId'])<=1024
                and wc['requiredRevisionId']!=source.revision_id,'global_response_revision_unverified')
    fresh=snapshot(readback,source.document_id,source.tab_id)
    expected=packet['expected_state'];verify(fresh.state,code,expected_root=root_of(expected),require_fresh=False)
    require(fresh.revision_id!=source.revision_id and fresh.state['epoch']>=expected['epoch']
            and fresh.state['events'][:expected['epoch']]==expected['events'],'global_operation_not_observed_no_replay')
    now=time.time() if now is None else now
    require(packet['expected_state']['events'][-1]['at']<=now<packet['execute_before'],
            'global_cas_acceptance_window_expired_no_replay')
    return fresh


def dispatch_id(state,route_id):
    d=state['logical']['demands'][route_id]
    return hash_bytes(canonical({'root':root_hash(state),'route_id':route_id,
                                'controller_epoch':d['controller_epoch'],'claim_id':d['claim_id']}))


def native_arguments(state,code,route_id,package_root,child_state_dir,*,handoff):
    """Exact compact arguments, bound to an already reserved immutable handoff."""
    from . import global_handoff
    value=global_handoff.read(handoff)
    expected=global_handoff.build(state,code,route_id,package_root,child_state_dir,handoff['path'],created=value['created'])
    require(value==expected,'global_handoff_binding_mismatch')
    return global_handoff.arguments(value,handoff)
