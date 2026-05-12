#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
PY_BIN="$REPO_ROOT/.venv/bin/python"

# Windows venv uses Scripts/ instead of bin/
if [[ ! -x "$PY_BIN" ]] && [[ -f "$REPO_ROOT/.venv/Scripts/python.exe" ]]; then
  PY_BIN="$REPO_ROOT/.venv/Scripts/python.exe"
elif [[ ! -x "$PY_BIN" ]]; then
  PY_BIN="${PYTHON_BIN:-python3}"
fi

exec "$PY_BIN" "$SCRIPT_DIR/run_redline.py" "$@"
