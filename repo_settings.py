"""Shared read/write for the gitignored repo-root settings.json.

This is the *repo* settings file (user-local preferences like sidecar
naming), distinct from Claude Code's .claude/settings.json. It lives here —
not in config.py — because startup.py must read preferences without
importing textual. SETTINGS_PATH is the only definition of where repo
settings live; startup.py and config.py import it and nothing else
re-derives it. Tests monkeypatch repo_settings.SETTINGS_PATH to a tmp file
(all helpers resolve it at call time, never at import time).

Known keys:
- "sidecar_dotfiles": bool — dot-prefixed markdown sidecars
  (.foo.docx.md) instead of the default visible style (foo.docx.md).
  Default false = current behavior.
- "ocr_int8": bool — run focr's experimental all-int8 decoder for
  `startup.py --ocr`. Default true (~1.9x faster at 0.999 similarity);
  false falls back to focr's conservative recipe.
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
            f"{settings_path}: the file isn't in the expected format"
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


def _read_bool(key: str, default: bool, path: Path | None) -> bool:
    """Strict boolean read: missing → default; present but non-bool
    ("true", 1, null) → RepoSettingsError. No truthiness coercion — a
    typo must never quietly change how documents are converted."""
    data = load_json_object(path)
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, bool):
        raise RepoSettingsError(
            f"{_resolve(path)}: the '{key}' setting must be "
            f"true or false, but it is {value!r}"
        )
    return value


def read_sidecar_dotfiles(path: Path | None = None) -> bool:
    """Dot-prefixed sidecars? Defaulting on a corrupted value in a
    dotfile-style repo would trigger a silent mass rename, so this read is
    strict (see _read_bool)."""
    return _read_bool("sidecar_dotfiles", False, path)


def read_ocr_int8(path: Path | None = None) -> bool:
    """Use focr's experimental all-int8 decoder for --ocr? Default true:
    on the OCR corpus it ran ~1.9x faster than focr's conservative recipe
    at 0.999 token similarity. Set false to fall back if a scan ever
    transcribes worse under it."""
    return _read_bool("ocr_int8", True, path)
