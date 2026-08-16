"""Values and helpers every startup_lib module shares.

SIDECAR_DOTFILES and OCR_INT8 are the two mutable repo preferences:
startup.main() writes them here from settings.json before any other module
runs, and every consumer reads them through this module's namespace so
there is exactly one authority (and one monkeypatch target) per setting.
"""

import zlib
from dataclasses import dataclass
from pathlib import Path

import tiktoken

from document_conversion import SOURCE_SUFFIXES

HASH_INDEX_FILENAME = ".hash_index.csv"
TOKEN_INDEX_FILENAME = ".token_index.csv"
CAPTION_OUTPUT_DIRNAME = "caption_cache"

# Statuses for ProcessingResult
STATUS_UNCHANGED = "unchanged"
STATUS_CONVERTED = "converted"
STATUS_FAILED = "failed"
STATUS_DEFERRED_FOR_OCR = "deferred_for_ocr"

# Cap applies specifically to active converter calls; hashing and index
# work use separate default-sized pools.
CONVERSION_MAX_WORKERS = 4

# Sidecar naming style; main() sets this from the repo settings.json
# (repo_settings.read_sidecar_dotfiles) before any index load.
SIDECAR_DOTFILES = False

# focr's experimental all-int8 decoder for --ocr; main() sets this from
# the repo settings.json (repo_settings.read_ocr_int8).
OCR_INT8 = True

# Create tiktoken encoding once at module level (thread-safe, Rust-backed)
_encoding = tiktoken.get_encoding("cl100k_base")


@dataclass(frozen=True)
class ProcessingResult:
    """Immutable per-source outcome; only the orchestrator stages indexes."""

    source_rel: str
    status: str
    route: str
    sidecar_rel: str | None = None
    file_hash: str | None = None
    tokens: int | None = None
    ocr_done: bool = False  # OCR-state transition to stage on success
    detail: str = ""
    staged_sidecar: Path | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def hash_file(path: Path) -> str:
    """Return CRC32 hex digest of a file, streaming to avoid large allocations."""
    checksum = 0
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            checksum = zlib.crc32(chunk, checksum)
    return f"{checksum & 0xFFFFFFFF:08x}"


def count_tokens(path: Path) -> int:
    """Count tokens in a UTF-8 text file using tiktoken cl100k_base."""
    text = path.read_text(encoding="utf-8")
    return len(_encoding.encode(text))


def _sidecar_path(source: Path, *, dotted: bool) -> Path:
    """Return the sidecar path for ``source`` in the requested style."""
    if source.suffix.lower() not in SOURCE_SUFFIXES:
        raise ValueError(f"Unsupported source type: {source}")
    if dotted:
        return source.parent / f".{source.name}.md"
    return source.parent / f"{source.name}.md"


def converted_path(source: Path) -> Path:
    """Return the sidecar path selected by the current repo setting."""
    return _sidecar_path(source, dotted=SIDECAR_DOTFILES)


def other_style_path(source: Path) -> Path:
    """The sidecar path in the style NOT currently selected (for migration)."""
    return _sidecar_path(source, dotted=not SIDECAR_DOTFILES)


def _rel(root: Path, path: Path) -> str:
    return str(path.relative_to(root))
