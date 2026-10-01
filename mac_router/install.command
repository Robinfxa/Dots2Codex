#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! "$PYTHON_BIN" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3,11) else 1)
PY
then
  echo "Python 3.11+ is required."
  exit 1
fi
if [ ! -d .venv-router ]; then
  "$PYTHON_BIN" -m venv .venv-router
fi
"$ROOT/.venv-router/bin/python3" -m pip install -r requirements-router-mac.txt
"$ROOT/.venv-router/bin/python3" -m remote_transport.router_mac configure "$@"
echo
echo "Installed. Next time double-click mac_router/start-router.command."
read -r -p "Press Enter to close..." _ || true
