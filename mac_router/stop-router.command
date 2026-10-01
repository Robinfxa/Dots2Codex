#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
"$ROOT/.venv-router/bin/python3" -m remote_transport.router_mac stop "$@"
echo "Inspect closed, process_stopped and bootstrap_cleanup above. Evidence is preserved."
read -r -p "Press Enter to close..." _ || true
