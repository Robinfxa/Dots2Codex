"""Bounded native-hosted web contract. No network or native tool execution.

A receipt records a trusted route owner's report; it is not platform attestation.
The executor must obey the prepared mapping, execute once through its real native
web tool, and report the complete result. Unknown delivery is never retry authority.
"""
from __future__ import annotations
import copy
import re
from urllib.parse import urlsplit
from .wire_support import canonical, require

WEB_FIELDS = {'type', 'external_web_access', 'indexed_web_access', 'filters',
              'user_location', 'search_context_size', 'search_content_types'}
WEB_ERRORS = frozenset({
    'invalid_web_search_declaration', 'invalid_web_search_action', 'invalid_web_search_item',
    'unsupported_web_search_option', 'web_search_cached_unavailable', 'web_search_indexed_unavailable',
    'web_search_location_unavailable', 'web_search_context_size_unavailable',
    'web_search_content_types_unavailable', 'web_search_filters_unavailable',
    'web_search_domain_forbidden', 'invalid_web_search_url', 'native_web_tool_required',
    'native_web_arguments_mismatch', 'native_web_result_required', 'native_web_result_too_large',
    'native_web_sources_required', 'native_web_source_not_in_result', 'invalid_native_web_source',
    'hosted_receipt_required', 'hosted_receipt_mismatch', 'hosted_operation_conflict',
    'hosted_operation_pending', 'hosted_operation_unknown', 'hosted_operation_limit',
    'hosted_result_conflict', 'hosted_result_failed', 'hosted_history_mismatch',
    'invalid_url_citation', 'citation_source_not_verified', 'tool_choice_violation',
    'parallel_tool_calls_violation', 'invalid_tool_choice', 'invalid_parallel_tool_calls',
})


def validate_web_declaration(tool):
    require(tool.get('type') == 'web_search' and 'name' not in tool, 'invalid_web_search_declaration')
    # Unknown fields are preserved for capability reporting, never ignored on use.
    for field in ('external_web_access', 'indexed_web_access'):
        require(field not in tool or isinstance(tool[field], bool), 'invalid_web_search_declaration')
    if 'filters' in tool:
        filters = tool['filters']
        require(isinstance(filters, dict), 'invalid_web_search_declaration')
        if 'allowed_domains' in filters:
            require(isinstance(filters['allowed_domains'], list) and len(filters['allowed_domains']) <= 100
                    and all(isinstance(x, str) and x for x in filters['allowed_domains']),
                    'invalid_web_search_declaration')
    if 'user_location' in tool:
        require(isinstance(tool['user_location'], dict), 'invalid_web_search_declaration')
    if 'search_context_size' in tool:
        require(tool['search_context_size'] in ('low', 'medium', 'high'), 'invalid_web_search_declaration')
    if 'search_content_types' in tool:
        require(isinstance(tool['search_content_types'], list)
                and all(isinstance(x, str) for x in tool['search_content_types']), 'invalid_web_search_declaration')


def web_capability(tool):
    """Report gaps at context delivery, before the worker decides to use search."""
    reasons = []
    if set(tool) - WEB_FIELDS: reasons.append('unsupported_web_search_option')
    if tool.get('external_web_access') is False: reasons.append('web_search_cached_unavailable')
    if tool.get('indexed_web_access') is True: reasons.append('web_search_indexed_unavailable')
    if 'user_location' in tool: reasons.append('web_search_location_unavailable')
    if 'search_context_size' in tool: reasons.append('web_search_context_size_unavailable')
    if 'search_content_types' in tool: reasons.append('web_search_content_types_unavailable')
    if set(tool.get('filters', {})) - {'allowed_domains'}: reasons.append('web_search_filters_unavailable')
    return {'type': 'web_search', 'declaration_accepted': True,
            'execution': 'native_worker', 'native_tool': 'web.run',
            'mapping_supported': not reasons, 'executable': not reasons, 'requires_actual_native_tool': True,
            'capability_errors': reasons,
            'result_provenance': 'trusted_worker_report_not_platform_attestation'}


