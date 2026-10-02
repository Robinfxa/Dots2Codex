"""Pure Responses validation and SSE. No inference, transport or legacy imports.

Full history is mandatory. Tool item/call IDs and namespace are never generated
or rewritten. Synthetic native-result builders belong to test fixtures only.
"""
from __future__ import annotations
import copy
import json
from .protocol import ProtocolError, canonical, require, sha256

MAX_BYTES = 1024 * 1024


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, 'duplicate_json_key')
            value[key] = item
        return value
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError('invalid_json') from None


def _text(value):
    require(isinstance(value, str) or (isinstance(value, list) and all(
        isinstance(p, dict) and p.get('type') in {'input_text', 'output_text'} and
        isinstance(p.get('text'), str) for p in value)), 'text_content_required')


def request_tools(request):
    result = {}
    sources = list(request.get('tools') or [])
    for item in request['input']:
        if item.get('type') == 'additional_tools':
            require(isinstance(item.get('tools'), list), 'invalid_additional_tools')
            sources.extend(item['tools'])
    def visit(tool, namespace=None):
        require(isinstance(tool, dict), 'invalid_tool')
        name, kind = tool.get('name'), tool.get('type')
        require(isinstance(name, str) and 0 < len(name) <= 256, 'invalid_tool_name')
        if kind == 'namespace':
            require(namespace is None and isinstance(tool.get('tools'), list), 'invalid_tool_namespace')
            for child in tool['tools']:
                visit(child, name)
        else:
            require(kind in {'function', 'custom'}, 'unsupported_tool_type')
            key = (namespace, name)
            require(key not in result, 'duplicate_tool')
            result[key] = tool
            if kind == 'function':
                _schema(tool.get('parameters'))
            elif 'format' in tool:
                require(isinstance(tool['format'], dict) and tool['format'].get('type') in {'text', 'grammar'},
                        'unsupported_custom_tool_format')
    for tool in sources:
        visit(tool)
    return result


def _schema(schema, value=None, *, check_value=False):
    require(isinstance(schema, dict) and schema.get('type') == 'object', 'tool_schema_required')
    def inspect(item, depth=0):
        require(depth <= 32, 'tool_schema_too_deep')
        if isinstance(item, dict):
            require(not any(k in item for k in ('$ref', '$dynamicRef', '$recursiveRef')), 'tool_schema_reference_rejected')
            for child in item.values(): inspect(child, depth + 1)
        elif isinstance(item, list):
            for child in item: inspect(child, depth + 1)
    inspect(schema)
    try:
        from jsonschema import Draft202012Validator
        Draft202012Validator.check_schema(schema)
        if check_value:
            require(Draft202012Validator(schema).is_valid(value), 'tool_arguments_schema_mismatch')
    except ImportError:
        raise ProtocolError('tool_schema_validator_unavailable') from None
    except ProtocolError:
        raise
    except Exception:
        raise ProtocolError('invalid_tool_schema') from None


def call_binding(item):
    """Exact execution-affecting fields; benign status annotations may vary."""
    kind = item.get('type')
    names = ('id', 'type', 'call_id', 'name', 'namespace',
             'arguments' if kind == 'function_call' else 'input')
    return {name: item[name] for name in names if name in item}


def validate_request(request, *, max_bytes=MAX_BYTES):
    require(isinstance(request, dict), 'invalid_request')
    require(len(canonical(request)) <= max_bytes, 'wire_request_too_large')
    require(request.get('stream') is True, 'stream_true_required')
    require(not any(k in request for k in ('reasoning_effort', 'model_reasoning_effort')), 'ambiguous_reasoning_effort')
    require(isinstance(request.get('model'), str) and request['model'], 'model_required')
    require(isinstance(request.get('reasoning'), dict) and
            isinstance(request['reasoning'].get('effort'), str) and request['reasoning']['effort'],
            'explicit_reasoning_effort_required')
    require(request.get('previous_response_id') is None, 'previous_response_id_unsupported_send_full_history')
    require(isinstance(request.get('input'), list), 'full_history_array_required')
    require(request.get('instructions') is None or isinstance(request['instructions'], str), 'invalid_instructions')
    require(request.get('tools') is None or isinstance(request['tools'], list), 'invalid_tools')
    calls, outputs, item_ids = {}, set(), set()
    for item in request['input']:
        require(isinstance(item, dict), 'invalid_input_item')
        kind = item.get('type', 'message' if 'role' in item else None)
        if 'id' in item:
            require(isinstance(item['id'], str) and item['id'] and item['id'] not in item_ids, 'duplicate_or_invalid_item_id')
            item_ids.add(item['id'])
        if kind == 'message':
            require(item.get('role') in {'user', 'assistant', 'system', 'developer'}, 'invalid_message_role')
            _text(item.get('content'))
        elif kind in {'function_call', 'custom_tool_call'}:
            cid = item.get('call_id')
            require(isinstance(cid, str) and cid and cid not in calls, 'duplicate_or_invalid_tool_call')
            require(isinstance(item.get('name'), str) and item['name'], 'invalid_tool_call_name')
            require(item.get('namespace') is None or isinstance(item['namespace'], str), 'invalid_tool_namespace')
            require(isinstance(item.get('arguments' if kind == 'function_call' else 'input'), str), 'invalid_tool_call_payload')
            if kind == 'function_call':
                require(isinstance(strict_json(item['arguments']), dict), 'tool_arguments_object_required')
            calls[cid] = item
        elif kind in {'function_call_output', 'custom_tool_call_output'}:
            cid = item.get('call_id')
            expected = 'function_call' if kind == 'function_call_output' else 'custom_tool_call'
            require(cid in calls and calls[cid]['type'] == expected and cid not in outputs, 'uncorrelated_tool_output')
            _text(item.get('output')); outputs.add(cid)
        elif kind == 'additional_tools':
            pass
        else:
            raise ProtocolError('unsupported_input_item')
    require(set(calls) == outputs, 'tool_outcome_unknown')
    request_tools(request)
    return copy.deepcopy(request)


