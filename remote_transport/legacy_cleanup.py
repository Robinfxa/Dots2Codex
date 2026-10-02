"""Close-only readers for existing current or exact retired-v1 session evidence.

No runtime parser, admission, selection, permit or bootstrap-start API uses this
module. Original bytes, catalog identifiers, hashes and HMACs are never rewritten.
Shared private schema checks preserve every non-catalog validation. The only
possible mutations are an exact control close and a signed bootstrap close/abort.
"""
import copy
import json
import re
import secrets
import time

from . import control as c
from . import router_bootstrap as b
from .model import Object, MAX_BYTES, ProtocolError, canonical, hash_bytes, require
from .selection import ADMISSION_KEYS, _validate_existing_selection


def _validate_admission(value, selection, native_task_id):
    _validate_existing_selection(selection)
    require(isinstance(value, dict) and set(value) == ADMISSION_KEYS,
            'native_admission_receipt_required')
    expected = {'contract': 'dots-native-admission/1', 'adapter': 'collaboration.spawn_agent',
                'native_task_id': native_task_id, 'submitted_model': selection['model'],
                'submitted_reasoning_effort': selection['reasoning_effort'], 'fork_turns': 'none',
                'catalog_sha256': selection['catalog_sha256'],
                'verification': 'parent_recorded_platform_admission', 'underlying_model_verified': False}
    require(value == expected and value['underlying_model_verified'] is False,
            'native_admission_binding_mismatch')


def _validate_inference(value, native_task_id):
    require(isinstance(value, dict) and set(value) == {'selection', 'admission'},
            'invalid_inference_binding')
    _validate_admission(value['admission'], value['selection'], native_task_id)


def pin_binding(raw):
    """Validate the original envelope and return only its immutable close binding.

    In particular this does not return a runtime-valid Object or create a pin.
    """
    require(isinstance(raw, bytes) and len(raw) <= MAX_BYTES, 'object_too_large')
    pin = Object(raw)
    pin._validate(_validate_inference)
    body = pin.body
    require(body['kind'] == 'deployment', 'deployment_pin_required')
    identity, payload = body['identity'], body['payload']
    value = dict(deployment_hash=pin.oid, identity=identity, created=payload['created'],
                 expires=payload['expires'], worker_id=identity['worker_id'],
                 generation=identity['generation'], native_task_id=identity['native_task_id'],
                 journal_id=identity['worker_journal_id'])
    if 'inference' in payload:
        value['selection'] = copy.deepcopy(payload['inference']['selection'])
    return value


def _control_state(state):
    return c._validate_state(state, _validate_existing_selection)


def _control_block(state):
    _control_state(state)
    block = c.BEGIN + canonical(state, max_bytes=c.MAX_CONTROL_BYTES).decode() + c.END
    require(len(block.encode()) <= c.MAX_CONTROL_BYTES, 'control_block_too_large')
    return block


def _decode_control(block):
    require(isinstance(block, str) and block.startswith(c.BEGIN) and block.endswith(c.END)
            and block.count(c.BEGIN) == 1 and block.count(c.END) == 1, 'invalid_control_block')
    try:
        state = json.loads(block[len(c.BEGIN):-len(c.END)])
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError('invalid_control_json') from None
    require(_control_block(state) == block, 'noncanonical_control_block')
    return state


class _CloseSnapshot(c.ControlSnapshot):
    @property
    def state(self):
        return _decode_control(self.block)


