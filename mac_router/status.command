#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
"$ROOT/.venv-router/bin/python3" -m remote_transport.router_mac status "$@"
read -r -p "Press Enter to close..." _ || true
