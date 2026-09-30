"""Strict repository-research tool admission and actual multi-call receipt correlation."""
import hashlib
import json
import re
import shlex
from pathlib import Path

from portable import QueueError, protocol, digest, encode
from repo_fetch import parse, FetchError, strict_json, REPO, CONTRACT
from tool_probe import output_text

HELPER = str(Path(__file__).resolve().parent / 'repo_fetch.py')
PREFIX = 'python3 -I -B ' + shlex.quote(HELPER) + ' '
FIXED_ARGS = {'login': False, 'sandbox_permissions': 'use_default', 'yield_time_ms': 30000, 'max_output_tokens': 16384}
MAX_CALLS = 12


def command(action):
    return PREFIX + action


def parse_command(args):
    if not isinstance(args, dict) or set(args) != {'cmd', *FIXED_ARGS} or any(type(args[k]) is not type(v) or args[k] != v for k, v in FIXED_ARGS.items()):
        raise QueueError('repo_review_exec_arguments_denied')
    cmd = args.get('cmd')
    if not isinstance(cmd, str) or not cmd.startswith(PREFIX):
        raise QueueError('repo_review_command_denied')
    suffix = cmd[len(PREFIX):]
    if not suffix or ' '.join(suffix.split(' ')) != suffix or any(not x for x in suffix.split(' ')):
        raise QueueError('repo_review_command_denied')
    try:
        return parse(suffix.split(' '))
    except FetchError:
        raise QueueError('repo_review_command_denied') from None


def validate_function(request, result, jid):
    try:
        item = protocol.validate_result(result, request, jid)
    except protocol.BridgeError as e:
        raise QueueError(e.code) from None
    if result.get('kind') != 'function_call' or result.get('name') != 'exec_command':
        raise QueueError('repo_review_tool_denied')
    action = parse_command(result.get('arguments'))
    tool = protocol.request_tools(request).get((result.get('namespace'), result['name']))
    schema = tool.get('parameters') if tool else None
    if not isinstance(schema, dict) or schema.get('type') != 'object' or len(encode(schema)) > 65536:
        raise QueueError('unsupported_advertised_schema')
    def check_refs(v, depth=0):
        if depth > 32:
            raise QueueError('unsupported_advertised_schema')
        if isinstance(v, dict):
            for k, x in v.items():
                if k in ('$ref', '$dynamicRef', '$recursiveRef'):
                    raise QueueError('schema_reference_rejected')
                check_refs(x, depth + 1)
        elif isinstance(v, list):
            for x in v:
                check_refs(x, depth + 1)
    check_refs(schema)
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        raise QueueError('tool_schema_validator_unavailable') from None
    try:
        Draft202012Validator.check_schema(schema)
        if not Draft202012Validator(schema).is_valid(result['arguments']):
            raise QueueError('arguments_do_not_match_advertised_schema')
    except QueueError:
        raise
    except Exception:
        raise QueueError('invalid_advertised_schema') from None
    props = schema.get('properties', {})
    for k, typ in {'cmd': 'string', 'login': 'boolean', 'sandbox_permissions': 'string', 'yield_time_ms': 'number', 'max_output_tokens': 'number'}.items():
        shape = props.get(k)
        if not isinstance(shape, dict) or not isinstance(shape.get('type'), str) or shape.get('type') not in ({'integer', 'number'} if typ == 'number' else {typ}):
            raise QueueError('unsupported_advertised_schema')
    return item, action


def receipt(output, action):
    text = output_text(output)
    if len(text.encode()) > 65536:
        raise QueueError('repo_review_output_too_large')
    match = re.fullmatch(r'(?:Chunk ID: [A-Za-z0-9_-]+\n)?Wall time: [0-9]+(?:\.[0-9]+)? seconds\nProcess exited with code ([0-9]+)\n(?:Original token count: [0-9]+\n)?(?:Output|Final output):\n(.*)', text, re.S)
    if match is None:
        # Running or unknown outcomes remain unresolved; they cannot unlock a final/replay.
        raise QueueError('repo_review_execution_outcome_unresolved')
    try:
        body = strict_json(match[2])
    except FetchError:
        return {'ok': False, 'error': 'helper_output_invalid_json', 'output_sha256': digest(text)}
    if not isinstance(body, dict) or body.get('contract') != CONTRACT or body.get('repo') != REPO or body.get('request') != action:
        raise QueueError('repo_review_receipt_scope_mismatch')
    if body.get('ok') is True and match[1] != '0':
        raise QueueError('repo_review_exit_status_mismatch')
    if body.get('ok') is not True:
        return {'ok': False, 'error': body.get('error', 'helper_failed'), 'output_sha256': digest(text)}
    if action['action'] == 'tree':
        if not isinstance(body.get('commit'), str) or not re.fullmatch('[0-9a-f]{40}', body['commit']) or not isinstance(body.get('files'), list):
            raise QueueError('repo_review_invalid_tree_receipt')
    elif body.get('commit') != action['commit'] or body.get('path') != action['path']:
        raise QueueError('repo_review_receipt_scope_mismatch')
    return body


