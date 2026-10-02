"""Noncircular byte identity for the separately reviewed v3 package.

This checksum is an integrity binding, not a publisher signature. Manifest
sources exclude the manifest itself, state, caches and generated runtime files.
"""
from pathlib import Path
from .protocol import canonical, hash_bytes, require, strict_json
from .private_io import no_symlinks

CONTRACT = 'dots-lite-package/3'
MANIFEST = 'LIGHTWEIGHT_PACKAGE_MANIFEST.json'


def package_identity(root):
    root = no_symlinks(root)
    manifest = strict_json(no_symlinks(root / MANIFEST).read_bytes())
    require(type(manifest) is dict and set(manifest) == {'contract', 'files'}
            and manifest['contract'] == CONTRACT, 'invalid_lightweight_manifest')
    entries = manifest['files']
    require(type(entries) is list and entries, 'empty_lightweight_manifest')
    seen = set()
    for entry in entries:
        require(type(entry) is dict and set(entry) == {'path', 'sha256'}, 'invalid_lightweight_manifest_entry')
        path = Path(entry['path'])
        require(not path.is_absolute() and '..' not in path.parts and str(path) == entry['path']
                and str(path) != MANIFEST and str(path) not in seen, 'invalid_lightweight_package_path')
        seen.add(str(path))
        source = no_symlinks(root / path)
        require(source.is_file() and hash_bytes(source.read_bytes()) == entry['sha256'],
                'lightweight_package_integrity_failed')
    require('dots_lite/launcher.py' in seen and 'dots_lite/protocol.py' in seen
            and 'LIGHTWEIGHT.command' in seen, 'incomplete_lightweight_manifest')
    return hash_bytes(canonical(manifest))
