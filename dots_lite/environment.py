# Extracted from remote_transport/mac_environment.py (Dots2Codex, MIT).
"""Versioned private venv installer. Requires explicit UI approval before mutations.

Candidates are created at their final unique paths and never renamed: generated
entry point shebangs retain valid absolute paths. Only a small JSON pointer is
atomically replaced after all health checks pass. Failed candidates are retained.
"""
from __future__ import annotations
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid
from .private_io import read_private_file, fsync_dir, no_symlinks, private_dir, private_lock, save as _save
private_directory = private_dir
from .ui import Cancelled
from .protocol import require


IMPORTS = ['jsonschema', 'googleapiclient.discovery', 'google.auth', 'google_auth_httplib2',
           'httplib2', 'requests', 'google_auth_oauthlib']


def pinned_requirements(root, *, include_global=True):
    root = Path(root)
    result = {}; visiting = set()
    allowed = {'requirements-router-mac.txt', 'requirements-remote.txt',
               'requirements-google-example.txt'}
    if include_global: allowed.add('requirements-global.txt')
    def read(name):
        require(name in allowed, 'launcher_unexpected_requirements_file')
        require(name not in visiting, 'launcher_recursive_requirements')
        visiting.add(name)
        for line in (root / name).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'): continue
            if line.startswith('-r '): read(line[3:].strip()); continue
            match = re.fullmatch(r'([A-Za-z0-9_-]+)==([A-Za-z0-9.]+)', line)
            require(match is not None, 'launcher_unpinned_dependency')
            key, version = match.groups()
            require(key not in result or result[key] == version, 'launcher_conflicting_dependency')
            result[key] = version
        visiting.remove(name)
    read('requirements-router-mac.txt')
    if include_global: read('requirements-global.txt')
    return result


def clean_env():
    env = {k:v for k,v in os.environ.items() if not k.startswith(('PIP_', 'PYTHON'))}
    env.update(PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK='1', PYTHONNOUSERSITE='1')
    return env




class Environment:
    def __init__(self, root, state, *, run=subprocess.run, python=None, include_global=True):
        self.root, self.state = Path(root), no_symlinks(state)
        self.run = run
        self.python = str(python or sys.executable)
        self.include_global = include_global
        self.pins = pinned_requirements(self.root, include_global=include_global)
        self.imports = IMPORTS + (['tomlkit'] if include_global else [])
        self.fingerprint = hashlib.sha256(json.dumps({'pins':self.pins,
            'python':list(sys.version_info[:2]), 'contract':1}, sort_keys=True).encode()).hexdigest()
        # Keep the established single-session environment usable after Global
        # setup; changing modes must not trigger a replacement installation.
        self.pointer = self.state / 'environment-v3.json'

    def _run(self, args, timeout=60):
        return self.run(args, capture_output=True, text=True, timeout=timeout,
                        check=False, env=clean_env(), cwd=str(self.root))

    def health(self, path):
        try:
            path = private_directory(path)
            python = path / 'bin/python3'
            if not python.is_file() or not os.access(python, os.X_OK): return False
            # Interpreter symlinks are standard venv behavior; directory/pointer
            # symlinks are rejected. Prefix and pyvenv.cfg prove the environment.
            if not (path / 'pyvenv.cfg').is_file() or (path / 'pyvenv.cfg').is_symlink(): return False
            script = ('import sys,json,importlib,importlib.metadata as m; '
                      'assert sys.version_info >= (3,11); '
                      'assert sys.prefix != sys.base_prefix; '
                      'assert sys.prefix == sys.argv[1]; '
                      'pins=json.loads(sys.argv[2]); '
                      'assert all(m.version(k)==v for k,v in pins.items()); '
                      '[importlib.import_module(k) for k in json.loads(sys.argv[3])]; print("healthy")')
            result = self._run([str(python), '-I', '-B', '-c', script, str(path), json.dumps(self.pins), json.dumps(self.imports)])
            if result.returncode or result.stdout.strip() != 'healthy': return False
            return self._run([str(python), '-I', '-m', 'pip', 'check']).returncode == 0
        except Exception: return False

    def current(self):
        if not self.pointer.exists() and not self.pointer.is_symlink(): return None
        try:
            private_directory(self.state)
            value = json.loads(read_private_file(self.pointer, 32768))
            require(set(value) == {'contract','path','fingerprint'} and value['contract'] == 1,
                    'launcher_bad_environment_pointer')
            path = no_symlinks(value['path'])
            require(path.parent == self.state / 'environments', 'launcher_environment_path_escape')
            if value['fingerprint'] == self.fingerprint and self.health(path): return path
        except Exception:
            # Unsafe pointer files must never be overwritten by an install attempt.
            if self.pointer.is_symlink(): raise RuntimeError('launcher_unsafe_environment_pointer') from None
            try: read_private_file(self.pointer, 32768)
            except Exception: raise RuntimeError('launcher_unsafe_environment_pointer') from None
        return None

    def ensure(self, ui):
        require(sys.version_info >= (3,11), 'launcher_python_311_required')
        current = self.current()
        if current: return current / 'bin/python3'
        dependencies = 'lightweight v3 dependencies (including tomlkit==0.13.3)' if self.include_global else 'Router dependencies'
        if not ui.confirm(f'Create a private Python environment and download the pinned {dependencies} from https://pypi.org?\n'
            f'Location: {self.state / "environments"}\nNo global Python or Codex changes. Existing environments and session evidence are kept.'):
            raise Cancelled()
        private_directory(self.state, create=True)
        with private_lock(self.state / 'install.lock'):
            current = self.current()
            if current: return current / 'bin/python3'
            base = private_directory(self.state / 'environments', create=True)
            candidate = base / ('py' + str(sys.version_info.major) + str(sys.version_info.minor) + '-' + self.fingerprint[:12] + '-' + uuid.uuid4().hex[:12])
            candidate.mkdir(mode=0o700)
            # No secrets, pip output or user URLs are persisted to state.
            _save(candidate / 'install-state.json', {'stage':'creating','fingerprint':self.fingerprint})
            try:
                print('[launcher] Creating the approved private environment...', flush=True)
                require(self._run([self.python, '-I', '-m', 'venv', str(candidate)], timeout=120).returncode == 0,
                        'launcher_venv_creation_failed')
                python = candidate / 'bin/python3'
                print('[launcher] Installing pinned dependencies from PyPI; no global changes...', flush=True)
                result = self._run([str(python), '-I', '-m', 'pip', '--isolated', 'install',
                    '--index-url', 'https://pypi.org/simple', '--disable-pip-version-check', '--no-input',
                    '--retries', '0', '--timeout', '30', *[f'{k}=={v}' for k,v in sorted(self.pins.items())]], timeout=600)
                require(result.returncode == 0, 'launcher_dependency_install_failed')
                print('[launcher] Verifying interpreter, imports, pinned versions and pip check...', flush=True)
                require(self.health(candidate), 'launcher_environment_health_failed')
                _save(candidate / 'install-state.json', {'stage':'healthy','fingerprint':self.fingerprint})
                _save(self.pointer, {'contract':1,'path':str(candidate),'fingerprint':self.fingerprint})
                return python
            except BaseException:
                _save(candidate / 'install-state.json', {'stage':'incomplete','fingerprint':self.fingerprint})
                raise
