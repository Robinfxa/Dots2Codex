"""Explicit native admission selection. This is a reviewed capability snapshot,
not a live entitlement probe or independently observed backend model identity.

The official Codex 0.159.2 CLI rewrites ``ultra`` before sending Responses, so
that native effort is intentionally NOT a selectable bridge wire effort.
"""
import copy
import json
import re
from pathlib import Path
from .model import canonical, hash_bytes, require

CATALOG_PATH = Path(__file__).with_name('native_capabilities.json')
CATALOG_SHA256 = 'ae293637817c1b758d5819b5f6944ad32fd8a21bd46092dd49aeeed5dcf5d583'
LEGACY_MODEL = 'native-subagent-bridge'
SELECTION_KEYS = {'contract', 'catalog_version', 'catalog_sha256', 'model', 'reasoning_effort'}
ADMISSION_KEYS = {'contract', 'adapter', 'native_task_id', 'submitted_model',
                  'submitted_reasoning_effort', 'fork_turns', 'catalog_sha256',
                  'verification', 'underlying_model_verified'}


def load_catalog(path=None):
    bundled = json.loads(CATALOG_PATH.read_bytes())
    require(hash_bytes(canonical(bundled)) == CATALOG_SHA256, 'bundled_capability_catalog_changed')
    value = bundled if path is None else json.loads(Path(path).read_bytes())
    require(value == bundled, 'capability_catalog_not_supported_by_this_release')
    require(value['contract'] == 'dots-native-capabilities/1', 'invalid_capability_catalog')
    return copy.deepcopy(value)


def catalog_hash(catalog=None):
    return hash_bytes(canonical(load_catalog() if catalog is None else catalog))


def select(catalog, model, effort):
    require(catalog == load_catalog(), 'capability_catalog_not_supported_by_this_release')
    require(isinstance(model, str) and model in catalog['models'], 'unsupported_native_model')
    require(isinstance(effort, str) and effort in catalog['models'][model]['bridge_efforts'],
            'unsupported_model_effort_pair')
    return {'contract': 'dots-native-selection/1', 'catalog_version': catalog['version'],
            'catalog_sha256': catalog_hash(catalog), 'model': model, 'reasoning_effort': effort}


def validate_selection(value, catalog=None):
    require(isinstance(value, dict) and set(value) == SELECTION_KEYS, 'invalid_native_selection')
    expected = select(load_catalog() if catalog is None else catalog, value['model'], value['reasoning_effort'])
    require(value == expected, 'native_selection_catalog_mismatch')
    return copy.deepcopy(value)


def spawn_arguments(selection, task_name, message):
    """Arguments for the TRUSTED PARENT's real native tool, never a Python spawn."""
    validate_selection(selection)
    require(isinstance(task_name, str) and re.fullmatch('[a-z0-9_]{1,100}', task_name),
            'invalid_native_task_name')
    require(isinstance(message, str) and 1 <= len(message) <= 128 * 1024, 'native_task_message_required')
    return {'task_name': task_name, 'message': message, 'fork_turns': 'none',
            'model': selection['model'], 'reasoning_effort': selection['reasoning_effort']}


def admission_receipt(selection, submitted_arguments, returned_task_name):
    """Record submitted arguments after parent observes successful native admission.

    Callers must supply the actual tool call and returned task_name. The receipt
    is parent-recorded evidence, not platform attestation or model telemetry.
    """
    validate_selection(selection)
    require(isinstance(submitted_arguments, dict), 'native_admission_arguments_required')
    required = spawn_arguments(selection, submitted_arguments.get('task_name'), submitted_arguments.get('message'))
    require(submitted_arguments == required, 'native_admission_arguments_mismatch')
    require(isinstance(returned_task_name, str) and re.fullmatch(r'/[A-Za-z0-9_./-]{1,255}', returned_task_name)
            and returned_task_name.rsplit('/', 1)[-1] == required['task_name'], 'native_admission_result_mismatch')
    receipt = {'contract': 'dots-native-admission/1', 'adapter': 'collaboration.spawn_agent',
               'native_task_id': returned_task_name, 'submitted_model': selection['model'],
               'submitted_reasoning_effort': selection['reasoning_effort'], 'fork_turns': 'none',
               'catalog_sha256': selection['catalog_sha256'],
               'verification': 'parent_recorded_platform_admission', 'underlying_model_verified': False}
    return receipt


def validate_admission(value, selection, native_task_id):
    validate_selection(selection)
    require(isinstance(value, dict) and set(value) == ADMISSION_KEYS, 'native_admission_receipt_required')
    expected = {'contract': 'dots-native-admission/1', 'adapter': 'collaboration.spawn_agent',
                'native_task_id': native_task_id, 'submitted_model': selection['model'],
                'submitted_reasoning_effort': selection['reasoning_effort'], 'fork_turns': 'none',
                'catalog_sha256': selection['catalog_sha256'],
                'verification': 'parent_recorded_platform_admission', 'underlying_model_verified': False}
    require(value == expected and value['underlying_model_verified'] is False,
            'native_admission_binding_mismatch')
    return copy.deepcopy(value)


def validate_inference(value, native_task_id):
    require(isinstance(value, dict) and set(value) == {'selection', 'admission'}, 'invalid_inference_binding')
    validate_admission(value['admission'], value['selection'], native_task_id)
    return copy.deepcopy(value)


def pin_selection(pin):
    inference = pin._body['payload'].get('inference')
    return None if inference is None else validate_inference(inference, pin._body['identity']['native_task_id'])['selection']


def validate_request_selection(request, selection=None):
    require(isinstance(request, dict), 'invalid_request')
    if selection is None:
        require(request.get('model') == LEGACY_MODEL, 'legacy_pin_requires_native_subagent_bridge')
        return
    validate_selection(selection)
    require(request.get('model') == selection['model'], 'model_change_requires_new_paired_session')
    reasoning = request.get('reasoning')
    require(isinstance(reasoning, dict) and isinstance(reasoning.get('effort'), str),
            'explicit_reasoning_effort_required')
    require(reasoning['effort'] == selection['reasoning_effort'], 'effort_change_requires_new_paired_session')
    require('reasoning_effort' not in request and 'model_reasoning_effort' not in request,
            'ambiguous_reasoning_effort')
