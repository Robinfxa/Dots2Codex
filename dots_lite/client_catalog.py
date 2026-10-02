"""Reviewed client model catalog and exact selection. Extracted from
remote_transport/selection.py and codex_catalog.py (Dots2Codex, MIT).
Official Codex fallback instructions are kept unchanged (OpenAI Apache-2.0).
Catalog metadata is not platform admission or backend model attestation.
"""
import copy
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from .protocol import canonical, hash_bytes, require
from .private_io import fsync_dir, read_private_file

CATALOG_PATH = Path(__file__).with_name('native_capabilities.json')
CATALOG_SHA256 = '67b816ad4cbc7a86dfd9beab767534039b02b71968b6664258f88b692ccd85c9'
DEFAULT_REASONING_EFFORT = 'xhigh'
RETIRED_CATALOG_VERSION = '2026-10-01.codex-0.159.2.v1'
RETIRED_CATALOG_SHA256 = 'ae293637817c1b758d5819b5f6944ad32fd8a21bd46092dd49aeeed5dcf5d583'
RETIRED_MODELS = frozenset(('gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna', 'gpt-5.6-sol'))
RETIRED_EFFORTS = frozenset(('low', 'medium', 'high', 'xhigh', 'max'))
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
    require(effort != 'max', 'max_effort_removed_explicit_reselection_required')
    require(isinstance(effort, str) and effort in catalog['models'][model]['bridge_efforts'],
            'unsupported_model_effort_pair')
    return {'contract': 'dots-native-selection/1', 'catalog_version': catalog['version'],
            'catalog_sha256': catalog_hash(catalog), 'model': model, 'reasoning_effort': effort}


def validate_selection(value, catalog=None):
    require(isinstance(value, dict) and set(value) == SELECTION_KEYS, 'invalid_native_selection')
    expected = select(load_catalog() if catalog is None else catalog, value['model'], value['reasoning_effort'])
    require(value == expected, 'native_selection_catalog_mismatch')
    return copy.deepcopy(value)


def validate_settings_selection(value):
    """Read an existing choice for explicit Settings; never for admission."""
    return _validate_existing_selection(value)


def _validate_existing_selection(value):
    """Read current or the exact retired v1 choice for Settings or closure only.

    Returned values are unchanged, including retired max choices. This does not
    authorize routing, new admission receipts or new signed roots: those must
    continue using validate_selection and explicit current-catalog reselection.
    """
    require(isinstance(value, dict) and set(value) == SELECTION_KEYS, 'invalid_native_selection')
    if (value['catalog_version'], value['catalog_sha256']) != (RETIRED_CATALOG_VERSION, RETIRED_CATALOG_SHA256):
        return validate_selection(value)
    require(value['contract'] == 'dots-native-selection/1'
            and isinstance(value['model'], str) and value['model'] in RETIRED_MODELS
            and isinstance(value['reasoning_effort'], str) and value['reasoning_effort'] in RETIRED_EFFORTS,
            'invalid_retired_native_selection')
    return copy.deepcopy(value)


CODEX_VERSION = 'codex-cli 0.159.2'
CODEX_SOURCE_COMMIT = 'ff6aec96948b70d94983af2641a6b67c94faeff5'
INSTRUCTIONS_PATH = Path(__file__).with_name('codex_instructions_0_159_2.txt')
INSTRUCTIONS_SHA256 = 'ac8ae107a0d72fe3476b430afb161ea4e67da2e446d778aefc44828160559807'
MAX_CATALOG_BYTES = 131072


def catalog_for_selection(selection):
    """Return a fresh ModelsResponse for exactly one pinned model/effort pair.

Use the official CLI's pre-existing fallback instructions unchanged. In
particular, do not replace approvals, Guardian settings, or sandbox policy with
model-catalog overrides. The adapter supports plain text and standard Responses
tool declarations; it does not implement Responses Lite or effort updates.
    """
    selected = validate_selection(selection)
    instructions = INSTRUCTIONS_PATH.read_bytes()
    require(hash_bytes(instructions) == INSTRUCTIONS_SHA256,
            'codex_instruction_source_changed')
    effort = selected['reasoning_effort']
    return {'models': [{
        'slug': selected['model'],
        'display_name': selected['model'],
        'description': 'Dots2Codex session-pinned native admission; changing model or effort requires a new paired session.',
        'default_reasoning_level': effort,
        'supported_reasoning_levels': [{'effort': effort, 'description': 'Pinned native admission effort for this session'}],
        'shell_type': 'unified_exec',
        'visibility': 'list',
        'supported_in_api': True,
        'priority': 1,
        'availability_nux': None,
        'upgrade': None,
        'model_messages': {'instructions_template': instructions.decode('utf-8')},
        'supports_reasoning_summary_parameter': False,
        'default_reasoning_summary': 'none',
        'support_verbosity': False,
        'default_verbosity': None,
        'apply_patch_tool_type': 'freeform',
        'truncation_policy': {'mode': 'bytes', 'limit': 10000},
        'experimental_supported_tools': [],
        'input_modalities': ['text'],
        'include_apps_usage_instructions': False,
        'use_responses_lite': False,
        'supports_reasoning_effort_updates': False,
        'multi_agent_version': None,
        'multi_agent_reasoning_effort': None,
        'additional_speed_tiers': [],
        'service_tiers': [],
        'default_service_tier': None,
    }]}


def validate_catalog(path, selection):
    """Verify the private, exact catalog before handing its path to Codex."""
    path = Path(path).expanduser().absolute()
    expected = canonical(catalog_for_selection(selection))
    raw = read_private_file(path, MAX_CATALOG_BYTES)
    require(raw == expected, 'codex_catalog_selection_mismatch')
    return str(path)


def write_catalog(path, selection):
    """Create immutable private catalog bytes, or verify an identical existing file.

Never overwrite a catalog after admission, or follow a symlink. A catalog is
local launch metadata and does not perform model discovery, admission or inference.
    """
    path = Path(path).expanduser().absolute()
    parent = path.parent
    info = parent.stat()
    require(not parent.is_symlink() and stat.S_ISDIR(info.st_mode)
            and info.st_uid == os.getuid() and not info.st_mode & 0o077,
            'private_codex_catalog_directory_required')
    raw = canonical(catalog_for_selection(selection))
    fd, temporary = tempfile.mkstemp(prefix='.codex-catalog-', dir=parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            pass  # Existing bytes must match, including ownership and permissions.
        fsync_dir(parent)
    finally:
        os.unlink(temporary)
    return validate_catalog(path, selection)
