#!/bin/sh
# An operator-invoked read-only help check. Never initializes or runs a tunnel.
set -eu
if ! command -v tunnel-client >/dev/null 2>&1; then
  printf '%s\n' 'tunnel-client is not present on PATH; see docs/MCP_AND_TUNNEL.md' >&2
  exit 2
fi
exec tunnel-client help quickstart
