"""Small authenticated Docs/Drive protocol, independent of legacy runtimes.

Canonical/validation primitives follow the frozen 751e14 implementation;
there is no event chain, model client, heartbeat authority, or implicit grant.
"""
import copy
import hashlib
import hmac
import json
import re
import time

PROTOCOL = 'dots-lite/3'
JOIN_MARKER = 'DOTS2CODEX_GLOBAL_JOIN_V3'
INBOX_MAX_BYTES = 16 * 1024
OUTBOX_MAX_BYTES = 8 * 1024
PAYLOAD_MAX_BYTES = 1024 * 1024
METADATA_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_LIMITS = {'max_routes': 3, 'max_children': 3, 'max_requests_per_route': 128,
                  'max_request_bytes': PAYLOAD_MAX_BYTES, 'max_result_bytes': PAYLOAD_MAX_BYTES}
GRANT_KEYS = {'protocol','activation_id','folder_id','inbox_id','created_at','expires_at','allowed_pairs','limits','package_sha256'}
ROUTE_KEYS = {'route_id','identity_sha256','model','reasoning_effort','outbox_id','request','stop'}
REQUEST_KEYS = {'seq','request_id','request_sha256','byte_length','file_id','folder_id','previous_result_ack','begin_before'}
RESULT_KEYS = {'result_id','result_sha256','byte_length','file_id','folder_id'}
OUTBOX_KEYS = {'protocol','kind','activation_id','route_id','identity_sha256','model','reasoning_effort','phase',
               'operation_id','spawn_operation_id','parent_task_id','child_task_id','admission','consumed_seq',
               'request','begin_operation_id','result','mac'}
PHASES = {'SPAWN_RESERVED','ADMITTED','BEGIN','RESULT'}

class ProtocolError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def require(condition, code):
    if not condition:
        raise ProtocolError(code)

def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError):
        raise ProtocolError('invalid_json') from None

def sha256(data):
    return hashlib.sha256(data).hexdigest()
hash_bytes = sha256

def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate_json_key')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError('nonfinite_json')))
    except (ValueError, UnicodeError, TypeError) as error:
        if isinstance(error, ProtocolError):
            raise
        raise ProtocolError('invalid_json') from None

def safe_id(value):
    require(isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_:/.-]{1,256}',value) is not None,'invalid_id')
    return value

def valid_hash(value):
    return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None

def exact(value, keys, code):
    require(type(value) is dict and set(value)==set(keys),code)

def validate_pair(pair):
    exact(pair, {'model','reasoning_effort'}, 'invalid_pair')
    safe_id(pair['model'])
    require(pair['reasoning_effort'] in {'low','medium','high','xhigh'}, 'unsupported_effort_explicit_reselection_required')
    return copy.deepcopy(pair)

def validate_grant(grant):
    exact(grant,GRANT_KEYS,'invalid_grant')
    require(grant['protocol']==PROTOCOL,'protocol_version_mismatch')
    for key in ('activation_id','folder_id','inbox_id'):
        safe_id(grant[key])
    require(type(grant['created_at']) is int and type(grant['expires_at']) is int and
            grant['created_at'] < grant['expires_at'], 'invalid_grant_deadline')
    require(valid_hash(grant['package_sha256']), 'invalid_package_hash')
    pairs = grant['allowed_pairs']
    require(type(pairs) is list and 1<=len(pairs)<=32,'invalid_allowed_pairs')
    for pair in pairs:
        validate_pair(pair)
    require(len({canonical(pair) for pair in pairs})==len(pairs),'duplicate_pair')
    limits=grant['limits'];exact(limits,DEFAULT_LIMITS,'invalid_limits')
    # Larger profiles require a separately tested protocol revision.
    require(all(type(limits[k]) is int and 1<=limits[k]<=v for k,v in DEFAULT_LIMITS.items()),'unsupported_limits')
    require(limits['max_children']<=limits['max_routes'],'invalid_limits')
    return copy.deepcopy(grant)

def grant_hash(grant):
    return sha256(canonical(validate_grant(grant)))

def _key(key):
    if isinstance(key,str):
        try: key=bytes.fromhex(key)
        except ValueError: raise ProtocolError('invalid_join_key') from None
    require(type(key) is bytes and len(key)>=32,'invalid_join_key')
    return key

