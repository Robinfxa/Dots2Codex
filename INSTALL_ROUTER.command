#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd -P)"
exec "$ROOT/mac_router/launch.command" settings "$@"
