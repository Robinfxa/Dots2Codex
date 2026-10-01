#!/bin/bash
# One backend for Finder, Terminal and the optional AppleScript applet.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$ROOT"
find_python() {
  local candidate
  for candidate in "${PYTHON_BIN:-}" /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 "$(command -v python3 || true)"; do
    [ -n "$candidate" ] && [ -x "$candidate" ] || continue
    # macOS's developer-tools shim can launch an installer. Do not invoke it.
    if [ "$(uname -s)" = Darwin ] && [ "$candidate" = /usr/bin/python3 ] && ! /usr/bin/xcode-select -p >/dev/null 2>&1; then continue; fi
    if "$candidate" -I -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"; return 0
    fi
  done
  return 1
}
PYTHON="$(find_python)" || {
  printf '%s\n' 'Python 3.11+ is required. Official macOS instructions: https://www.python.org/downloads/macos/' 'No software will be installed automatically.'
  if [ -t 0 ]; then
    read -r -p 'Path to an existing Python 3.11+ executable (blank cancels): ' PYTHON || exit 130
    [ -n "$PYTHON" ] && [ -x "$PYTHON" ] || exit 130
    "$PYTHON" -I -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' || exit 1
  else
    exit 1
  fi
}
unset PYTHONPATH PYTHONHOME
exec "$PYTHON" -B -m remote_transport.mac_launcher "$@"
