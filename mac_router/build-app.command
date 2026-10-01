#!/bin/bash
# Must be run on macOS; Linux can inspect templates, not compile an applet.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
if [ "$(uname -s)" != Darwin ] || [ ! -x /usr/bin/osacompile ]; then
  echo 'Router.app requires macOS osacompile. No app was built; Mac launch remains unverified.'
  exit 2
fi
OUT="${1:-$ROOT/dist/Router.app}"
if [ -e "$OUT" ]; then echo 'Output already exists. Choose a new output path; nothing overwritten.'; exit 1; fi
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -I -c 'import sys; assert sys.version_info >= (3,11)'
mkdir -p "$(dirname "$OUT")"
/usr/bin/osacompile -o "$OUT" "$ROOT/mac_router/Router.applescript"
# Copy exactly the verified public manifest. Never bundle a venv, state or token.
"$PYTHON_BIN" -I - "$ROOT" "$OUT/Contents/Resources/Dots2Codex" <<'PY'
import hashlib,json,pathlib,shutil,sys
root,dest=map(pathlib.Path,sys.argv[1:])
manifest=json.loads((root/'ROUTER_PACKAGE_MANIFEST.json').read_bytes())
for item in manifest['files']:
    rel=pathlib.Path(item['path'])
    assert not rel.is_absolute() and '..' not in rel.parts
    source=root/rel
    assert not source.is_symlink() and hashlib.sha256(source.read_bytes()).hexdigest()==item['sha256']
    target=dest/rel;target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(source,target)
shutil.copy2(root/'ROUTER_PACKAGE_MANIFEST.json',dest/'ROUTER_PACKAGE_MANIFEST.json')
PY
printf '%s\n' "Built unsigned applet: $OUT" 'Not notarized. Finder, Gatekeeper, Terminal and native dialogs still require real Mac acceptance.' 'Do not bypass macOS security warnings. No authentication or launch was performed.'