def sign_record(record,key):
    value=copy.deepcopy(record);value.pop('mac',None)
    require(value.get('protocol')==PROTOCOL and value.get('kind') in {'inbox','outbox'},'invalid_record_domain')
    domain=(PROTOCOL+'/'+value['kind']+'\0').encode()
    value['mac']=hmac.new(_key(key),domain+canonical(value),hashlib.sha256).hexdigest()
    return value

def verify_record(record,key,kind,cap):
    require(type(record) is dict and record.get('protocol')==PROTOCOL,'protocol_version_mismatch')
    require(record.get('kind')==kind and len(canonical(record))<=cap,'invalid_or_oversize_record')
    require(valid_hash(record.get('mac')),'invalid_record_mac')
    require(hmac.compare_digest(sign_record(record,key)['mac'],record['mac']),'record_authentication_failed')
    return copy.deepcopy(record)

def validate_request_descriptor(value,grant):
    exact(value,REQUEST_KEYS,'invalid_request_descriptor')
    for key in ('request_id','file_id','folder_id'):safe_id(value[key])
    require(type(value['seq']) is int and 1<=value['seq']<=grant['limits']['max_requests_per_route'],'request_budget_exhausted')
    require(valid_hash(value['request_sha256']),'invalid_request_hash')
    require(type(value['byte_length']) is int and 1<=value['byte_length']<=grant['limits']['max_request_bytes'],'request_too_large')
    require(value['folder_id']==grant['folder_id'],'request_folder_mismatch')
    require(type(value['begin_before']) is int and grant['created_at']<value['begin_before']<=grant['expires_at'],'invalid_request_deadline')
    ack=value['previous_result_ack']
    if ack is not None:
        exact(ack,{'result_id','result_sha256'},'invalid_previous_result_ack');safe_id(ack['result_id'])
        require(valid_hash(ack['result_sha256']),'invalid_previous_result_ack')
    require((value['seq']==1)==(ack is None),'missing_or_unexpected_previous_ack')
    return copy.deepcopy(value)

def validate_route(route,grant):
    exact(route,ROUTE_KEYS,'invalid_route')
    safe_id(route['route_id']);safe_id(route['outbox_id'])
    require(valid_hash(route['identity_sha256']),'invalid_route_identity')
    require({'model':route['model'],'reasoning_effort':route['reasoning_effort']} in grant['allowed_pairs'],'pair_not_authorized')
    require(type(route['stop']) is bool,'invalid_route_stop')
    if route['request'] is not None:validate_request_descriptor(route['request'],grant)
    return copy.deepcopy(route)

def parse_inbox(resource,pinned_grant,key):
    grant=validate_grant(pinned_grant)
    if type(resource) is dict and resource.get('kind')!='inbox' and any(k in resource for k in ('documentId','structuredContent','result')):
        from .docs import snapshot
        resource=snapshot(resource,grant['inbox_id'],max_bytes=INBOX_MAX_BYTES)
    record=_record(resource,INBOX_MAX_BYTES)
    verify_record(record,key,'inbox',INBOX_MAX_BYTES)
    exact(record,{'protocol','kind','grant','routes','stop','operation_id','mac'},'invalid_inbox')
    require(record['grant']==grant,'immutable_grant_mismatch');safe_id(record['operation_id'])
    require(type(record['stop']) is bool and type(record['routes']) is list and len(record['routes'])<=grant['limits']['max_routes'],'invalid_inbox_routes')
    for route in record['routes']:validate_route(route,grant)
    require(len({r['route_id'] for r in record['routes']})==len(record['routes']),'duplicate_route')
    ids=[r['outbox_id'] for r in record['routes']]
    require(len(set(ids))==len(ids) and grant['inbox_id'] not in ids,'document_reuse')
    return copy.deepcopy(record)

def validate_admission(admission,record):
    exact(admission,{'adapter','native_task_id','submitted_model','submitted_reasoning_effort','fork_turns',
                     'arguments_sha256','tool_result_sha256','verification','underlying_model_verified'},'invalid_admission')
    require(admission['adapter']=='collaboration.spawn_agent' and admission['native_task_id']==record['child_task_id']
            and admission['submitted_model']==record['model'] and admission['submitted_reasoning_effort']==record['reasoning_effort']
            and admission['fork_turns']=='none' and admission['verification']=='parent_recorded_platform_admission'
            and admission['underlying_model_verified'] is False,'admission_binding_mismatch')
    require(valid_hash(admission['arguments_sha256']) and valid_hash(admission['tool_result_sha256']),'invalid_admission_hash')

