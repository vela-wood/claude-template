"""Usage-guard hooks: install usage_guard.py's PreToolUse/SessionStart
hooks into the repo's .claude/settings.local.json (gitignored, so the
guard stays per-machine and repo-local while the statusline stays
account-wide). Pure helpers; no TUI here.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

from .common import SetupError
from .statusline import _load_settings_or_raise, _quote_for_platform

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


def build_guard_hook_commands(
    python3: str,
    repo_root: Path,
    *,
    platform: str = sys.platform,
) -> dict[str, str]:
    """Hook commands per event. The PreToolUse fast path shell-tests for
    .ccguard/ before spawning Python, so unarmed sessions (the default)
    never pay an interpreter start per tool call."""
    script = Path(repo_root) / GUARD_SCRIPT_FILENAME
    ccguard = Path(repo_root) / ".ccguard"
    q_python = _quote_for_platform(python3, platform)
    q_script = _quote_for_platform(str(script), platform)
    if platform == "win32":
        # Hand-built dir token: cmd has no backslash escaping inside quotes,
        # and the trailing \ makes `if exist` a directory test. list2cmdline
        # would double that trailing backslash, breaking the test.
        dir_token = '"' + str(ccguard) + '\\"'
        pretooluse = f"if exist {dir_token} {q_python} {q_script} --hook-json"
    else:
        q_ccguard = _quote_for_platform(str(ccguard), platform)
        pretooluse = f"[ ! -d {q_ccguard} ] || {q_python} {q_script} --hook-json"
    return {
        "PreToolUse": pretooluse,
        "SessionStart": f"{q_python} {q_script} --session-start",
    }


def merge_guard_hooks(
    path: Path,
    python3: str,
    repo_root: Path,
    *,
    platform: str = sys.platform,
) -> bool:
    """Install/replace our guard hooks in a Claude settings file, preserving
    every other key and any foreign hooks (even ones sharing our matcher
    entry). Returns False when already identical (skip). Malformed JSON or
    unexpected hooks structure → SetupError; never clobber."""
    data = _load_settings_or_raise(path)
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

    commands = build_guard_hook_commands(python3, repo_root, platform=platform)
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

    if data == snapshot:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


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