class CloseOnlyControlStore(c.GoogleDocsCASControlStore):
    """Exact CAS port that cannot write any operation other than a close fence."""
    def snapshot_from_document(self, document):
        require(isinstance(document, dict) and document.get('documentId') == self.document_id,
                'control_document_mismatch')
        revision = document.get('revisionId')
        require(isinstance(revision, str) and 1 <= len(revision) <= 1024, 'editable_revision_required')
        block = c._document_text(document, self.tab_id)
        state = _decode_control(block)
        require(state['control_id'] == self.control_id and state['session_id'] == self.session_id,
                'control_pin_mismatch')
        return _CloseSnapshot(self.document_id, self.tab_id, self.writer_identity,
                              revision, block, time.monotonic())

    def plan_close(self, snapshot, binding, operation_id):
        require(isinstance(operation_id, str) and re.fullmatch('[A-Za-z0-9_-]{1,64}', operation_id),
                'invalid_operation_id')
        state = snapshot.state
        require(state['binding'] == binding, 'control_stale_worker_binding')
        require(not state.get('closed', False), 'control_session_closed')
        require(len(state['operations']) < c.MAX_OPERATIONS, 'control_operation_budget_exceeded')
        state['closed'] = True
        state['control_epoch'] += 1
        state['operations'].append({'id': operation_id, 'kind': 'close',
                                   'arguments_hash': hash_bytes(canonical({'binding': binding}))})
        _control_state(state)
        return state

    def prepare_update(self, snapshot, new_state):
        require(isinstance(snapshot, _CloseSnapshot) and snapshot.document_id == self.document_id
                and snapshot.tab_id == self.tab_id and snapshot.writer_identity == self.writer_identity,
                'snapshot_writer_mismatch')
        require(0 <= time.monotonic() - snapshot.acquired_at <= self.snapshot_ttl, 'control_snapshot_expired')
        _control_state(new_state)
        old = snapshot.state
        require(new_state['control_id'] == self.control_id and new_state['session_id'] == self.session_id,
                'control_pin_mismatch')
        expected = self.plan_close(snapshot, old['binding'], new_state['operations'][-1]['id'])
        require(new_state == expected, 'cleanup_close_only_transition_required')
        replacement = _control_block(new_state)
        request = {'replaceAllText': {'containsText': {'text': snapshot.block[:-1], 'matchCase': True,
                    'searchByRegex': False}, 'replaceText': replacement[:-1],
                    'tabsCriteria': {'tabIds': [self.tab_id]}}}
        return {'document_id': self.document_id, 'requests': [request],
                'write_control': {'requiredRevisionId': snapshot.revision_id}}


def _bootstrap_state(state):
    return b._validate_state(state, _validate_existing_selection, _validate_admission)


def _verify_bootstrap(state, join_code):
    return b._verify_join_code(state, join_code, _bootstrap_state)


def _bootstrap_block(state):
    _bootstrap_state(state)
    begin, end = ((b.SELECTED_BEGIN, b.SELECTED_END) if state['contract'] == b.SELECTED_CONTRACT
                  else (b.BOOT_BEGIN, b.BOOT_END))
    block = begin + canonical(state, max_bytes=b.MAX_BOOT_BYTES).decode() + end
    require(len(block.encode()) <= b.MAX_BOOT_BYTES, 'bootstrap_block_too_large')
    return block


def _decode_bootstrap(block):
    begin, end = ((b.SELECTED_BEGIN, b.SELECTED_END) if isinstance(block, str)
                  and block.startswith(b.SELECTED_BEGIN) else (b.BOOT_BEGIN, b.BOOT_END))
    require(isinstance(block, str) and len(block.encode()) <= b.MAX_BOOT_BYTES
            and block.startswith(begin) and block.endswith(end)
            and block.count(begin) == 1 and block.count(end) == 1, 'invalid_bootstrap_block')
    try:
        state = json.loads(block[len(begin):-len(end)])
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError('invalid_bootstrap_json') from None
    require(_bootstrap_block(state) == block, 'noncanonical_bootstrap_block')
    return state


class _BootstrapSnapshot(b.BootstrapSnapshot):
    @property
    def state(self):
        return _decode_bootstrap(self.block)


def bootstrap_snapshot(document, document_id, tab_id, *, join_code, expected_root):
    require(isinstance(document, dict) and document.get('documentId') == document_id,
            'bootstrap_document_mismatch')
    revision = document.get('revisionId')
    require(isinstance(revision, str) and revision, 'bootstrap_revision_required')
    block = c._document_text(document, tab_id)
    state = _decode_bootstrap(block)
    _verify_bootstrap(state, join_code)
    require(state['bootstrap_document_id'] == document_id and state['bootstrap_tab_id'] == tab_id,
            'bootstrap_source_identity_mismatch')
    require(b.root_context(state) == expected_root, 'bootstrap_local_root_mismatch')
    return _BootstrapSnapshot(document_id, tab_id, revision, block)


