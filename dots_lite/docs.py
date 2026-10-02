"""Strict sole-tab Docs snapshots and exact revision-fenced indexed updates.

Indexed topology/UTF-16 primitives extracted from frozen 751e14
remote_transport/global_control.py (repository LICENSE applies). Retains SDK
and observed flattened connector topology/null handling, without legacy imports.
"""
import copy
from .protocol import ProtocolError, require, canonical, strict_json, safe_id, INBOX_MAX_BYTES, sha256

def _utf16_length(text):
    """Google Docs offsets count UTF-16 units, not Python Unicode characters."""
    require(isinstance(text,str),'invalid_global_document_text')
    try:return len(text.encode('utf-16-le'))//2
    except UnicodeError:raise ProtocolError('invalid_global_document_text') from None


def _indexed_document_text(document,tab_id,max_bytes):
    """Accept only a complete, dedicated, indexed plain-text tab.

    V3 uses dedicated single-writer control Docs. Never infer offsets from a
    lossy paragraph extraction or normalize provider text to fit the snapshot.
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
    require(len(text.encode('utf-8'))<=max_bytes,'global_queue_too_large')
    return text



def unwrap(resource):
    """Only structured provider data, never text summaries or planned receipts."""
    for _ in range(4):
        require(type(resource) is dict,'structured_provider_response_required')
        require(resource.get('isError') is not True,'provider_error')
        if 'documentId' in resource:return resource
        if type(resource.get('structuredContent')) is dict:
            resource=resource['structuredContent'];continue
        if type(resource.get('result')) is dict:
            resource=resource['result'];continue
        break
    require(type(resource) is dict and 'documentId' in resource,'structured_provider_response_required')
    return resource

def snapshot(resource,document_id,tab_id=None,max_bytes=INBOX_MAX_BYTES):
    document=unwrap(resource)
    require(document.get('documentId')==document_id,'document_mismatch');safe_id(document_id)
    if tab_id is None:
        tabs=document.get('tabs')
        require(type(tabs) is list and len(tabs)==1 and type(tabs[0]) is dict,'dedicated_single_tab_required')
        tab_id=tabs[0].get('tabProperties',tabs[0]).get('tabId')
    safe_id(tab_id)
    revision=document.get('revisionId')
    require(isinstance(revision,str) and 1<=len(revision)<=1024,'revision_required')
    text=_indexed_document_text(document,tab_id,max_bytes)
    return {'document_id':document_id,'tab_id':tab_id,'revision_id':revision,'text':text,'max_bytes':max_bytes}

def _validate_snapshot(value):
    require(type(value) is dict and set(value)=={'document_id','tab_id','revision_id','text','max_bytes'},'invalid_snapshot')
    safe_id(value['document_id']);safe_id(value['tab_id'])
    require(isinstance(value['revision_id'],str) and 1<=len(value['revision_id'])<=1024,'revision_required')
    require(type(value['max_bytes']) is int and 1<=value['max_bytes']<=INBOX_MAX_BYTES,'invalid_snapshot_cap')
    require(isinstance(value['text'],str) and value['text'].endswith('\n') and len(value['text'].encode())<=value['max_bytes']+1,'invalid_snapshot_text')

def plan_write(known_snapshot,new_record,op_id):
    _validate_snapshot(known_snapshot);safe_id(op_id)
    require(type(new_record) is dict and new_record.get('operation_id')==op_id,'operation_id_mismatch')
    text=canonical(new_record).decode()+'\n'
    require(len(text.encode())<=known_snapshot['max_bytes']+1,'control_document_too_large')
    source=known_snapshot
    # Empty Docs contain only their mandatory newline, so initializing one is
    # a single insert; nonempty replacement is exactly delete + insert.
    requests=[]
    if source['text']!='\n':
        requests.append({'deleteContentRange':{'range':{'startIndex':1,'endIndex':1+_utf16_length(source['text'][:-1]),'tabId':source['tab_id']}}})
    requests.append({'insertText':{'location':{'index':1,'tabId':source['tab_id']},'text':text[:-1]}})
    packet={'operation_id':op_id,'source':copy.deepcopy(source),'record':copy.deepcopy(new_record),'text':text,
            'document_id':source['document_id'],'body':{'writeControl':{'requiredRevisionId':source['revision_id']},'requests':requests}}
    packet['plan_sha256']=sha256(canonical(packet))
    return packet

def validate_plan(plan):
    require(type(plan) is dict and set(plan)=={'operation_id','source','record','text','document_id','body','plan_sha256'},'invalid_write_plan')
    copy_plan=copy.deepcopy(plan);checksum=copy_plan.pop('plan_sha256')
    require(sha256(canonical(copy_plan))==checksum,'write_plan_changed')
    require(plan_write(plan['source'],plan['record'],plan['operation_id'])==plan,'write_plan_changed')
    return plan

def accept_write(plan,actual_response):
    validate_plan(plan)
    try:
        response=unwrap(actual_response)
        require(response.get('documentId')==plan['document_id'],'response_document_mismatch')
        replies=response.get('replies');wc=response.get('writeControl')
        require(type(replies) is list and len(replies)==len(plan['body']['requests']) and all(type(r) is dict and not r for r in replies),'exact_indexed_replies_required')
        require(type(wc) is dict and set(wc) in ({'requiredRevisionId'},{'requiredRevisionId','targetRevisionId'}) and wc.get('targetRevisionId') is None,'response_revision_unverified')
        revision=wc.get('requiredRevisionId')
        require(isinstance(revision,str) and 1<=len(revision)<=1024 and revision!=plan['source']['revision_id'],'response_revision_unverified')
        require(response.get('revisionId') in (None,revision),'response_revision_conflict')
    except ProtocolError as error:
        return {'status':'unknown','reason':error.code}
    known={**plan['source'],'revision_id':revision,'text':plan['text']}
    return {'status':'accepted','revision_id':revision,'snapshot':known}

def reconcile_write(plan,fresh_resource):
    validate_plan(plan)
    try:
        fresh=snapshot(fresh_resource,plan['document_id'],plan['source']['tab_id'],plan['source']['max_bytes'])
    except ProtocolError as error:
        return {'status':'conflicting','reason':error.code}
    if fresh['text']==plan['text'] and fresh['revision_id']!=plan['source']['revision_id']:
        return {'status':'applied','revision_id':fresh['revision_id'],'snapshot':fresh}
    if fresh['text']==plan['source']['text']:
        return {'status':'unknown','reason':'operation_not_observed_no_retry'}
    return {'status':'conflicting','reason':'different_control_record'}
