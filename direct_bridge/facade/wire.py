"""Pure Responses validation and SSE. No inference, transport or legacy imports.

Full history is mandatory. Tool item/call IDs and namespace are never generated
or rewritten. Synthetic native-result builders belong to test fixtures only.
"""
from __future__ import annotations
import copy
import json
from decimal import Decimal
from .wire_support import ProtocolError, canonical, require, sha256

MAX_BYTES = 1024 * 1024
HOSTED_TOOL_TYPES = frozenset({'web_search', 'web_search_preview', 'file_search', 'code_interpreter',
                              'image_generation', 'computer', 'computer_use_preview'})
TOOL_DIAGNOSTIC_TYPES = HOSTED_TOOL_TYPES | {'namespace', 'function', 'custom', 'tool_search', 'unknown'}


def _tool_error(code, tool, index, child_index=None):
    error = ProtocolError(code)
    kind = tool.get('type')
    error.details = {'tool_index': index, 'tool_type': kind if isinstance(kind, str) and kind in TOOL_DIAGNOSTIC_TYPES else 'unknown',
                     'name_present': 'name' in tool}
    if child_index is not None: error.details['child_index'] = child_index
    raise error


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, 'duplicate_json_key')
            value[key] = item
        return value
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        # Tool arguments remain an exact emitted string. Reject loss during the
        # validation projection, including negative zero, rather than validating
        # a rounded number and later emitting different numeric semantics.
        def number(token):
            decimal = Decimal(token)
            return decimal, decimal.is_zero() and decimal.is_signed()
        original = json.loads(raw, parse_int=number, parse_float=number)
        projected = json.loads(canonical(value), parse_int=number, parse_float=number)
        require(original == projected, 'numeric_precision_loss')
        return value
    except ProtocolError:
        raise
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
    def visit(tool, index, namespace=None, child_index=None):
        require(isinstance(tool, dict), 'invalid_tool')
        name, kind = tool.get('name'), tool.get('type')
        # Hosted/client-discovery declarations are not ordinary named tools.
        # Reject their semantics explicitly before applying name validation.
        if isinstance(kind, str) and kind in HOSTED_TOOL_TYPES:
            _tool_error('unsupported_hosted_tool', tool, index, child_index)
        if kind == 'tool_search':
            _tool_error('unsupported_client_tool_search', tool, index, child_index)
        if not isinstance(kind, str) or kind not in {'namespace', 'function', 'custom'}:
            _tool_error('unsupported_tool_type', tool, index, child_index)
        if not isinstance(name, str) or not 0 < len(name) <= 256:
            _tool_error('invalid_tool_name', tool, index, child_index)
        if kind == 'namespace':
            require(namespace is None and isinstance(tool.get('tools'), list), 'invalid_tool_namespace')
            for offset, child in enumerate(tool['tools']):
                visit(child, index, name, offset)
        else:
            key = (namespace, name)
            require(key not in result, 'duplicate_tool')
            result[key] = tool
            if kind == 'function':
                _schema(tool.get('parameters'))
            elif 'format' in tool:
                require(isinstance(tool['format'], dict) and tool['format'].get('type') in {'text', 'grammar'},
                        'unsupported_custom_tool_format')
    for index, tool in enumerate(sources):
        visit(tool, index)
    return result


# These limits bound schema traversal and recursive/combinatorial evaluation.
# They do not expand references or change the original wire schema.
SCHEMA_MAX_NODES = 16384
SCHEMA_MAX_STEPS = 50000
SCHEMA_MAX_EVALUATION_DEPTH = 64


