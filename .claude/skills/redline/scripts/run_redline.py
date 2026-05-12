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
    # Import directly from the module file to avoid triggering netdocs/__init__.py
    # which has heavy dependencies (textual, asyncpg, etc.)
    import importlib.util as _ilu
    _env_spec = _ilu.spec_from_file_location("netdocs.env", repo_root / "netdocs" / "env.py")
    _env_mod = _ilu.module_from_spec(_env_spec)
    _env_spec.loader.exec_module(_env_mod)
    load_dotenv_file = _env_mod.load_dotenv_file

    load_dotenv_file(repo_root / ".env", override=True)

    adeu_bin = repo_root / ".venv" / "bin" / "adeu"
    if not adeu_bin.exists():
        # Windows venv uses Scripts/ and .exe extension
        adeu_bin = repo_root / ".venv" / "Scripts" / "adeu.exe"
    if not adeu_bin.exists():
        print("adeu binary not found in .venv/bin or .venv/Scripts", file=sys.stderr)
        print(
            "Run 'uv sync' at the repo root. The default 'redline' group installs Adeu into the shared .venv.",
            file=sys.stderr,
        )
        return 1

    cmd = [str(adeu_bin), *argv]
    return subprocess.call(cmd, cwd=str(repo_root), env=os.environ.copy())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
