"""One CLI process's Direct provider overrides, using the shared service.

This module never writes Codex configuration or starts an archived trial route.
The bearer is passed only through Credentials.codex_env, never command arguments.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from dots_lite.protocol import require

CODEX_VERSION = '0.159.2'
BEARER_NAME = 'DOTS_BRIDGE_HTTP_BEARER'


def existing_codex():
    explicit = os.environ.get('CODEX_BIN')
    candidates = [explicit] if explicit else [shutil.which('codex'),
        '/Applications/ChatGPT.app/Contents/Resources/codex',
        '/Applications/Codex.app/Contents/Resources/codex']
    binary = next((str(Path(path).expanduser().absolute()) for path in candidates
                   if path and Path(path).expanduser().is_file()
                   and os.access(Path(path).expanduser(), os.X_OK)), None)
    require(binary is not None, 'existing_codex_cli_missing')
    return binary


def preflight(ui):
    """Resolve client/project before starting anything or asking for credentials."""
    from direct_bridge.global_credentials import safe_child_env
    binary = existing_codex()
    result = subprocess.run([binary, '--version'], env=safe_child_env(os.environ),
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    require(result.returncode == 0 and result.stdout.strip() == 'codex-cli ' + CODEX_VERSION,
            'session_requires_codex_cli_0_159_2')
    project = Path(ui.text('本次 Direct CLI 的项目目录（输入 0 取消）', str(Path.home()))).expanduser()
    if str(project) == '0':
        from direct_bridge.global_launcher import Cancelled
        raise Cancelled()
    require(project.is_absolute() and project.is_dir(), 'existing_project_directory_required')
    return binary, project


def codex_argv(binary, project, info, session_id):
    from direct_bridge.global_config import _validated_info
    selected, catalog = _validated_info(info)
    require(re.fullmatch(r'[0-9a-f]{32}', session_id or ''), 'invalid_session_identity')
    provider = 'dots2codex_direct_session_' + session_id
    # One complete, unique provider table prevents inheriting headers or auth
    # from a fixed global provider name. JSON strings are valid TOML strings;
    # object separators must be TOML '=' rather than JSON ':'.
    table = {'name': 'Dots2Codex Direct session', 'base_url': info['base_url'],
             'env_key': BEARER_NAME, 'wire_api': 'responses',
             'requires_openai_auth': False, 'supports_websockets': False,
             'request_max_retries': 0, 'stream_max_retries': 0}
    encode = lambda value: json.dumps(value, ensure_ascii=False)
    overrides = [('model_provider', encode(provider)),
                 ('model', encode(selected['model'])),
                 ('model_reasoning_effort', encode(selected['reasoning_effort'])),
                 ('model_catalog_json', encode(catalog)),
                 ('model_providers.' + provider,
                  '{' + ', '.join(key + '=' + encode(value) for key, value in table.items()) + '}')]
    argv = [str(binary), '--no-daemon', '-C', str(project)]
    for key, value in overrides:
        argv.extend(['-c', key + '=' + value])
    return argv
