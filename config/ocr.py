"""OCR task: install Franken OCR (the `focr` command) and its model.

`uv run startup.py --ocr` shells out to `focr` (see startup.run_ocr). That
binary is not a Python dependency and cannot be installed by uv — upstream
ships prebuilt binaries through an install script per platform, and the
model weights are a separate ~4 GB download (`focr pull`). This module owns
locating both, and building the commands that install them. Pure helpers;
no TUI here (the screen lives in config/app.py).

Layout the upstream installers use, and that find_focr/model_cache_dir
mirror so a fresh install is found even when its directory is not yet on
PATH in the running process:

    binary   POSIX:   ~/.local/bin/focr        (PREFIX/bin with PREFIX set)
             Windows: %LOCALAPPDATA%\\Programs\\focr\\focr.exe
    model    POSIX:   ~/.cache/franken_ocr/*.focrq
             Windows: %LOCALAPPDATA%\\franken_ocr\\*.focrq
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import httpx

from .common import SetupError

INSTALL_SH_URL = (
    "https://raw.githubusercontent.com/Dicklesworthstone/franken_ocr/main/install.sh"
)
INSTALL_PS1_URL = (
    "https://raw.githubusercontent.com/Dicklesworthstone/franken_ocr/main/install.ps1"
)
PROJECT_URL = "https://github.com/Dicklesworthstone/franken_ocr"

# What `focr pull` downloads: the int8 weights plus tokenizer.
MODEL_DOWNLOAD_SIZE = "about 4 GB"

STATE_MISSING = "missing"  # no focr binary
STATE_NO_MODEL = "no-model"  # binary present, weights not downloaded
STATE_READY = "ready"


def _is_windows() -> bool:
    # Module-attribute call sites let tests fake the platform.
    return sys.platform == "win32"


def _local_app_data() -> Path | None:
    """%LOCALAPPDATA%, with the installers' own USERPROFILE fallback."""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local)
    profile = os.environ.get("USERPROFILE")
    if profile:
        return Path(profile) / "AppData" / "Local"
    return None


def binary_name() -> str:
    return "focr.exe" if _is_windows() else "focr"


def install_dirs() -> list[Path]:
    """Directories an installer may have put the binary in, most likely
    first. Searched only after PATH."""
    if _is_windows():
        local = _local_app_data()
        return [local / "Programs" / "focr"] if local else []
    dirs = [Path.home() / ".local" / "bin"]
    prefix = os.environ.get("PREFIX")
    if prefix:
        dirs.append(Path(prefix) / "bin")
    dirs += [Path("/usr/local/bin"), Path("/opt/homebrew/bin")]
    return dirs


def find_focr() -> str | None:
    """Absolute path to a runnable `focr`, or None.

    PATH first, then the installers' default directories: on POSIX the
    installer only edits shell rc files, so a freshly installed binary is
    not on PATH in this process (nor in an already-open terminal), and on
    Windows the user PATH edit does not reach processes already running.
    """
    found = shutil.which(binary_name())
    if found:
        return found
    for directory in install_dirs():
        candidate = directory / binary_name()
        if candidate.is_file():
            return str(candidate)
    return None


def model_cache_dir() -> Path:
    """Where `focr pull` writes the weights."""
    if _is_windows():
        local = _local_app_data()
        base = local if local else Path.home() / "AppData" / "Local"
        return base / "franken_ocr"
    return Path.home() / ".cache" / "franken_ocr"


def model_installed() -> bool:
    """True when at least one model artifact (`*.focrq`) is downloaded.
    Named models land in a `models/` subdirectory, so the search recurses."""
    cache = model_cache_dir()
    try:
        return any(cache.rglob("*.focrq"))
    except OSError:
        return False


def ocr_state() -> str:
    """STATE_MISSING | STATE_NO_MODEL | STATE_READY."""
    if find_focr() is None:
        return STATE_MISSING
    return STATE_READY if model_installed() else STATE_NO_MODEL


def installer_url() -> str:
    return INSTALL_PS1_URL if _is_windows() else INSTALL_SH_URL


def installer_filename() -> str:
    return "install.ps1" if _is_windows() else "install.sh"


def installer_command(script: Path) -> list[str]:
    """Run a downloaded installer script non-interactively.

    `--no-pull` / `-NoPull`: the model download is our own next step, so the
    installer should not also prompt for it. `--easy-mode` puts ~/.local/bin
    on PATH in the user's shell rc files (the POSIX installer otherwise only
    prints advice); the Windows installer already edits the user PATH.
    `--no-gum` keeps output plain for the log pane.
    """
    if _is_windows():
        return [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-NoPull",
        ]
    return ["bash", str(script), "--easy-mode", "--no-pull", "--no-gum"]


def pull_command(focr: str) -> list[str]:
    """Download the model weights. Idempotent: an already-present artifact
    is left alone, so this is safe to re-run."""
    return [focr, "pull"]


def download_installer(dest_dir: Path, *, timeout: float = 30.0) -> Path:
    """Fetch the platform installer into dest_dir and return its path.

    Downloaded to a file rather than piped into a shell so that the script
    can take flags, and so a failed download never reaches an interpreter.
    """
    url = installer_url()
    path = Path(dest_dir) / installer_filename()
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise SetupError(
            "Couldn't download the text-recognition installer. Check your "
            f"internet connection and try again. ({exc})"
        )
    path.write_bytes(response.content)
    try:
        path.chmod(0o700)
    except OSError:
        pass  # the script is run through bash/powershell, not the execute bit
    return path


def status_text(state: str, focr: str | None) -> str:
    """The hub row for this task."""
    if state == STATE_MISSING:
        return "not set up yet — scanned PDFs can't be read yet"
    if state == STATE_NO_MODEL:
        return (
            "needs attention — the reader is installed but its "
            f"{MODEL_DOWNLOAD_SIZE} model hasn't been downloaded yet"
        )
    on_path = shutil.which(binary_name()) is not None
    if not on_path:
        return (
            f"ready — installed at {focr} (restart your terminal to use "
            "`focr` directly)"
        )
    return "ready — scanned PDFs can be read"