def domain(value):
    require(isinstance(value, str) and len(value) <= 253
            and re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', value)
            and '..' not in value, 'web_search_filters_unavailable')
    return value.lower()


def valid_url(value, allowed_domains=()):
    require(isinstance(value, str) and len(value) <= 8192
            and not any(ord(c) <= 32 or ord(c) == 127 for c in value)
            and '\\' not in value, 'invalid_web_search_url')
    try:
        parsed = urlsplit(value)
        require(parsed.scheme in {'https', 'http'} and parsed.hostname
                and parsed.username is None and parsed.password is None, 'invalid_web_search_url')
        host = parsed.hostname.lower()
        parsed.port  # Reject malformed/out-of-range ports.
    except ValueError:
        require(False, 'invalid_web_search_url')
    require(not allowed_domains or any(host == d or host.endswith('.' + d) for d in allowed_domains),
            'web_search_domain_forbidden')
    return value


def validate_action(action):
    require(isinstance(action, dict), 'invalid_web_search_action')
    kind = action.get('type')
    if kind == 'search':
        require(not (set(action) - {'type', 'query', 'queries'}), 'invalid_web_search_action')
        queries = action.get('queries') if 'queries' in action else [action.get('query')]
        require(('query' not in action or isinstance(action['query'], str) and 0 < len(action['query']) <= 4096)
                and isinstance(queries, list) and 1 <= len(queries) <= 4
                and all(isinstance(q, str) and 0 < len(q) <= 4096 for q in queries),
                'invalid_web_search_action')
    elif kind in {'open_page', 'find_in_page'}:
        fields = {'type', 'url'} | ({'pattern'} if kind == 'find_in_page' else set())
        require(set(action) == fields, 'invalid_web_search_action')
        valid_url(action['url'])
        if kind == 'find_in_page':
            require(isinstance(action['pattern'], str) and 0 < len(action['pattern']) <= 4096,
                    'invalid_web_search_action')
    else:
        require(False, 'invalid_web_search_action')


def validate_web_item(item):
    require(isinstance(item.get('id'), str) and item['id'].startswith('ws_') and len(item['id']) > 3
            and item.get('status') in {'completed', 'failed'}, 'invalid_web_search_item')
    validate_action(item.get('action'))


def prepare_web(tool, action, item_id):
    capability = web_capability(tool)
    require(not capability['capability_errors'], capability['capability_errors'][0]
            if capability['capability_errors'] else 'unsupported_web_search_option')
    validate_action(action)
    domains = [domain(x) for x in tool.get('filters', {}).get('allowed_domains', [])]
    if action['type'] == 'search':
        queries = list(action.get('queries', []))
        if 'query' in action and action['query'] not in queries:
            queries.insert(0, action['query'])
        require(len(queries) <= 4, 'invalid_web_search_action')
        arguments = {'search_query': [{'q': q, **({'domains': domains} if domains else {})} for q in queries]}
        if len(queries) == 4:
            arguments['response_length'] = 'medium'  # Native batch requirement, not retrieval context size.
    else:
        valid_url(action['url'], domains)
        arguments = ({'open': [{'ref_id': action['url']}]} if action['type'] == 'open_page' else
                     {'find': [{'ref_id': action['url'], 'pattern': action['pattern']}]})
    item = {'id': item_id, 'type': 'web_search_call', 'status': 'completed', 'action': copy.deepcopy(action)}
    validate_web_item(item)
    return {'native_tool': 'web.run', 'native_arguments': arguments,
            'item': item, 'allowed_domains': domains}


