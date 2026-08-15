"""Shared read/write for the gitignored repo-root settings.json.

This is the *repo* settings file (user-local preferences like sidecar
naming), distinct from Claude Code's .claude/settings.json. It lives here —
not in setup_claude.py — because startup.py must read preferences without
importing textual. SETTINGS_PATH is the only definition of where repo
settings live; startup.py and setup_claude.py import it and nothing else
re-derives it. Tests monkeypatch repo_settings.SETTINGS_PATH to a tmp file
(all helpers resolve it at call time, never at import time).

Known keys:
- "sidecar_dotfiles": bool — dot-prefixed markdown sidecars
  (.foo.docx.md) instead of the default visible style (foo.docx.md).
  Default false = current behavior.
"""

from __future__ import annotations

import json
from pathlib import Path

from fsio import atomic_write_text

REPO_ROOT = Path(__file__).resolve().parent
SETTINGS_PATH = REPO_ROOT / "settings.json"


class RepoSettingsError(Exception):
    """Malformed or invalid repo settings; callers must fail loudly."""


def _resolve(path: Path | None) -> Path:
    # Late binding so a monkeypatched SETTINGS_PATH takes effect.
    return Path(path) if path is not None else SETTINGS_PATH


def load_json_object(path: Path | None = None) -> dict:
    """Return the settings object; {} if missing, RepoSettingsError otherwise."""
    settings_path = _resolve(path)
    if not settings_path.exists():
        return {}
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RepoSettingsError(f"{settings_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RepoSettingsError(
            f"{settings_path}: expected a JSON object, got {type(data).__name__}"
        )
    return data


def merge_json_object(existing: dict, updates: dict) -> dict:
    """Shallow top-level merge; updates win."""
    merged = dict(existing)
    merged.update(updates)
    return merged


def write_json_object(data: dict, path: Path | None = None) -> None:
    settings_path = _resolve(path)
    atomic_write_text(settings_path, json.dumps(data, indent=2) + "\n")


def update_json_object(updates: dict, path: Path | None = None) -> dict:
    """Load + merge + write; the one mutator."""
    merged = merge_json_object(load_json_object(path), updates)
    write_json_object(merged, path)
    return merged


def read_sidecar_dotfiles(path: Path | None = None) -> bool:
    """Strict boolean read of "sidecar_dotfiles": missing → False; present
    but non-bool ("true", 1, null) → RepoSettingsError. No truthiness
    coercion: defaulting on a corrupted value in a dotfile-style repo would
    trigger a silent mass rename."""
    data = load_json_object(path)
    if "sidecar_dotfiles" not in data:
        return False
    value = data["sidecar_dotfiles"]
    if not isinstance(value, bool):
        raise RepoSettingsError(
            f"{_resolve(path)}: key 'sidecar_dotfiles' must be a JSON boolean, "
            f"got {value!r}"
        )
    return value