def parse_outbox(resource,route,key,grant=None):
    if type(resource) is dict and resource.get('kind')!='outbox' and any(k in resource for k in ('documentId','structuredContent','result')):
        from .docs import snapshot
        resource=snapshot(resource,route['outbox_id'],max_bytes=OUTBOX_MAX_BYTES)
    record=_record(resource,OUTBOX_MAX_BYTES);verify_record(record,key,'outbox',OUTBOX_MAX_BYTES)
    exact(record,OUTBOX_KEYS,'invalid_outbox')
    for name in ('activation_id','route_id','operation_id','spawn_operation_id','parent_task_id'):safe_id(record[name])
    require(all(record[k]==route[k] for k in ('route_id','identity_sha256','model','reasoning_effort')),'outbox_route_mismatch')
    require(record['phase'] in PHASES,'invalid_outbox_phase')
    require(type(record['consumed_seq']) is int and record['consumed_seq']>=0,'invalid_consumed_seq')
    if grant is not None:
        validate_grant(grant);validate_route(route,grant)
        require(record['activation_id']==grant['activation_id'],'activation_mismatch')
        require(record['consumed_seq']<=grant['limits']['max_requests_per_route'],'request_budget_exhausted')
    if record['phase']=='SPAWN_RESERVED':
        require(record['child_task_id'] is None and record['admission'] is None,'premature_admission')
    else:
        safe_id(record['child_task_id']);validate_admission(record['admission'],record)
    if record['phase'] in {'SPAWN_RESERVED','ADMITTED'}:
        require(record['consumed_seq']==0 and record['request'] is None and record['begin_operation_id'] is None and record['result'] is None,'premature_request_state')
    else:
        safe_id(record['begin_operation_id'])
        require(type(record['request']) is dict and record['request'].get('seq')==record['consumed_seq'],'outbox_request_mismatch')
        if grant is not None:validate_request_descriptor(record['request'],grant)
        if record['phase']=='BEGIN':require(record['result'] is None,'premature_result')
        else:
            exact(record['result'],RESULT_KEYS,'invalid_result_locator')
            for name in ('result_id','file_id','folder_id'):safe_id(record['result'][name])
            require(valid_hash(record['result']['result_sha256']) and type(record['result']['byte_length']) is int and record['result']['byte_length']>0,'invalid_result_locator')
            if grant is not None:require(record['result']['folder_id']==grant['folder_id'] and record['result']['byte_length']<=grant['limits']['max_result_bytes'],'result_bounds_mismatch')
    return copy.deepcopy(record)

def _record(resource,cap):
    if isinstance(resource,(str,bytes)):
        require(len(resource.encode() if isinstance(resource,str) else resource)<=cap+1,'record_too_large')
        raw=resource.decode() if isinstance(resource,bytes) else resource
        require(raw.endswith('\n'),'canonical_document_newline_required')
        record=strict_json(raw[:-1]);require(canonical(record).decode()+'\n'==raw,'noncanonical_record')
        return record
    if type(resource) is dict and 'text' in resource and 'revision_id' in resource:return _record(resource['text'],cap)
    return copy.deepcopy(resource)

def validate_result_envelope(value,grant,route,outbox):
    validate_grant(grant);validate_route(route,grant)
    require(outbox['activation_id']==grant['activation_id'] and all(outbox[k]==route[k] for k in ('route_id','identity_sha256','model','reasoning_effort')),'result_route_mismatch')
    exact(value,{'protocol','kind','activation_id','route_id','request_id','request_sha256','seq',
                 'begin_operation_id','child_task_id','model','reasoning_effort','result_id','output'},'invalid_result_envelope')
    require(value['protocol']==PROTOCOL and value['kind']=='result','protocol_version_mismatch')
    for key in ('activation_id','route_id','begin_operation_id','child_task_id','model','reasoning_effort'):
        require(value[key]==outbox[key],'result_binding_mismatch')
    for key in ('request_id','request_sha256','seq'):
        require(value[key]==outbox['request'][key],'result_request_mismatch')
    require(value['result_id']==outbox['result']['result_id'],'result_identity_mismatch')
    require(type(value['output']) is dict,'invalid_result_output')
    return copy.deepcopy(value)

def make_inbox(grant,routes,key,operation_id,stop=False):
    value=sign_record({'protocol':PROTOCOL,'kind':'inbox','grant':grant,'routes':routes,'stop':stop,'operation_id':operation_id},key)
    return parse_inbox(value,grant,key)
