#!/bin/sh
# Finder/Terminal entry point. Guided global service launcher. First use requires local review.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONDONTWRITEBYTECODE=1
# Finder has a smaller PATH than an interactive shell. Check known locations too.
if [ -n "${PYTHON_BIN:-}" ]; then
  if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
    printf '%s\n' 'DIRECT needs Python 3.11 or newer at PYTHON_BIN.' >&2
    exit 2
  fi
else
  PYTHON_BIN=
  for candidate in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
      PYTHON_BIN=$candidate
      break
    fi
  done
  if [ -z "$PYTHON_BIN" ]; then
    printf '%s\n' 'DIRECT needs Python 3.11 or newer. Install Python from https://www.python.org/downloads/macos/ and reopen DIRECT.command.' >&2
    if [ -t 0 ]; then printf 'Press Return to close: '; read -r ignored || :; fi
    exit 2
  fi
fi
exec "$PYTHON_BIN" "$ROOT/direct_bridge/global_launcher.py" "$@"
