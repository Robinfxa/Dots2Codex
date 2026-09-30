"""Reuse frozen Responses schema/SSE code; never modify its POSIX store."""
import sys
from pathlib import Path
from .model import ProtocolError, canonical, require, MAX_WIRE_BYTES, MAX_RESULT_BYTES
VENDOR = Path(__file__).resolve().parents[1] / 'vendor'
sys.path.insert(0,str(VENDOR))
from core import protocol, QueueError  # verifies frozen source hashes
from facade import ResponsesHandler, ResponsesServer


def validate_text_request(request):
    try:
        unknown = protocol.validate_request(request)
    except protocol.BridgeError as exc:
        raise ProtocolError(exc.code) from None
    require(not unknown, 'unsupported_input')
    require(all(item.get('type','message' if 'role' in item else None) in {'message','additional_tools'}
                for item in request['input']), 'text_only_input_required')
    # Tool advertisements may be present in Codex; no tool call may be returned.
    require(len(canonical(request)) <= MAX_WIRE_BYTES, 'wire_request_too_large')
    return request


def validate_remote_request(request,scope):
    if scope=='text_only':return validate_text_request(request)
    require(scope=='responses_tools','unsupported_scope')
    try:unknown=protocol.validate_request(request)
    except protocol.BridgeError as exc:raise ProtocolError(exc.code) from None
    require(not unknown,'unsupported_input')
    require(len(canonical(request))<=MAX_WIRE_BYTES,'wire_request_too_large')
    # Outputs alone are not authorization to create a call. Full emitted-call
    # correlation is checked against the controller's durable results below.
    seen={};outputs=set()
    for item in request['input']:
        kind=item.get('type')
        if kind in {'function_call','custom_tool_call'}:
            cid=item.get('call_id');require(isinstance(cid,str) and cid and cid not in seen,'duplicate_or_invalid_tool_call')
            seen[cid]=kind
        if kind in {'function_call_output','custom_tool_call_output'}:
            cid=item.get('call_id');expected='function_call' if kind=='function_call_output' else 'custom_tool_call'
            require(cid in seen and seen[cid]==expected and cid not in outputs,'uncorrelated_tool_output')
            require('output' in item,'tool_output_required')
            out=item['output']
            require(isinstance(out,str) or (isinstance(out,list) and all(
                isinstance(part,dict) and part.get('type') in {'input_text','output_text'} and
                isinstance(part.get('text'),str) for part in out)),'text_tool_output_required')
            outputs.add(cid)
    require(set(seen)==outputs,'tool_outcome_unknown')
    return request


def response_result(payload):
    return payload.get('response_result',{'kind':'message','text':payload.get('text')})


def result_item(payload,request,request_id,scope='text_only'):
    result=response_result(payload)
    require(scope=='responses_tools' or result.get('kind')=='message','text_only_result_required')
    try:item=protocol.validate_result(result,request,request_id)
    except protocol.BridgeError as exc:raise ProtocolError(exc.code) from None
    if item['type']=='function_call':
        schema=protocol.request_tools(request)[(result.get('namespace'),result['name'])].get('parameters')
        require(isinstance(schema,dict) and schema.get('type')=='object','tool_schema_required')
        def no_remote_refs(value,depth=0):
            require(depth<=32,'tool_schema_too_deep')
            if isinstance(value,dict):
                require(not any(k in value for k in ('$ref','$dynamicRef','$recursiveRef')),'tool_schema_reference_rejected')
                for child in value.values():no_remote_refs(child,depth+1)
            elif isinstance(value,list):
                for child in value:no_remote_refs(child,depth+1)
        no_remote_refs(schema)
        try:
            from jsonschema import Draft202012Validator
            Draft202012Validator.check_schema(schema)
            require(Draft202012Validator(schema).is_valid(result['arguments']),'tool_arguments_schema_mismatch')
        except ImportError:raise ProtocolError('tool_schema_validator_unavailable') from None
        except ProtocolError:raise
        except Exception:raise ProtocolError('invalid_tool_schema') from None
    require(len(canonical(result))<=MAX_RESULT_BYTES,'wire_result_too_large')
    return item
