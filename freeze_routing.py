"""Freeze implementation sources before a bounded live validation run."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
files = {}
for path in sorted(p for p in ROOT.rglob('*') if p.is_file() and p.suffix in {'.py','.js'}):
    if any(part in ('runtime', 'runtimes', 'router-sessions', 'router-join-state', 'router-joins', 'runs', 'evidence', '__pycache__', 'audit',
                    '.venv', '.venv-router', 'venv', '.git', '.codex', 'release', 'dist')
           for part in path.relative_to(ROOT).parts):
        continue
    files[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
aggregate = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
out = dict(contract='dots-sticky-routing/1', files=files, aggregate_sha256=aggregate,
           scope='Implementation/test source only. Runtime, native admissions, credentials and live prompts excluded.')
(ROOT / 'evidence').mkdir(mode=0o700, exist_ok=True)
(ROOT / 'evidence/routing-source-freeze.json').write_text(json.dumps(out, indent=2) + '\n')
print(json.dumps(dict(files=len(files), aggregate_sha256=aggregate)))
