"""Shared atomic file-replacement primitive for sidecars and CSV indexes.

Temp files are Windows-safe: created with delete=False semantics (mkstemp),
closed before os.replace(), and removed by the caller on failure. Text is
always written as UTF-8. An existing target keeps its permissions; a new
target gets 0666 & ~umask so legal documents do not become more broadly
readable than the user's default.
"""

import os
import tempfile
from pathlib import Path

# Capture the process umask once at import (single-threaded); os.umask is
# not safe to toggle from worker threads.
_UMASK = os.umask(0)
os.umask(_UMASK)


def _target_mode(target: Path) -> int:
    if target.exists():
        return os.stat(target).st_mode & 0o777
    return 0o666 & ~_UMASK


def stage_text(target: Path, text: str, newline: str = "\n") -> Path:
    """Write text to a unique closed temp file in target's directory."""
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as f:
            f.write(text)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def commit_staged(tmp: Path, target: Path) -> None:
    """Atomically replace target with the staged temp file."""
    try:
        os.chmod(tmp, _target_mode(target))
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def discard_staged(tmp: Path) -> None:
    tmp.unlink(missing_ok=True)


def atomic_write_text(target: Path, text: str, newline: str = "\n") -> None:
    """stage_text + commit_staged in one step."""
    commit_staged(stage_text(target, text, newline=newline), target)
