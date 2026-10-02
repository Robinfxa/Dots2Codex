#!/usr/bin/env python3
"""Verify the release inventory and content hashes without network access."""
import hashlib
import json
from pathlib import Path
import sys
import stat

ROOT = Path(__file__).resolve().parents[1]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def verify():
    manifest = json.loads((ROOT / 'MANIFEST.json').read_text(encoding='utf-8'))
    expected = manifest['files']
    actual = {}
    for path in sorted(ROOT.rglob('*')):
        if '__pycache__' in path.parts:
            continue
        if path.is_symlink():
            raise ValueError('symbolic_links_not_allowed')
        if not path.is_file() or path.name == 'MANIFEST.json' and path.parent == ROOT:
            continue
        relative = path.relative_to(ROOT).as_posix()
        raw = path.read_bytes()
        actual[relative] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                            'mode': format(stat.S_IMODE(path.stat().st_mode), '04o')}
    if expected != actual:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(key for key in set(actual) & set(expected) if actual[key] != expected[key])
        raise ValueError(f'inventory_mismatch missing={missing} extra={extra} changed={changed}')
    digest = hashlib.sha256(canonical(actual)).hexdigest()
    if digest != manifest['tree_sha256']:
        raise ValueError('tree_hash_mismatch')
    print(f"Verified {len(actual)} files; tree SHA-256 {digest}")


if __name__ == '__main__':
    try:
        verify()
    except (OSError, ValueError, KeyError) as exc:
        print('Release verification failed: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
