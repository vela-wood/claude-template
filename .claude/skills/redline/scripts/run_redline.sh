#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
ADEU_BIN="$REPO_ROOT/.venv/bin/adeu"

if [[ ! -x "$ADEU_BIN" ]]; then
  echo "adeu binary not found at $ADEU_BIN" >&2
  echo "Run 'uv sync' at the repo root to install Adeu into .venv." >&2
  exit 1
fi

exec "$ADEU_BIN" "$@"
