#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _repo_root_from_script() -> Path:
    script_path = Path(__file__).resolve()
    # .../<repo>/.claude/skills/caption-cli/scripts/run_caption.py
    return script_path.parents[4]


def _python_bin_for_repo(repo_root: Path) -> Path:
    repo_venv_python = repo_root / ".venv" / "bin" / "python"
    if repo_venv_python.exists():
        return repo_venv_python
    return Path(sys.executable)


def _has_env_override(argv: list[str]) -> bool:
    for arg in argv:
        if arg == "--env-file" or arg.startswith("--env-file="):
            return True
    return False


def main(argv: list[str]) -> int:
    repo_root = _repo_root_from_script()
    caption_entry = repo_root / "caption-cli" / "caption.py"
    env_file = repo_root / ".env"

    if not caption_entry.exists():
        print(f"caption entrypoint not found: {caption_entry}", file=sys.stderr)
        return 1

    caption_args = list(argv)
    if not _has_env_override(caption_args):
        caption_args = ["--env-file", str(env_file), *caption_args]

    cmd = [
        str(_python_bin_for_repo(repo_root)),
        str(caption_entry),
        *caption_args,
    ]
    child_env = os.environ.copy()
    # Ensure caption-cli package imports work when launched from outside caption-cli/.
    current_pythonpath = child_env.get("PYTHONPATH", "")
    caption_module_path = str((repo_root / "caption-cli").resolve())
    child_env["PYTHONPATH"] = (
        caption_module_path if not current_pythonpath else f"{caption_module_path}:{current_pythonpath}"
    )
    return subprocess.call(cmd, cwd=str(repo_root), env=child_env)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