def validate_response(response, request, *, max_bytes=MAX_BYTES):
    require(isinstance(response, dict) and len(canonical(response)) <= max_bytes, 'invalid_or_oversize_response')
    require(isinstance(response.get('id'), str) and response['id'], 'response_id_required')
    require(response.get('object', 'response') == 'response' and response.get('status') == 'completed', 'completed_response_required')
    require(response.get('model', request['model']) == request['model'], 'response_model_mismatch')
    require(response.get('error') is None, 'response_error_not_success')
    output = response.get('output')
    require(isinstance(output, list) and output, 'response_output_required')
    tools, ids, calls = request_tools(request), set(), set()
    old_calls = {item['call_id'] for item in request['input'] if item.get('type') in {'function_call', 'custom_tool_call'}}
    for item in output:
        require(isinstance(item, dict), 'invalid_response_item')
        require(isinstance(item.get('id'), str) and item['id'] and item['id'] not in ids, 'response_item_id_required')
        ids.add(item['id']); kind = item.get('type')
        if kind == 'message':
            require(item.get('role') == 'assistant' and isinstance(item.get('content'), list), 'invalid_assistant_message')
            require(all(isinstance(p, dict) and p.get('type') == 'output_text' and isinstance(p.get('text'), str)
                        for p in item['content']), 'text_response_required')
        elif kind in {'function_call', 'custom_tool_call'}:
            cid = item.get('call_id')
            require(isinstance(cid, str) and cid and cid not in calls and cid not in old_calls, 'duplicate_or_invalid_response_call_id')
            calls.add(cid)
            tool = tools.get((item.get('namespace'), item.get('name')))
            require(tool is not None and tool['type'] == ('function' if kind == 'function_call' else 'custom'), 'unadvertised_tool')
            if kind == 'function_call':
                require(isinstance(item.get('arguments'), str), 'arguments_string_required')
                args = strict_json(item['arguments'])
                require(isinstance(args, dict), 'tool_arguments_object_required')
                _schema(tool['parameters'], args, check_value=True)
            else:
                require(isinstance(item.get('input'), str), 'custom_input_string_required')
        else:
            raise ProtocolError('unsupported_response_item')
    return copy.deepcopy(response)


def validate_history(request, previous_request, prior_outputs, prior_call_hashes=None):
    """Full input prefix plus exact earlier calls, compactly hash-bound."""
    prior = []
    if previous_request is not None:
        prior = previous_request['input']
        require(request['input'][:len(prior)] == prior, 'full_history_prefix_mismatch')
    emitted = {item['call_id']: sha256(canonical(call_binding(item))) for output in prior_outputs for item in output['output']
               if item.get('type') in {'function_call', 'custom_tool_call'}}
    if prior_call_hashes is not None:
        emitted = prior_call_hashes
    supplied = {item['call_id']: sha256(canonical(call_binding(item))) for item in request['input']
                if item.get('type') in {'function_call', 'custom_tool_call'}}
    require(supplied == emitted, 'tool_history_does_not_match_delivered_calls')
    # The previous assistant answer is part of full history, too. Ignore only
    # optional item status/id annotations which the Codex serializer may omit.
    suffix = request['input'][len(prior):]
    messages = [{'role': x.get('role'), 'content': x.get('content')} for x in suffix
                if x.get('type', 'message' if 'role' in x else None) == 'message']
    for output in prior_outputs:
        for item in output['output']:
            if item.get('type') == 'message':
                require({'role': item['role'], 'content': item['content']} in messages,
                        'previous_assistant_message_missing')


def has_tools(response):
    return any(item.get('type') in {'function_call', 'custom_tool_call'} for item in response['output'])


def event_frame(event):
    return b'event: ' + event['type'].encode('ascii') + b'\ndata: ' + canonical(event) + b'\n\n'


def response_events(response):
    created = {**response, 'status': 'in_progress', 'output': []}
    events = [{'type': 'response.created', 'response': created}]
    for index, item in enumerate(response['output']):
        events.append({'type': 'response.output_item.added', 'output_index': index, 'item': item})
        events.append({'type': 'response.output_item.done', 'output_index': index, 'item': item})
    events.append({'type': 'response.completed', 'response': response})
    return b''.join(event_frame({**event, 'sequence_number': n}) for n, event in enumerate(events))
