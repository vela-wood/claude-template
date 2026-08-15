"""Shared paths and error type for the setup modules (config/)."""
from __future__ import annotations

from pathlib import Path

# This file lives in <repo>/config/, one level below the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"
LOCAL_SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.local.json"
# User-level Claude Code directory. The statusline installs there
# (hooks/ccstatus.py + a statusLine in settings.json), refreshing the
# usage cache in every folder; a repo-local statusLine would shadow it
# inside this repo.
USER_CLAUDE_DIR = Path.home() / ".claude"
USER_SETTINGS_PATH = USER_CLAUDE_DIR / "settings.json"


class SetupError(Exception):
    pass
