#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

CAPTION_OUTPUT_DIRNAME = "caption_cache"
TOKEN_HEAVY_COMMANDS = {"list_projects", "list_folders", "dl_transcript"}
GLOBAL_FLAGS_WITH_VALUE = {"--cache-path", "--output", "--env-file"}
FIXED_OUTPUT_FILENAMES = {
    "list_projects": "list_projects.out",
    "list_folders": "list_folders.out",
}


def _repo_root_from_script() -> Path:
    script_path = Path(__file__).resolve()
    # .../<repo>/.claude/skills/caption/scripts/run_caption.py
    return script_path.parents[4]


def _ensure_repo_imports(repo_root: Path) -> None:
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)


def _caption_bin_for_repo(repo_root: Path) -> Path:
    return repo_root / ".venv" / "bin" / "caption"


def _has_env_override(argv: list[str]) -> bool:
    for arg in argv:
        if arg == "--env-file" or arg.startswith("--env-file="):
            return True
    return False


def _env_file_from_argv(argv: list[str], repo_root: Path) -> Path:
    for index, arg in enumerate(argv):
        if arg == "--env-file" and index + 1 < len(argv):
            return Path(argv[index + 1]).expanduser()
        if arg.startswith("--env-file="):
            return Path(arg.split("=", 1)[1]).expanduser()
    return repo_root / ".env"


def _resolve_command(argv: list[str]) -> tuple[str | None, int | None]:
    index = 0
    while index < len(argv):
        arg = argv[index]
        if any(arg.startswith(f"{flag}=") for flag in GLOBAL_FLAGS_WITH_VALUE):
            index += 1
            continue
        if arg in GLOBAL_FLAGS_WITH_VALUE:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return arg, index
    return None, None


def _sanitize_filename_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", value)
    return sanitized.strip("._-") or "transcript"


def _extract_transcript_id(argv: list[str], command_index: int) -> str | None:
    index = command_index + 1
    while index < len(argv):
        arg = argv[index]
        if any(arg.startswith(f"{flag}=") for flag in GLOBAL_FLAGS_WITH_VALUE):
            index += 1
            continue
        if arg in GLOBAL_FLAGS_WITH_VALUE:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return arg
    return None


def _output_path_for_command(
    output_dir: Path,
    command_name: str,
    argv: list[str],
    command_index: int | None,
) -> Path:
    fixed_name = FIXED_OUTPUT_FILENAMES.get(command_name)
    if fixed_name:
        return output_dir / fixed_name

    if command_name == "dl_transcript" and command_index is not None:
        transcript_id = _extract_transcript_id(argv, command_index)
        if transcript_id:
            return output_dir / f"{_sanitize_filename_component(transcript_id)}.txt"
        return output_dir / "transcript.txt"

    return output_dir / f"{command_name}.out"


def main(argv: list[str]) -> int:
    repo_root = _repo_root_from_script()
    _ensure_repo_imports(repo_root)
    from netdocs.env import load_dotenv_file

    caption_bin = _caption_bin_for_repo(repo_root)
    env_file = _env_file_from_argv(argv, repo_root)
    load_dotenv_file(env_file, override=True)

    if not caption_bin.exists():
        print(f"caption binary not found at {caption_bin}", file=sys.stderr)
        print("Run 'uv sync' at the repo root to install caption-cli into .venv.", file=sys.stderr)
        return 1

    caption_args = list(argv)
    if not _has_env_override(caption_args):
        caption_args = ["--env-file", str(env_file), *caption_args]
    command_name, command_index = _resolve_command(caption_args)

    cmd = [
        str(caption_bin),
        *caption_args,
    ]
    child_env = os.environ.copy()
    if command_name in TOKEN_HEAVY_COMMANDS:
        completed = subprocess.run(
            cmd,
            cwd=str(repo_root),
            env=child_env,
            capture_output=True,
            text=True,
        )
        output_dir = repo_root / CAPTION_OUTPUT_DIRNAME
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = _output_path_for_command(output_dir, command_name, caption_args, command_index)
        output_path.write_text(completed.stdout, encoding="utf-8")
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr)
        print(f"Saved {command_name} output to {output_path}")
        return completed.returncode

    return subprocess.call(cmd, cwd=str(repo_root), env=child_env)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
