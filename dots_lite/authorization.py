"""Bounded transport scope carried in an actual owner's fresh JOIN.

This is a deterministic disclosure and binding, never a platform approval or
proof of message authorship. The actual native parent must verify the source
user message; neither a Doc nor an unsigned local file creates authorization.
"""
from __future__ import annotations
import copy
from datetime import datetime, timezone

from . import protocol as p

CONTRACT = 'dots-lite-transport-authorization/1'
JOIN_KEYS = {'activation_id', 'inbox_id', 'grant_sha256', 'join_code'}
MAX_JOIN_BYTES = 65536

TRANSPORT_PERMISSIONS = (
    'Read this activation\'s authenticated Inbox and its hash-bound request files. '
    'Create and update only this activation\'s route-owned Outbox protocol records in the exact consented '
    'Google Drive folder, using exact revision compare-and-swap (CAS), including replacing the control Doc body '
    'for SPAWN_RESERVED, ADMITTED, BEGIN and RESULT. '
    'Upload actual user-task result JSON to that same folder. '
    'These result uploads and control-Doc writes may contain only the task result and necessary activation, '
    'route, native-task, operation, request and result IDs, sequence numbers, model/effort, file pointers, '
    'hashes, protocol/status fields and record-authenticator metadata. '
    'The JOIN secret and account credentials must never be uploaded or quoted. '
    'No unrelated Docs, file/Doc deletion, sharing changes, OAuth grants or scope expansion are authorized. '
    'Sensitive content needs the user\'s specific consent for that data and destination; '
    'highly sensitive credentials still require the user\'s secure handoff. '
    'Platform approvals, safety rules and later stop/cancel instructions still apply. '
)


def _utc_timestamp(value):
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')
    except (OverflowError, OSError, ValueError):
        # Protocol integers outside datetime's display range are still rendered
        # exactly. Normal launcher times always have an ISO UTC display.
        return str(value) + ' Unix UTC seconds'


def authorization_scope(grant):
    """Public, secret-free scope. Valid only when supplied by the actual owner."""
    grant = p.validate_grant(grant)
    limits = grant['limits']
    pairs = ', '.join(pair['model'] + '/' + pair['reasoning_effort'] for pair in grant['allowed_pairs'])
    statement = (
        'I authorize this bounded Dots2Codex v3 transport for the following fresh activation only.\n'
        f"Activation: {grant['activation_id']}\nGoogle Drive folder: {grant['folder_id']}\n"
        f"Inbox: {grant['inbox_id']}\nGrant SHA-256: {p.grant_hash(grant)}\n"
        f"Reviewed package SHA-256: {grant['package_sha256']}\n"
        f"UTC window: {_utc_timestamp(grant['created_at'])} to {_utc_timestamp(grant['expires_at'])}\n"
        f"Created at / expires at (Unix UTC seconds): {grant['created_at']} / {grant['expires_at']}\n"
        f"Exact model/effort: {pairs}\n"
        f"Limits: {limits['max_routes']} routes, {limits['max_children']} children, "
        f"{limits['max_requests_per_route']} requests per route, "
        f"{limits['max_request_bytes']} request bytes, {limits['max_result_bytes']} result bytes.\n"
        + TRANSPORT_PERMISSIONS +
        'Expiry blocks new BEGIN; only already-begun work may publish its bound result afterward. '
        'This statement and its source-message reference record my supplied scope; '
        'they are not a platform approval or evidence that every future action is permitted.'
    )
    return {'contract': CONTRACT, 'grant': grant, 'statement': statement}


def validate_join_authorization(join, authenticated_grant):
    """Compare with a grant only after its Inbox MAC has been authenticated."""
    grant = p.validate_grant(authenticated_grant)
    p.exact(join, JOIN_KEYS | {'transport_authorization'}, 'invalid_join')
    p.require(join['activation_id'] == grant['activation_id'] and join['inbox_id'] == grant['inbox_id']
              and join['grant_sha256'] == p.grant_hash(grant), 'join_grant_binding_mismatch')
    scope = authorization_scope(grant)
    p.require(p.canonical(join['transport_authorization']) == p.canonical(scope),
              'transport_authorization_binding_mismatch')
    return scope


def parse_join(raw):
    """Fail closed for old JOINs; never infer broader authority from four fields."""
    p.require(isinstance(raw, (str, bytes)), 'invalid_join')
    try:
        encoded = raw.encode('utf-8') if isinstance(raw, str) else raw
        p.require(len(encoded) <= MAX_JOIN_BYTES, 'join_too_large')
        value = encoded.decode('utf-8').strip()
    except UnicodeError:
        raise p.ProtocolError('invalid_join') from None
    p.require(value.startswith(p.JOIN_MARKER + ' '), 'fresh_v3_join_required')
    join = p.strict_json(value[len(p.JOIN_MARKER) + 1:])
    if type(join) is dict and set(join) == JOIN_KEYS:
        raise p.ProtocolError('fresh_transport_authorization_required')
    p.exact(join, JOIN_KEYS | {'transport_authorization'}, 'invalid_join')
    p.safe_id(join['activation_id']); p.safe_id(join['inbox_id'])
    p.require(p.valid_hash(join['grant_sha256']) and p.valid_hash(join['join_code']), 'invalid_join')
    scope = join['transport_authorization']
    p.exact(scope, {'contract', 'grant', 'statement'}, 'invalid_transport_authorization')
    p.require(scope['contract'] == CONTRACT, 'unsupported_transport_authorization')
    # This grant is claimed in the owner message, not authenticated yet. Reject
    # altered disclosure now, then repeat exact binding after Inbox verification.
    validate_join_authorization(join, scope['grant'])
    return copy.deepcopy(join)
