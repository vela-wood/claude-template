"""Statusline task: install the statusline (ccstatus.py, which renders
the bar and caches the usage payload) into the user's ~/.claude. Nothing
statusline-related is written inside the repo's .claude/. Pure helpers; no
TUI here. (Repo-local usage-guard hooks live in config/guard.py — they are
the one piece that IS written inside the repo's .claude/.)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .common import (
    SetupError,
    load_settings_or_raise,
    quote_for_platform,
    write_settings,
)

SCRIPT_FILENAME = "ccstatus.py"


def _is_venv_path_entry(entry: str, virtual_env: str | None) -> bool:
    if virtual_env and (entry == virtual_env or entry.startswith(virtual_env.rstrip(os.sep) + os.sep)):
        return True
    return ".venv" in Path(entry).parts


def validate_python(python3: str, *, timeout: float = 8.0) -> bool:
    """True when `python3` is a runnable Python interpreter. Actually
    executes it (`-c`, not `--version`: the Windows Store python.exe stub
    exits nonzero without executing code, so `-c` rejects it)."""
    if not python3:
        return False
    kwargs: dict = {}
    if sys.platform == "win32":
        # No console window flash under the TUI.
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            [python3, "-c", "import sys; sys.exit(0)"],
            capture_output=True,
            timeout=timeout,
            **kwargs,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def detect_python3() -> str | None:
    """A python3 that outlives this venv, validated to actually run, or
    None when nothing runnable is found (caller must ask the user).

    Under `uv run`, both sys.executable and a naive which() resolve into
    .venv/bin — a venv that may not exist tomorrow — so PATH entries under
    $VIRTUAL_ENV (and any .venv path segment) are filtered out first.
    Every candidate is checked with validate_python before being returned,
    so a dead command is never handed to the installer.
    """
    virtual_env = os.environ.get("VIRTUAL_ENV")
    entries = [
        entry
        for entry in (os.environ.get("PATH") or "").split(os.pathsep)
        if entry and not _is_venv_path_entry(entry, virtual_env)
    ]
    filtered_path = os.pathsep.join(entries)
    candidates: list[str] = []
    if sys.platform == "win32":
        for name in ("python", "python3", "py"):
            found = shutil.which(name, path=filtered_path)
            if found:
                candidates.append(found)
        # uv-managed base interpreter: outlives the venv it spawned.
        candidates.append(str(Path(sys.base_prefix) / "python.exe"))
    else:
        found = shutil.which("python3", path=filtered_path)
        if found:
            candidates.append(found)
        candidates.append("/usr/bin/python3")
        candidates.append(str(Path(sys.base_prefix) / "bin" / "python3"))
    for candidate in candidates:
        # Module-attribute call so tests can monkeypatch validate_python.
        if validate_python(candidate):
            return candidate
    return None


def script_source(repo_root: Path) -> Path:
    """The in-repo statusline script (the install source)."""
    return Path(repo_root) / SCRIPT_FILENAME


def installed_script(user_claude_dir: Path) -> Path:
    """Where the statusline script lives once installed."""
    return Path(user_claude_dir) / "hooks" / SCRIPT_FILENAME


def build_statusline_command(
    python3: str,
    script_path: Path,
    *,
    platform: str = sys.platform,
) -> str:
    """The statusLine command for ~/.claude/settings.json: the installed
    statusline script. No Node, no external renderer."""
    quoted_python = quote_for_platform(python3, platform)
    quoted_script = quote_for_platform(script_path, platform)
    return f"{quoted_python} {quoted_script}"


def uses_installed_script(command: str | None, user_claude_dir: Path) -> bool:
    """True when a statusLine command runs the installed script (which is
    what refreshes the usage cache the guard reads). Substring check:
    quoting on either platform keeps the raw path intact."""
    if not command:
        return False
    return str(installed_script(user_claude_dir)) in command


def install_state(repo_root: Path, user_claude_dir: Path) -> str:
    """'missing' | 'outdated' | 'current' — the installed copy vs the repo
    source, compared by content so repo updates surface in the hub."""
    target = installed_script(user_claude_dir)
    try:
        installed = target.read_bytes()
    except OSError:
        return "missing"
    try:
        source = script_source(repo_root).read_bytes()
    except OSError:
        return "current"  # no source to compare against; don't nag
    return "current" if installed == source else "outdated"


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


def merge_statusline_settings(path: Path, statusline_cmd: str) -> bool:
    """Set statusLine in a Claude settings file, preserving every other key
    (permissions, hooks, env, ...). Returns False when already identical
    (skip). Malformed JSON → SetupError; never clobber."""
    data = load_settings_or_raise(path)
    entry = {"type": "command", "command": statusline_cmd, "padding": 0}
    if data.get("statusLine") == entry:
        return False
    data["statusLine"] = entry
    write_settings(path, data)
    return True


def remove_local_statusline(path: Path) -> bool:
    """Delete the statusLine block from the repo's settings.local.json (a
    leftover local statusLine would shadow the account-wide one inside this
    repo), preserving every other key. Returns False when there is nothing
    to remove. Malformed JSON → SetupError; never clobber."""
    if not path.exists():
        return False
    data = load_settings_or_raise(path)
    if "statusLine" not in data:
        return False
    del data["statusLine"]
    write_settings(path, data)
    return True


@dataclass(frozen=True)
class PreparedStatuslineInstallation:
    """Validated statusline copy and final user-settings mutation."""

    source: Path
    target: Path
    command: str
    settings_path: Path
    settings: dict
    settings_changed: bool


def prepare_user_statusline(
    repo_root: Path,
    user_claude_dir: Path,
    python3: str,
    *,
    platform: str = sys.platform,
    settings_path: Path | None = None,
) -> PreparedStatuslineInstallation:
    """Validate and prepare an install without copying or writing files."""
    source = script_source(repo_root)
    if not source.exists():
        raise SetupError(
            f"The statusline script is missing from this toolkit "
            f"({source}) — update the toolkit (git pull) and try again."
        )
    target = installed_script(user_claude_dir)
    command = build_statusline_command(python3, target, platform=platform)
    exact_settings_path = (
        Path(settings_path)
        if settings_path is not None
        else Path(user_claude_dir) / "settings.json"
    )
    settings = load_settings_or_raise(exact_settings_path)
    entry = {"type": "command", "command": command, "padding": 0}
    settings_changed = settings.get("statusLine") != entry
    if settings_changed:
        settings["statusLine"] = entry
    return PreparedStatuslineInstallation(
        source=source,
        target=target,
        command=command,
        settings_path=exact_settings_path,
        settings=settings,
        settings_changed=settings_changed,
    )


def commit_user_statusline(prepared: PreparedStatuslineInstallation) -> None:
    """Commit an already-validated statusline preparation without rereading."""
    prepared.target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(prepared.source, prepared.target)
    try:
        os.chmod(prepared.target, 0o755)
    except OSError:
        pass  # the command runs it via python3, not the execute bit
    if prepared.settings_changed:
        write_settings(prepared.settings_path, prepared.settings)


def install_user_statusline(
    repo_root: Path,
    user_claude_dir: Path,
    python3: str,
    *,
    platform: str = sys.platform,
    settings_path: Path | None = None,
) -> str:
    """Install the statusline account-wide: copy the script into
    ~/.claude/hooks/ and point statusLine in ~/.claude/settings.json at it.
    Returns the command written. The exact settings path is validated before
    the script is copied."""
    prepared = prepare_user_statusline(
        repo_root,
        user_claude_dir,
        python3,
        platform=platform,
        settings_path=settings_path,
    )
    commit_user_statusline(prepared)
    return prepared.command
