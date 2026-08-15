"""Shared paths and error type for the setup modules (config/)."""
from __future__ import annotations

from pathlib import Path

# This file lives in <repo>/config/, one level below the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"
LOCAL_SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.local.json"
# User-level Claude Code settings. A statusLine here that runs this repo's
# tee (piped into a personal renderer) refreshes the usage cache in every
# folder; a local statusLine would shadow it inside this repo.
USER_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


class SetupError(Exception):
    pass
