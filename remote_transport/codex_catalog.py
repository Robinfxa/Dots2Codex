"""Pair-bound model metadata for the unmodified official Codex 0.159.2 CLI.

This describes the bridge's wire adapter, not backend capabilities or entitlement.
Only the admitted pair is shown: changing it requires a new paired session.
The native capability snapshot and admission receipt remain separate authorities.

Schema and effort normalization source: openai/codex, rust-v0.159.2,
commit ff6aec96948b70d94983af2641a6b67c94faeff5:
  codex-rs/protocol/src/openai_models.rs
  codex-rs/protocol/src/openai_models/reasoning_effort.rs
The latter rewrites ultra and persistent, so neither is a bridge wire effort.
"""
import os
import stat
import tempfile
from pathlib import Path

from .backend import fsync_dir, read_private_file
from .model import canonical, hash_bytes, require
from .selection import validate_selection

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
    expected = canonical(catalog_for_selection(selection), max_bytes=MAX_CATALOG_BYTES)
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
    raw = canonical(catalog_for_selection(selection), max_bytes=MAX_CATALOG_BYTES)
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
