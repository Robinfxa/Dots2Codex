#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ ! -x .venv-router/bin/python3 ]; then
  echo "Router environment is not installed. Run mac_router/install.command first."
  read -r -p "Press Enter to close..." _ || true
  exit 1
fi
exec "$ROOT/.venv-router/bin/python3" -m remote_transport.router_mac start --launch-codex "$@"
