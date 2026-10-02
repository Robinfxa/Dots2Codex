#!/bin/sh
# Run only when explicitly invoked. No installs, downloads, profile writes or daemon.
set -eu
# The tunnel credential must not reach the Python process.
unset CONTROL_PLANE_API_KEY
export PYTHONDONTWRITEBYTECODE=1
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-python3}
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  printf '%s\n' 'bridge_mcp: python_missing' >&2
  exit 2
fi
cd "$ROOT"
exec "$PYTHON_BIN" -m mcp_adapter "$@"