def history(d, request, reservations):
    inputs = request.get('input', [])
    if not isinstance(inputs, list) or any(not isinstance(x, dict) for x in inputs):
        raise QueueError('repo_review_invalid_history')
    if any(x.get('type') in ('custom_tool_call', 'custom_tool_call_output') for x in inputs):
        raise QueueError('repo_review_unrecognized_tool_history')
    calls = [x for x in inputs if x.get('type') == 'function_call']
    outputs = [x for x in inputs if x.get('type') == 'function_call_output']
    if len(calls) != len(reservations) or len(outputs) != len(reservations):
        raise QueueError('repo_review_incomplete_or_extra_history')
    observations = []
    prior_output = -1
    for r, call, out in zip(reservations, calls, outputs):
        if call.get('call_id') != r['call_id'] or out.get('call_id') != r['call_id'] or call.get('name') != 'exec_command' or call.get('namespace') != r['namespace']:
            raise QueueError('repo_review_call_correlation_failed')
        try:
            args = protocol.decode(call['arguments'].encode())
        except (KeyError, AttributeError, ValueError, protocol.BridgeError):
            raise QueueError('repo_review_call_arguments_mismatch') from None
        if digest(args) != r['arguments_sha256']:
            raise QueueError('repo_review_call_arguments_mismatch')
        ci, oi = inputs.index(call), inputs.index(out)
        if not prior_output < ci < oi:
            raise QueueError('repo_review_history_order_failed')
        prior_output = oi
        q = d.queue()
        try:
            source = q.get(r['job_id'], owner=d.m['owner_id'], session='session_' + d.m['run_id'])
        finally:
            q.close()
        if source['state'] != 'completed' or source['delivery'] != 'delivered' or digest(source.get('result')) != r['intent_sha256']:
            raise QueueError('repo_review_source_not_delivered')
        actual_output_sha = digest(out.get('output'))
        if r.get('output_sha256') is not None and r['output_sha256'] != actual_output_sha:
            raise QueueError('repo_review_receipt_changed')
        observation = receipt(out.get('output'), r['action'])
        r['output_sha256'] = actual_output_sha
        observations.append(observation)
    return observations


def validate_and_reserve(d, s, job, request, result):
    if not isinstance(result, dict):
        raise QueueError('invalid_result')
    state = s.setdefault('repo_review', {'calls': [], 'final': None})
    reservations = state['calls']
    same_job = next((r for r in reservations if r['job_id'] == job['id']), None)
    # Same-job retry is only the exact original intent; it doesn't admit another tool.
    if same_job:
        if digest(result) != same_job['intent_sha256'] or job['request_sha256'] != same_job['request_sha256']:
            raise QueueError('repo_review_retry_changed')
        validate_function(request, result, job['id'])
        history(d, request, reservations[:reservations.index(same_job)])
        return
    if state['final']:
        if state['final'] == {'job_id': job['id'], 'result_sha256': digest(result)}:
            return
        raise QueueError('repo_review_already_final')
    observations = history(d, request, reservations)
    d.save(s)  # Pin first-seen actual outputs before admitting the next intent.
    if result.get('kind') == 'function_call':
        item, action = validate_function(request, result, job['id'])
        if len(reservations) >= MAX_CALLS:
            raise QueueError('repo_review_tool_budget_exhausted')
        if any(o.get('ok') is not True for o in observations):
            raise QueueError('repo_review_stop_after_failure')
        if not reservations:
            if action != {'action': 'tree'}:
                raise QueueError('repo_review_tree_required_first')
        else:
            if action['action'] == 'tree':
                raise QueueError('repo_review_tree_already_fetched')
            tree = observations[0]
            if action.get('commit') != tree['commit']:
                raise QueueError('repo_review_commit_not_pinned')
            if action.get('path') not in {x.get('path') for x in tree['files'] if x.get('mode') in ('100644', '100755')}:
                raise QueueError('repo_review_path_not_in_tree')
        reservations.append(dict(job_id=job['id'], request_sha256=job['request_sha256'], call_id=item['call_id'],
                                 namespace=result.get('namespace'), intent_sha256=digest(result), arguments_sha256=digest(result['arguments']), action=action))
        d.save(s)  # Reservation persists before any tool intent can be delivered.
    elif result.get('kind') == 'message':
        if set(result) != {'kind', 'text'} or not isinstance(result.get('text'), str) or not result['text'].strip() or len(result['text'].encode()) > 24000:
            raise QueueError('repo_review_invalid_final')
        if not reservations:
            raise QueueError('repo_review_requires_actual_fetch')
        failed = any(o.get('ok') is not True for o in observations)
        if not failed and (len(observations) < 4 or sum(r['action']['action'] == 'read' for r in reservations) < 2):
            raise QueueError('repo_review_more_research_required')
        state['final'] = {'job_id': job['id'], 'result_sha256': digest(result)}
        state['outcome'] = 'blocked_with_actual_evidence' if failed else 'research_complete'
        state['receipts'] = [{'call_id': r['call_id'], 'action': r['action'], 'ok': o.get('ok'), 'output_sha256': digest(o)} for r, o in zip(reservations, observations)]
        d.save(s)
    else:
        raise QueueError('repo_review_result_kind_denied')
