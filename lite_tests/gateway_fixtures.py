"""Synthetic provider with real SDK/flattened connector topology, no Google I/O."""
import copy
import time
from dots_lite.protocol import PROTOCOL, canonical, sha256, DEFAULT_LIMITS, sign_record

KEY = '11' * 32

def grant():
    return {'protocol': PROTOCOL, 'activation_id': 'activation-test', 'folder_id': 'folder-test', 'inbox_id': 'inbox-test',
            'created_at': int(time.time()) - 5, 'expires_at': int(time.time()) + 3600,
            'allowed_pairs': [{'model': 'gpt-6.1-sol', 'reasoning_effort': 'xhigh'}, {'model':'gpt-6-astra','reasoning_effort':'high'}],
            'limits': copy.deepcopy(DEFAULT_LIMITS), 'package_sha256': 'a' * 64}


def request(model='gpt-6.1-sol', effort='xhigh'):
    return {'model': model, 'reasoning': {'effort': effort}, 'stream': True,
            'input': [{'role':'user','content':[{'type':'input_text','text':'Hello 🌲'}]}],
            'tools': [{'type':'namespace','name':'functions','tools':[
                {'type':'function','name':'exec_command','parameters':{'type':'object','properties':{'cmd':{'type':'string'}},'required':['cmd'],'additionalProperties':False}},
                {'type':'custom','name':'apply_patch','format':{'type':'text'}}]}]}


def response(rid, kind='message'):
    if kind == 'message':
        item = {'type':'message','id':'msg_'+rid,'role':'assistant','content':[{'type':'output_text','text':'Done 🌲'}]}
    else:
        item = {'type':kind,'id':'item_'+rid,'call_id':'call_'+rid,'name':'exec_command' if kind == 'function_call' else 'apply_patch','namespace':'functions'}
        item['arguments' if kind == 'function_call' else 'input'] = '{"cmd":"pwd"}' if kind == 'function_call' else '*** Begin Patch\n*** End Patch'
    return {'id':'resp_'+rid,'object':'response','status':'completed','model':'gpt-6.1-sol','output':[item]}


class FakeGoogle:
    def __init__(self, flattened=False):
        self.texts = {'inbox-test':'\n'}; self.revs = {'inbox-test':1}; self.blobs = {}; self.calls = []
        self.flattened = flattened; self.lose_write_reply = False; self.hide_write = False
    def get_document(self, document_id, **kwargs):
        self.calls.append(('get_document', document_id))
        text = self.texts[document_id]; end = 1 + len(text.encode('utf-16-le')) // 2
        body = {'content':[{'endIndex':1,'sectionBreak':{'sectionStyle':{}}}, {'startIndex':1,'endIndex':end,
            'paragraph':{'elements':[{'startIndex':1,'endIndex':end,'textRun':{'content':text,'textStyle':{}}}], 'paragraphStyle':{}}}]}
        value = {'documentId':document_id,'revisionId':'rev-'+str(self.revs[document_id]),'suggestionsViewMode':'SUGGESTIONS_INLINE','body':None}
        if self.flattened:
            value['tabs'] = [{'tabId':'tab-'+document_id,'index':0,'nestingLevel':None,'body':body,'headers':None,'inlineObjects':{}}]
            return {'structuredContent':{'result':value}}
        value['tabs'] = [{'tabProperties':{'tabId':'tab-'+document_id,'index':0,'nestingLevel':0},'documentTab':{'body':body}}]
        return value
    def batch_update_document(self, document_id, requests, write_control, **kwargs):
        self.calls.append(('batch_update', document_id))
        assert write_control == {'requiredRevisionId':'rev-'+str(self.revs[document_id])}
        old = self.texts[document_id]
        assert len(requests) == (1 if old == '\n' else 2)
        if old != '\n':
            assert requests[0] == {'deleteContentRange':{'range':{'startIndex':1,'endIndex':len(old.encode('utf-16-le'))//2,'tabId':'tab-'+document_id}}}
        assert requests[-1]['insertText']['location'] == {'index':1,'tabId':'tab-'+document_id}
        if not self.hide_write:
            self.texts[document_id] = requests[-1]['insertText']['text']+'\n'; self.revs[document_id] += 1
        if self.lose_write_reply: raise TimeoutError('synthetic')
        return {'documentId':document_id,'replies':[{} for _ in requests],
                'writeControl':{'requiredRevisionId':'rev-'+str(self.revs[document_id]),'targetRevisionId':None}}
    def create_document_once(self, folder, name, **kwargs):
        self.calls.append(('create_document', name)); ident = 'outbox-'+str(len(self.texts))
        self.texts[ident]='\n'; self.revs[ident]=1
        return ident
    def create_bytes(self, folder, name, raw, file_id=None, **kwargs):
        self.calls.append(('upload', name)); ident = file_id or 'blob-'+str(len(self.blobs))
        self.blobs[ident]=(folder,raw); return ident
    def get_metadata(self, file_id, **kwargs):
        self.calls.append(('get_metadata',file_id)); folder,raw=self.blobs[file_id]
        return {'id':file_id,'parents':[folder],'mimeType':'application/json','trashed':False,'size':str(len(raw))}
    def get_bytes(self, file_id, limit, **kwargs):
        self.calls.append(('get_bytes',file_id)); return self.blobs[file_id][1]
    def publish_result(self, gateway, ticket, output):
        state = gateway.journal.read(); slot = state['routes'][ticket['route_id']]['slot']; desc = slot['request']
        child='/root/child-'+ticket['route_id']
        admission={'adapter':'collaboration.spawn_agent','native_task_id':child,'submitted_model':slot['model'],
                   'submitted_reasoning_effort':slot['reasoning_effort'],'fork_turns':'none','arguments_sha256':'b'*64,
                   'tool_result_sha256':'c'*64,'verification':'parent_recorded_platform_admission','underlying_model_verified':False}
        envelope={'protocol':PROTOCOL,'kind':'result','activation_id':gateway.grant['activation_id'],'route_id':slot['route_id'],
                  'request_id':desc['request_id'],'request_sha256':desc['request_sha256'],'seq':desc['seq'],
                  'begin_operation_id':'begin-'+desc['request_id'],'child_task_id':child,'model':slot['model'],
                  'reasoning_effort':slot['reasoning_effort'],'result_id':'result-'+desc['request_id'],'output':output}
        raw=canonical(envelope); fid=self.create_bytes(gateway.grant['folder_id'],'result',raw)
        record={'protocol':PROTOCOL,'kind':'outbox','activation_id':gateway.grant['activation_id'],
                **{k:slot[k] for k in ('route_id','identity_sha256','model','reasoning_effort')},'phase':'RESULT',
                'operation_id':'result-'+desc['request_id'],'spawn_operation_id':'spawn-'+slot['route_id'],
                'parent_task_id':'/root','child_task_id':child,'admission':admission,'consumed_seq':desc['seq'],
                'request':desc,'begin_operation_id':'begin-'+desc['request_id'],
                'result':{'result_id':envelope['result_id'],'result_sha256':sha256(raw),'byte_length':len(raw),
                          'file_id':fid,'folder_id':gateway.grant['folder_id']}}
        self.texts[slot['outbox_id']]=canonical(sign_record(record,KEY)).decode()+'\n'; self.revs[slot['outbox_id']]+=1
        return record
