#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _repo_root_from_script() -> Path:
    script_path = Path(__file__).resolve()
    # .../<repo>/.claude/skills/redline/scripts/run_redline.py
    return script_path.parents[4]


def _ensure_repo_imports(repo_root: Path) -> None:
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)


def main(argv: list[str]) -> int:
    repo_root = _repo_root_from_script()
    _ensure_repo_imports(repo_root)
    from netdocs.env import load_dotenv_file

    load_dotenv_file(repo_root / ".env", override=True)

    adeu_bin = repo_root / ".venv" / "bin" / "adeu"
    if not adeu_bin.exists():
        print(f"adeu binary not found at {adeu_bin}", file=sys.stderr)
        print(
            "Run 'uv sync' at the repo root. The default 'redline' group installs Adeu into the shared .venv.",
            file=sys.stderr,
        )
        return 1

    cmd = [str(adeu_bin), *argv]
    return subprocess.call(cmd, cwd=str(repo_root), env=os.environ.copy())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