def finish_bootstrap_state(state, *, join_code, closed, now=None):
    _verify_bootstrap(state, join_code)
    require(state['stage'] not in {'CLOSED', 'ABORTED'}, 'bootstrap_already_final')
    kind = 'closed' if closed and state['stage'] == 'CONSUMED' else 'aborted'
    nxt = copy.deepcopy(state)
    nxt.update(stage='CLOSED' if kind == 'closed' else 'ABORTED', epoch=state['epoch'] + 1)
    if kind == 'aborted':
        nxt['bundle'] = None
    event = {'epoch': nxt['epoch'], 'operation_id': secrets.token_hex(16), 'kind': kind,
             'actor': 'mac', 'at': b._now(now),
             'parent_hash': hash_bytes(canonical(state['events'][-1])) if state['events'] else b.context_hash(state),
             'before_hash': hash_bytes(canonical(b._logical(state))), 'after': b._logical(nxt),
             'reason': '' if kind == 'closed' else 'mac_operator_stop'}
    event['mac'] = b.proof(join_code, 'transition', b._bound(state, event))
    nxt['events'].append(event)
    _verify_bootstrap(nxt, join_code)
    return nxt


def bootstrap_plan(snapshot, new_state, *, join_code):
    require(isinstance(snapshot, _BootstrapSnapshot), 'cleanup_bootstrap_snapshot_required')
    old = snapshot.state
    _verify_bootstrap(old, join_code)
    require(old['bootstrap_document_id'] == snapshot.document_id
            and old['bootstrap_tab_id'] == snapshot.tab_id, 'bootstrap_source_identity_mismatch')
    _verify_bootstrap(new_state, join_code)
    require(old['stage'] not in {'CLOSED', 'ABORTED'} and new_state['stage'] in {'CLOSED', 'ABORTED'}
            and new_state['epoch'] == old['epoch'] + 1 and new_state['events'][:-1] == old['events']
            and new_state['events'][-1]['kind'] in {'closed', 'aborted'}, 'cleanup_close_only_transition_required')
    expected = copy.deepcopy(old)
    expected.update(epoch=new_state['epoch'], stage=new_state['stage'], events=copy.deepcopy(new_state['events']))
    if new_state['stage'] == 'ABORTED':
        expected['bundle'] = None
    require(new_state == expected, 'cleanup_bootstrap_binding_changed')
    replacement = _bootstrap_block(new_state)
    request = {'replaceAllText': {'containsText': {'text': snapshot.block[:-1], 'matchCase': True,
                'searchByRegex': False}, 'replaceText': replacement[:-1],
                'tabsCriteria': {'tabIds': [snapshot.tab_id]}}}
    return {'contract': 'dots-router-plan/2', 'tab_id': snapshot.tab_id, 'source_block': snapshot.block,
            'expected_state': copy.deepcopy(new_state), 'operation_id': new_state['events'][-1]['operation_id'],
            'tool_arguments': {'document_id': snapshot.document_id, 'requests': [request],
                               'write_control': {'requiredRevisionId': snapshot.revision_id}}}


def verify_bootstrap_update(plan, response, readback, *, join_code):
    require(isinstance(plan, dict) and set(plan) == {'contract', 'tab_id', 'source_block',
            'expected_state', 'operation_id', 'tool_arguments'} and plan['contract'] == 'dots-router-plan/2',
            'invalid_bootstrap_plan')
    expected, args = plan['expected_state'], plan['tool_arguments']
    source = _BootstrapSnapshot(args['document_id'], plan['tab_id'],
                                args['write_control']['requiredRevisionId'], plan['source_block'])
    require(plan == bootstrap_plan(source, expected, join_code=join_code), 'bootstrap_plan_changed')
    response, readback = b._structured(response), b._structured(readback)
    if response is not None:
        require(isinstance(response, dict) and response.get('documentId') == args['document_id'],
                'bootstrap_response_document_mismatch')
        replies, wc = response.get('replies'), response.get('writeControl')
        require(type(replies) is list and len(replies) == 1 and isinstance(replies[0], dict)
                and isinstance(replies[0].get('replaceAllText'), dict)
                and type(replies[0]['replaceAllText'].get('occurrencesChanged')) is int
                and replies[0]['replaceAllText']['occurrencesChanged'] == 1, 'bootstrap_exact_replace_required')
        require(isinstance(wc, dict) and isinstance(wc.get('requiredRevisionId'), str)
                and wc['requiredRevisionId'] and wc['requiredRevisionId'] != source.revision_id,
                'bootstrap_response_revision_unverified')
    fresh = bootstrap_snapshot(readback, source.document_id, source.tab_id,
                               join_code=join_code, expected_root=b.root_context(expected))
    require(fresh.revision_id != source.revision_id and fresh.state == expected,
            'bootstrap_operation_not_observed_no_replay')
    return fresh.state
