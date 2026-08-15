"""Statusline task: build the statusLine command and merge it into
.claude/settings.local.json. Pure helpers (§A.2); no TUI here.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .common import SetupError

# Pinned at implementation time (2026-08); upgrading is a one-line change
# here — never a per-refresh `@latest` resolution in the statusline command.
PINNED_CCSTATUSLINE_VERSION = "2.2.27"


def _is_venv_path_entry(entry: str, virtual_env: str | None) -> bool:
    if virtual_env and (entry == virtual_env or entry.startswith(virtual_env.rstrip(os.sep) + os.sep)):
        return True
    return ".venv" in Path(entry).parts


def detect_python3() -> str:
    """A python3 that outlives this venv, for the statusline command.

    Under `uv run`, both sys.executable and a naive which() resolve into
    .venv/bin — a venv that may not exist tomorrow — so PATH entries under
    $VIRTUAL_ENV (and any .venv path segment) are filtered out first.
    Fallback: /usr/bin/python3 on POSIX; on Windows whatever filtered
    which() finds (python, then the py launcher).
    """
    virtual_env = os.environ.get("VIRTUAL_ENV")
    entries = [
        entry
        for entry in (os.environ.get("PATH") or "").split(os.pathsep)
        if entry and not _is_venv_path_entry(entry, virtual_env)
    ]
    filtered_path = os.pathsep.join(entries)
    if sys.platform == "win32":
        for name in ("python", "python3", "py"):
            found = shutil.which(name, path=filtered_path)
            if found:
                return found
        return "python"
    return shutil.which("python3", path=filtered_path) or "/usr/bin/python3"


def _quote_for_platform(arg: str, platform: str) -> str:
    """shlex.quote on POSIX; list2cmdline-style double-quoting on Windows.
    Testable on any OS."""
    if platform == "win32":
        return subprocess.list2cmdline([arg])
    return shlex.quote(arg)


def detect_renderer(repo_root: Path) -> str | None:
    """Pinned local ccstatusline executable path, or None."""
    bin_dir = Path(repo_root) / ".statusline" / "node_modules" / ".bin"
    candidates = ["ccstatusline.cmd", "ccstatusline"] if sys.platform == "win32" else ["ccstatusline"]
    for name in candidates:
        exe = bin_dir / name
        if exe.exists():
            return str(exe)
    return None


def build_statusline_command(
    python3: str,
    repo_root: Path,
    renderer: str | None,
    *,
    platform: str = sys.platform,
) -> str:
    """The statusLine command for .claude/settings.local.json.

    renderer=None → tee-only `--render` (zero Node); renderer=<path> → tee
    piped into the pinned local install. Never `npx -y ...@latest`.
    """
    tee = str(Path(repo_root) / ".claude" / "hooks" / "ccstatus-tee.py")
    quoted_python = _quote_for_platform(python3, platform)
    quoted_tee = _quote_for_platform(tee, platform)
    if renderer is None:
        return f"{quoted_python} {quoted_tee} --render"
    return f"{quoted_python} {quoted_tee} | {_quote_for_platform(renderer, platform)}"


def uses_repo_tee(command: str | None, repo_root: Path) -> bool:
    """True when a statusLine command runs this repo's ccstatus-tee.py —
    e.g. a user-level (~/.claude/settings.json) command piping the tee into
    a personal renderer, which refreshes the usage cache in every folder.
    Substring check: quoting on either platform keeps the raw path intact."""
    if not command:
        return False
    return str(Path(repo_root) / ".claude" / "hooks" / "ccstatus-tee.py") in command


def statusline_settings(path: Path) -> str | None:
    """Current statusLine command in a Claude settings file, or None."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    status_line = data.get("statusLine")
    if isinstance(status_line, dict) and isinstance(status_line.get("command"), str):
        return status_line["command"]
    return None


def merge_local_settings(path: Path, statusline_cmd: str) -> bool:
    """Set statusLine in settings.local.json, preserving every other key
    (permissions, prefersReducedMotion, ...). Returns False when already
    identical (skip). Malformed JSON → SetupError; never clobber."""
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise SetupError(f"{path} is not valid JSON ({exc}); fix or delete it first.")
        if not isinstance(data, dict):
            raise SetupError(f"{path} does not contain a JSON object; fix or delete it first.")
    else:
        data = {}
    entry = {"type": "command", "command": statusline_cmd, "padding": 0}
    if data.get("statusLine") == entry:
        return False
    data["statusLine"] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def remove_local_statusline(path: Path) -> bool:
    """Delete the statusLine block from settings.local.json (so a global
    tee'd statusline applies inside this repo too), preserving every other
    key. Returns False when there is nothing to remove. Malformed JSON →
    SetupError; never clobber."""
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SetupError(f"{path} is not valid JSON ({exc}); fix or delete it first.")
    if not isinstance(data, dict):
        raise SetupError(f"{path} does not contain a JSON object; fix or delete it first.")
    if "statusLine" not in data:
        return False
    del data["statusLine"]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True