def _schema_shape_budget(schema):
    pending, count = [(schema, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        require(count <= SCHEMA_MAX_NODES, 'tool_schema_too_complex')
        require(depth <= 32, 'tool_schema_too_deep')
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)


def _schema_reference(ref):
    """Normalize URI fragments once; enforce RFC 6901 escape syntax."""
    from urllib.parse import quote, unquote
    import re
    require(isinstance(ref, str), 'invalid_tool_schema')
    base, marker, fragment = ref.partition('#')
    if not marker:
        return ref, None
    fragment = unquote(fragment, errors='strict')
    if fragment.startswith('/'):
        require(not re.search(r'~(?:[^01]|$)', fragment), 'tool_schema_reference_invalid')
        # referencing decodes pointer fragments itself. Quote again to avoid
        # double decoding e.g. a property literally named "%2F".
        return base + '#/' + quote(fragment[1:], safe='/~'), fragment
    return base + '#' + fragment, fragment


def _schema_lookup(resolver, ref):
    """Resolve only against the closed registry; reject non-schema targets."""
    normalized, fragment = _schema_reference(ref)
    if fragment is not None and fragment.startswith('/'):
        # referencing accepts Python sequence indexes such as -1 and 01;
        # JSON Pointer permits neither. Check the pointer before lookup.
        import re
        base = normalized.partition('#')[0]
        target = resolver.lookup(base or '#').contents
        for token in fragment[1:].split('/'):
            token = token.replace('~1', '/').replace('~0', '~')
            if isinstance(target, list):
                require(re.fullmatch(r'0|[1-9][0-9]*', token) is not None,
                        'tool_schema_reference_invalid')
                target = target[int(token)]
            else:
                require(isinstance(target, dict), 'tool_schema_reference_invalid')
                target = target[token]
    resolved = resolver.lookup(normalized)
    require(isinstance(resolved.contents, (dict, bool)), 'tool_schema_reference_invalid')
    return resolved


def _schema_validator(schema):
    from jsonschema import Draft201909Validator, Draft202012Validator, validators
    from referencing import Registry
    from referencing.exceptions import NoSuchResource
    from referencing.jsonschema import DRAFT201909, DRAFT202012
    from urllib.parse import urljoin

    dialects = {
        'https://json-schema.org/draft/2020-12/schema': (Draft202012Validator, DRAFT202012),
        'https://json-schema.org/draft/2019-09/schema': (Draft201909Validator, DRAFT201909),
    }
    dialect = schema.get('$schema', 'https://json-schema.org/draft/2020-12/schema')
    require(isinstance(dialect, str) and dialect.rstrip('#') in dialects,
            'tool_schema_dialect_unsupported')
    dialect = dialect.rstrip('#')
    base_validator, specification = dialects[dialect]
    base_validator.check_schema(schema)

    def deny_retrieval(uri):
        # Never add filesystem, HTTP, metaschema, or global registry fallbacks.
        raise NoSuchResource(ref=uri)

    def inspect_node(item):
        require(isinstance(item, (dict, bool)), 'invalid_tool_schema')
        if isinstance(item, bool):
            return
        declared = item.get('$schema', dialect)
        require(isinstance(declared, str) and declared.rstrip('#') == dialect,
                'tool_schema_dialect_unsupported')
        for keyword in ('$ref', '$dynamicRef', '$recursiveRef'):
            if keyword in item:
                require(keyword in base_validator.VALIDATORS,
                        'tool_schema_reference_unsupported')
                _schema_reference(item[keyword])
        if '$recursiveRef' in item:
            require(item['$recursiveRef'] == '#', 'tool_schema_reference_unsupported')

    # Only schema positions participate in resource discovery. A property named
    # "$ref", examples, enum/const/default values, and arbitrary annotations are
    # data, not reference instructions. Reject ambiguous IDs/anchors explicitly.
    root = specification.create_resource(schema)
    initial_uri = root.id() or ''
    resources, anchors, structural = {}, set(), []
    pending = [(root, '')]
    while pending:
        resource, parent_uri = pending.pop()
        inspect_node(resource.contents)
        uri = urljoin(parent_uri, resource.id()) if resource.id() is not None else parent_uri
        if resource.id() is not None or resource is root:
            require(uri not in resources, 'tool_schema_resource_duplicate')
            resources[uri] = resource
        for anchor in resource.anchors():
            require((uri, anchor.name) not in anchors, 'tool_schema_anchor_duplicate')
            anchors.add((uri, anchor.name))
        structural.append((resource, uri))
        pending.extend((specification.create_resource(child), uri)
                       for child in specification.subresources_of(resource.contents))
    registry = Registry(retrieve=deny_retrieval).with_resource('', root).crawl()
    resolver = registry.resolver(initial_uri)

    # Preflight all refs, even optional/unused branches, so a bad local pointer
    # or an external target fails before request admission. Follow references
    # without expansion; visited schema/base pairs terminate reference cycles.
    pending = [(resource, registry.resolver(uri)) for resource, uri in structural]
    seen = set()
    checked = {id(resource.contents) for resource, _ in structural}
    while pending:
        resource, current = pending.pop()
        item = resource.contents
        key = (id(item), current._base_uri)
        if key in seen:
            continue
        seen.add(key)
        require(len(seen) <= SCHEMA_MAX_NODES, 'tool_schema_too_complex')
        inspect_node(item)
        if id(item) not in checked:
            base_validator.check_schema(item)
            checked.add(id(item))
        if isinstance(item, bool):
            continue
        for keyword in ('$ref', '$dynamicRef', '$recursiveRef'):
            if keyword in item:
                try:
                    resolved = _schema_lookup(current, item[keyword])
                except ProtocolError:
                    raise
                except Exception:
                    raise ProtocolError('tool_schema_reference_unresolvable') from None
                pending.append((specification.create_resource(resolved.contents), resolved.resolver))
        for child in specification.subresources_of(item):
            subresource = specification.create_resource(child)
            pending.append((subresource, current.in_subresource(subresource)))

    budget = {'steps': 0, 'depth': 0}
    class ClosedResolver:
        # Annotation helpers (unevaluatedProperties/Items) also call lookup
        # directly. Normalize and budget at the resolver boundary, not only
        # inside the reference keyword handler.
        def __init__(self, inner):
            self.inner = inner

        def lookup(self, ref):
            budget['steps'] += 1
            require(budget['steps'] <= SCHEMA_MAX_STEPS, 'tool_schema_validation_too_complex')
            resolved = _schema_lookup(self.inner, ref)
            return type(resolved)(contents=resolved.contents, resolver=ClosedResolver(resolved.resolver))

        def in_subresource(self, resource):
            return ClosedResolver(self.inner.in_subresource(resource))

        def dynamic_scope(self):
            return self.inner.dynamic_scope()

    def bounded(keyword, operation):
        def validate(validator, constraint, instance, subschema):
            budget['steps'] += 1
            budget['depth'] += 1
            try:
                require(budget['steps'] <= SCHEMA_MAX_STEPS and
                        budget['depth'] <= SCHEMA_MAX_EVALUATION_DEPTH,
                        'tool_schema_validation_too_complex')
                yield from operation(validator, constraint, instance, subschema)
            finally:
                budget['depth'] -= 1
        return validate
    bounded_validator = validators.extend(base_validator, {
        keyword: bounded(keyword, operation)
        for keyword, operation in base_validator.VALIDATORS.items()})

    def evolve(self, **changes):
        # jsonschema's default evolve chooses an unbounded built-in validator
        # when a child contains $schema. All dialects were checked above; retain
        # this validator and its per-validation budget across every descent.
        changes.setdefault('schema', self.schema)
        changes.setdefault('registry', self._registry)
        changes.setdefault('_resolver', self._resolver)
        changes.setdefault('format_checker', self.format_checker)
        return bounded_validator(**changes)
    bounded_validator.evolve = evolve
    # Explicit _resolver avoids jsonschema's implicit built-in metaschema
    # registry merge. Only resources contained in this tool schema exist here.
    return bounded_validator(schema, registry=registry, _resolver=ClosedResolver(resolver))


def _schema(schema, value=None, *, check_value=False):
    require(isinstance(schema, dict) and schema.get('type') == 'object', 'tool_schema_required')
    _schema_shape_budget(schema)
    try:
        validator = _schema_validator(schema)
        if check_value:
            require(validator.is_valid(value), 'tool_arguments_schema_mismatch')
    except ImportError:
        raise ProtocolError('tool_schema_validator_unavailable') from None
    except ProtocolError:
        raise
    except RecursionError:
        raise ProtocolError('tool_schema_validation_too_complex') from None
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
            # The pinned Codex client splits response item IDs at the first
            # underscore. Preserve the ID verbatim; never rewrite call_id.
            prefix, separator, suffix = item['id'].partition('_')
            require(prefix and separator and suffix, 'tool_item_id_prefix_suffix_required')
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


def message_binding(item):
    """Exact message content, excluding known Codex-dropped text metadata.

    The pinned Codex ContentItem::OutputText stores only text. Its typed
    round-trip drops Responses annotations/logprobs before echoing history.
    Normalize only those fields for comparison; never rewrite source history or
    emitted responses, concatenate parts, coerce input_text, or discard unknown
    fields. Role, text bytes, content types, boundaries and order stay exact.
    """
    content = item.get('content')
    if isinstance(content, list):
        content = [{key: value for key, value in part.items()
                    if key not in {'annotations', 'logprobs'}}
                   if isinstance(part, dict) and part.get('type') == 'output_text'
                   else part for part in content]
    return {'role': item.get('role'), 'content': content}


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
    # optional item annotations and known typed-client text metadata omissions.
    suffix = request['input'][len(prior):]
    messages = [message_binding(x) for x in suffix
                if x.get('type', 'message' if 'role' in x else None) == 'message']
    for output in prior_outputs:
        for item in output['output']:
            if item.get('type') == 'message':
                require(message_binding(item) in messages,
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
