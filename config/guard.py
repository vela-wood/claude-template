"""Usage-guard hooks: install usage_guard.py's PreToolUse/SessionStart
hooks into the repo's .claude/settings.local.json (gitignored, so the
guard stays per-machine and repo-local while the statusline stays
account-wide). Pure helpers; no TUI here.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path

from .common import (
    SetupError,
    load_settings_or_raise,
    shell_quote,
    write_settings,
)

GUARD_SCRIPT_FILENAME = "usage_guard.py"
GUARD_MATCHERS = {"PreToolUse": "*", "SessionStart": "startup|clear"}


def _is_ours(hook: object) -> bool:
    """A hook entry we installed (or the older hand-written equivalent):
    any command that runs usage_guard.py."""
    return (
        isinstance(hook, dict)
        and isinstance(hook.get("command"), str)
        and GUARD_SCRIPT_FILENAME in hook["command"]
    )


def build_guard_hook_commands(python3: str, repo_root: Path) -> dict[str, str]:
    """Hook commands per event. The PreToolUse fast path shell-tests for
    .ccguard/ before spawning Python, so unarmed sessions (the default)
    never pay an interpreter start per tool call.

    One POSIX form on every platform: Claude Code runs hook commands through
    a POSIX shell (Git Bash on Windows), never cmd, so a cmd `if exist` test
    fails to parse there and takes every tool call down with it."""
    script = Path(repo_root) / GUARD_SCRIPT_FILENAME
    ccguard = Path(repo_root) / ".ccguard"
    q_python = shell_quote(python3)
    q_script = shell_quote(script)
    q_ccguard = shell_quote(ccguard)
    return {
        "PreToolUse": f"[ ! -d {q_ccguard} ] || {q_python} {q_script} --hook-json",
        "SessionStart": f"{q_python} {q_script} --session-start",
    }


@dataclass(frozen=True)
class PreparedGuardHooks:
    """Validated final guard settings ready for an atomic commit."""

    path: Path
    settings: dict
    changed: bool


def prepare_guard_hooks(
    path: Path,
    python3: str,
    repo_root: Path,
) -> PreparedGuardHooks:
    """Validate and merge guard hooks in memory without writing anything."""
    path = Path(path)
    data = load_settings_or_raise(path)
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise SetupError(
            f"A settings file has a problem and setup can't change it "
            f"safely — ask for help, or delete it and run setup again. "
            f"(File: {path}; 'hooks' is not in the expected format)"
        )
    for event in GUARD_MATCHERS:
        if event in hooks and not isinstance(hooks[event], list):
            raise SetupError(
                f"A settings file has a problem and setup can't change it "
                f"safely — ask for help, or delete it and run setup again. "
                f"(File: {path}; 'hooks.{event}' is not in the expected format)"
            )
    snapshot = copy.deepcopy(data)

    commands = build_guard_hook_commands(python3, repo_root)
    for event, matcher in GUARD_MATCHERS.items():
        entries = []
        for entry in hooks.get(event, []):
            if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
                kept = [h for h in entry["hooks"] if not _is_ours(h)]
                if not kept:
                    continue  # entry held only our hooks → drop it
                entry = {**entry, "hooks": kept}
            entries.append(entry)
        entries.append(
            {
                "matcher": matcher,
                "hooks": [{"type": "command", "command": commands[event]}],
            }
        )
        hooks[event] = entries
    data["hooks"] = hooks

    return PreparedGuardHooks(path=path, settings=data, changed=data != snapshot)


def commit_guard_hooks(prepared: PreparedGuardHooks) -> None:
    """Commit an already-validated guard preparation without rereading."""
    if prepared.changed:
        write_settings(prepared.path, prepared.settings)


def merge_guard_hooks(path: Path, python3: str, repo_root: Path) -> bool:
    """Install or replace guard hooks, preserving unrelated settings."""
    prepared = prepare_guard_hooks(path, python3, repo_root)
    commit_guard_hooks(prepared)
    return prepared.changed


def guard_hooks_present(path: Path) -> bool:
    """True iff every guard event already has one of our hooks. Tolerant
    read (missing/malformed → False), cheap enough for hub_status."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for event in GUARD_MATCHERS:
        entries = hooks.get(event)
        if not isinstance(entries, list):
            return False
        if not any(
            _is_ours(h)
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("hooks"), list)
            for h in entry["hooks"]
        ):
            return False
    return True
