"""OCR task: store the Gemini API key that `startup.py --ocr` needs.

OCR runs in Google's cloud (startup_lib/gemini_ocr.py): every page image
of a scanned PDF is uploaded, so the only setup is a key in the repo-root
.env. The user creates the key at KEY_URL and pastes it here; nothing is
ever fetched or installed. Pure helpers; no TUI (the screen lives in
config/app.py).
"""
from __future__ import annotations

from pathlib import Path

from startup_lib.gemini_client import ENV_KEY, KEY_URL, MODEL

from .common import ENV_FILE, SetupError
from .env import WriteResult, read_existing_env_values, write_env_file

STATE_MISSING = "missing"  # no key in .env
STATE_READY = "ready"


def configured_key(env_file: Path = ENV_FILE) -> str | None:
    """The key python-dotenv will load: the last non-blank value in .env."""
    values = [v.strip() for v in read_existing_env_values(env_file).get(ENV_KEY, [])]
    values = [v for v in values if v]
    return values[-1] if values else None


def ocr_state(env_file: Path = ENV_FILE) -> str:
    """STATE_MISSING | STATE_READY."""
    return STATE_READY if configured_key(env_file) else STATE_MISSING


def validate_key(raw: str) -> str:
    """Trim and sanity-check a pasted key; the API is the real check."""
    key = raw.strip()
    if not key:
        raise SetupError("Please paste your Gemini API key first.")
    if any(ch.isspace() for ch in key):
        raise SetupError("A key has no spaces in it. Check that you copied only the key.")
    return key


def save_key(key: str, env_file: Path = ENV_FILE) -> WriteResult:
    """Append the key to .env. A different existing value is not edited in
    place; the new line is appended and wins, since dotenv keeps the last."""
    return write_env_file(env_file, {ENV_KEY: key})


def status_row(state: str) -> tuple[str, str]:
    """The hub row for this task, as (status, detail) columns."""
    if state == STATE_MISSING:
        return "Not set up", "scanned PDFs can't be read yet"
    return "Ready", f"scanned PDFs are read with {MODEL}"


__all__ = [
    "ENV_KEY",
    "KEY_URL",
    "MODEL",
    "STATE_MISSING",
    "STATE_READY",
    "configured_key",
    "ocr_state",
    "save_key",
    "status_row",
    "validate_key",
]
