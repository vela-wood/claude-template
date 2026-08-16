"""Shared paths, settings helpers, and error type for setup modules."""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from fsio import atomic_write_text

# This file lives in <repo>/config/, one level below the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"


class SetupError(Exception):
    pass


def user_claude_dir() -> Path:
    """Claude's user config directory, honoring a non-empty override."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".claude"


def local_settings_path(repo_root: Path) -> Path:
    """The per-machine Claude settings path for a repository."""
    return Path(repo_root) / ".claude" / "settings.local.json"


def posix_path(arg: object) -> str:
    """Render a path with forward slashes. Windows accepts them everywhere,
    and they cannot be eaten as escapes by the shell that runs our commands."""
    return str(arg).replace("\\", "/")


def shell_quote(arg: object) -> str:
    """Quote one argument for the POSIX shell that runs Claude Code hooks and
    the statusline. On Windows that shell is Git Bash, not cmd: a backslash
    path is mangled there (bare paths lose every separator, and a trailing
    backslash escapes the closing quote), so separators are normalized first."""
    return shlex.quote(posix_path(arg))


def load_settings_or_raise(path: Path) -> dict:
    """Load a settings object without ever repairing malformed content."""
    path = Path(path)
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        # An empty file is what's left after a hand-repair; treat it as "no
        # settings yet" rather than malformed JSON we must refuse to touch.
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SetupError(
            f"A settings file has a problem and setup can't change it "
            f"safely — ask for help, or delete it and run setup again. "
            f"(File: {path}; {exc})"
        )
    if not isinstance(data, dict):
        raise SetupError(
            f"A settings file has a problem and setup can't change it "
            f"safely — ask for help, or delete it and run setup again. "
            f"(File: {path}; not in the expected format)"
        )
    return data


def write_settings(path: Path, data: dict) -> None:
    """Atomically serialize and replace one Claude settings object."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2) + "\n"
    atomic_write_text(path, text)


# Keep import-time constants: callers and tests intentionally monkeypatch them.
# Repo-local, gitignored settings hold the per-machine usage-guard hooks.
LOCAL_SETTINGS_PATH = local_settings_path(REPO_ROOT)
# The statusline script, settings, and usage cache live under this directory.
USER_CLAUDE_DIR = user_claude_dir()
USER_SETTINGS_PATH = USER_CLAUDE_DIR / "settings.json"
