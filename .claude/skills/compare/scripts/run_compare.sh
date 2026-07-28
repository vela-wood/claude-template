#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
PY_BIN="$REPO_ROOT/.venv/bin/python"

if [[ ! -x "$PY_BIN" ]]; then
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    PY_BIN="$PYTHON_BIN"
  else
    echo "No interpreter at $PY_BIN — run 'uv sync' at the repo root." >&2
    echo "(Override with PYTHON_BIN=/path/to/python if you know what you're doing.)" >&2
    exit 1
  fi
fi

exec "$PY_BIN" "$SCRIPT_DIR/run_compare.py" "$@"