def verify_result(prepared, result):
    require(isinstance(result, dict) and result.get('native_tool') == prepared['native_tool'], 'native_web_tool_required')
    require(result.get('native_arguments') == prepared['native_arguments'], 'native_web_arguments_mismatch')
    raw = result.get('native_result')
    require(isinstance(raw, (str, dict, list)) and raw, 'native_web_result_required')
    encoded = canonical(raw)
    require(len(encoded) <= 512 * 1024, 'native_web_result_too_large')
    require(result.get('status') in {'completed', 'failed'}, 'native_web_result_required')
    sources = result.get('sources')
    require(isinstance(sources, list) and len(sources) <= 100, 'native_web_sources_required')
    if result['status'] == 'failed':
        require(not sources, 'invalid_native_web_source')
    urls = set()
    for source in sources:
        require(isinstance(source, dict) and set(source) == {'url', 'title', 'reference'}
                and isinstance(source['title'], str) and len(source['title']) <= 4096
                and isinstance(source['reference'], str) and 0 < len(source['reference']) <= 256, 'invalid_native_web_source')
        url = valid_url(source['url'], prepared['allowed_domains'])
        require(url not in urls, 'invalid_native_web_source')
        urls.add(url)
        require(canonical(url)[1:-1] in encoded and canonical(source['reference'])[1:-1] in encoded,
                'native_web_source_not_in_result')
    return copy.deepcopy(result)


def validate_hosted_output(response, receipts, *, prior_citation_sources=()):
    receipts = list(receipts)
    verified = {x['prepared']['item']['id']: x for x in receipts if x.get('result') is not None}
    require({item['id'] for item in response['output'] if item.get('type') == 'web_search_call'}
            == set(verified), 'hosted_receipt_mismatch')
    for item in response['output']:
        if item.get('type') == 'web_search_call':
            require(item['id'] in verified, 'hosted_receipt_required')
            expected = {**verified[item['id']]['prepared']['item'], 'status': verified[item['id']]['result']['status']}
            require(item == expected, 'hosted_receipt_mismatch')
    sources = {s['url'] for receipt in verified.values() for s in receipt['result']['sources']}
    sources.update(prior_citation_sources)
    for item in response['output']:
        if item.get('type') != 'message': continue
        for part in item['content']:
            for annotation in part.get('annotations', []):
                require(isinstance(annotation, dict) and annotation.get('type') == 'url_citation', 'invalid_url_citation')
                require(type(annotation.get('start_index')) is int and type(annotation.get('end_index')) is int
                        and 0 <= annotation['start_index'] < annotation['end_index'] <= len(part['text'])
                        and isinstance(annotation.get('title'), str), 'invalid_url_citation')
                require(annotation.get('url') in sources, 'citation_source_not_verified')


def enforce_tool_policy(request, calls):
    choice = request.get('tool_choice', 'auto')
    require(isinstance(choice, (str, dict)), 'invalid_tool_choice')
    if choice == 'none': require(not calls, 'tool_choice_violation')
    if choice == 'required': require(bool(calls), 'tool_choice_violation')
    if isinstance(choice, dict):
        require(bool(calls), 'tool_choice_violation')
        kind = choice.get('type')
        require(kind in {'function', 'custom', 'web_search', 'tool_search'}, 'invalid_tool_choice')
        if kind in {'function', 'custom'}:
            require(isinstance(choice.get('name'), str) and bool(choice['name'])
                    and (choice.get('namespace') is None or isinstance(choice['namespace'], str)), 'invalid_tool_choice')
        for call in calls:
            if kind in {'function', 'custom'}:
                require(call.get('type') == ('function_call' if kind == 'function' else 'custom_tool_call')
                        and call.get('name') == choice.get('name')
                        and call.get('namespace') == choice.get('namespace'), 'tool_choice_violation')
            else:
                require(kind in {'web_search', 'tool_search'} and call.get('type') == kind + '_call', 'tool_choice_violation')
    if request.get('parallel_tool_calls') is False:
        require(sum(call.get('type') != 'web_search_call' for call in calls) <= 1, 'parallel_tool_calls_violation')
