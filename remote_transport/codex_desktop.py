"""Read-only signed macOS app discovery; no internal CLI path or launch required.

Info.plist identifies an installation, not its engine/catalog compatibility.
No app code runs during discovery. Only fixed Apple metadata/signature tools run.
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys

from .mac_setup import no_symlinks
from .model import ProtocolError, require

APP_ID = 'com.openai.codex'
# OpenAI Developer ID observed in the upstream macOS app report #19041.
# This is a fail-closed identity pin, not a compatibility/version allowlist.
TEAM_ID = '2DC432GLL2'
CONTRACT = 'dots-desktop-app-identity/1'
TRIAL = 'desktop-app-config-trial/1'
REQUIREMENT = f'anchor apple generic and identifier "{APP_ID}" and certificate leaf[subject.OU] = "{TEAM_ID}"'
MAX_METADATA = 2 * 1024 * 1024


def _file(path, *, maximum=256 * 1024 * 1024, executable=False):
    path = no_symlinks(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_uid in (0, os.getuid())
                and not before.st_mode & 0o022 and before.st_size <= maximum,
                'desktop_app_unsafe_file')
        require(not executable or os.access(path, os.X_OK), 'desktop_app_executable_missing')
        digest = hashlib.sha256(); chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk: break
            digest.update(chunk)
            if not executable: chunks.append(chunk)
        after = os.fstat(fd)
        identity = lambda st: [st.st_dev, st.st_ino, st.st_uid, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns]
        require(identity(before) == identity(after), 'desktop_app_changed_during_read')
        return {'path': str(path), 'sha256': digest.hexdigest(), 'identity': identity(after)}, b''.join(chunks)
    finally:
        os.close(fd)


def application_evidence(path, *, run=None, platform=None):
    require((sys.platform if platform is None else platform) == 'darwin', 'desktop_app_requires_macos')
    run = subprocess.run if run is None else run
    path = no_symlinks(path)
    require(path.suffix == '.app' and path.is_dir(), 'desktop_app_bundle_required')
    info, raw = _file(path / 'Contents' / 'Info.plist', maximum=MAX_METADATA)
    try: value = plistlib.loads(raw)
    except Exception: raise ProtocolError('desktop_app_invalid_metadata') from None
    require(isinstance(value, dict) and value.get('CFBundleIdentifier') == APP_ID, 'desktop_app_identity_mismatch')
    executable = value.get('CFBundleExecutable')
    require(isinstance(executable, str) and re.fullmatch(r'[A-Za-z0-9_. -]{1,128}', executable)
            and executable not in ('.', '..'), 'desktop_app_invalid_executable')
    version = value.get('CFBundleShortVersionString') or value.get('CFBundleVersion')
    require(isinstance(version, str) and re.fullmatch(r'[0-9][A-Za-z0-9.+_-]{0,127}', version),
            'desktop_app_version_unavailable')
    main, _ = _file(path / 'Contents' / 'MacOS' / executable, executable=True)
    signature, _ = _file(path / 'Contents' / '_CodeSignature' / 'CodeResources', maximum=16 * 1024 * 1024)
    try:
        result = run(['/usr/bin/codesign', '--verify', '--deep', '--strict', '-R', REQUIREMENT, str(path)],
                     capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        raise ProtocolError('desktop_app_signature_check_unavailable') from None
    require(result.returncode == 0, 'desktop_app_signature_not_verified')
    # Re-read after signature verification; do not mix two app update states.
    require(_file(path / 'Contents' / 'Info.plist', maximum=MAX_METADATA)[0] == info
            and _file(path / 'Contents' / 'MacOS' / executable, executable=True)[0] == main
            and _file(path / 'Contents' / '_CodeSignature' / 'CodeResources', maximum=16 * 1024 * 1024)[0] == signature,
            'desktop_app_changed_during_read')
    return {'contract': CONTRACT, 'path': str(path), 'bundle_id': APP_ID, 'team_id': TEAM_ID,
            'app_version': version, 'info': info, 'executable': main, 'signature': signature,
            'engine_version': None, 'compatibility_verified': False}


def check_application(evidence, **kwargs):
    require(isinstance(evidence, dict) and evidence.get('contract') == CONTRACT, 'desktop_app_evidence_required')
    require(application_evidence(evidence['path'], **kwargs) == evidence, 'desktop_app_evidence_changed')
    return evidence


def _registered_paths(run):
    # NSWorkspace queries LaunchServices without launching or scripting Codex.
    script = ('ObjC.import("AppKit"); var u=$.NSWorkspace.sharedWorkspace.'
              'URLForApplicationWithBundleIdentifier("' + APP_ID + '"); '
              'if (u) { ObjC.unwrap(u.path); } else { ""; }')
    commands = [(['/usr/bin/osascript', '-l', 'JavaScript', '-e', script], '\n'),
                (['/usr/bin/mdfind', '-0', f'kMDItemCFBundleIdentifier == "{APP_ID}"'], '\0')]
    paths = []
    for command, separator in commands:
        try:
            result = run(command, capture_output=True, text=True, timeout=10, check=False)
            if result.returncode or len(result.stdout) > 131072: continue
            for value in result.stdout.split(separator)[:32]:
                if value and Path(value).is_absolute() and Path(value).suffix == '.app': paths.append(Path(value))
        except (OSError, subprocess.SubprocessError): continue
    return paths


def discover_application(*, explicit=None, home=None, ui=None, run=None, platform=None):
    require((sys.platform if platform is None else platform) == 'darwin', 'desktop_app_requires_macos')
    run = subprocess.run if run is None else run
    if explicit is not None:
        require(Path(explicit).expanduser().is_absolute(), 'desktop_app_path_must_be_absolute')
        return application_evidence(explicit, run=run, platform=platform)
    home = Path.home() if home is None else Path(home)
    candidates = _registered_paths(run)
    candidates += [root / name for root in (Path('/Applications'), home / 'Applications')
                   for name in ('Codex.app', 'ChatGPT.app')]
    found = []; seen = set(); rejected = False
    for path in candidates:
        if str(path) in seen: continue
        seen.add(str(path))
        if not path.exists() and not path.is_symlink(): continue
        try: evidence = application_evidence(path, run=run, platform=platform)
        except (OSError, ProtocolError): rejected = True; continue
        found.append(evidence)
    if not found:
        if ui is not None:
            ui.notify('No verified Codex desktop app was found. Open your installed Codex/ChatGPT app once, then retry. '
                'For an unregistered custom installation, use the advanced --desktop-app option with the .app folder. '
                'No internal executable is needed. Global configuration has not been changed.')
        raise ProtocolError('desktop_app_not_verified' if rejected else 'desktop_app_not_found')
    if len(found) == 1: return found[0]
    require(ui is not None, 'desktop_app_multiple_installations_choose_app')
    choices = [item['path'] + ' (app ' + item['app_version'] + ')' for item in found]
    selected = ui.choose('Multiple verified Codex desktop apps were found. Choose the app you use.', choices)
    require(selected in choices, 'desktop_app_selection_invalid')
    result = found[choices.index(selected)]
    return check_application(result, run=run, platform=platform)
