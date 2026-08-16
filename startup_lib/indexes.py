"""Typed loaders and serializers for the hash and token indexes.

All UTF-8, all written through fsio's atomic replacement. The OCR index
has the same shape but lives in pdfcheck, next to the classifier that
fills it in.
"""

import csv
import io
from pathlib import Path
from typing import Any

from fsio import atomic_write_text
from startup_lib.common import HASH_INDEX_FILENAME, TOKEN_INDEX_FILENAME

# ---------------------------------------------------------------------------
# Index I/O (three typed loaders/serializers; all UTF-8, atomic replacement)
# ---------------------------------------------------------------------------


def _serialize_index(index: dict[str, Any], value_header: str) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["file", value_header])
    for rel_path in sorted(index):
        writer.writerow([rel_path, index[rel_path]])
    return buf.getvalue()


def serialize_hash_index(index: dict[str, str]) -> str:
    return _serialize_index(index, "hash")


def serialize_token_index(index: dict[str, int]) -> str:
    return _serialize_index(index, "tokens")


def load_hash_index(root: Path) -> dict[str, str]:
    """Load .hash_index.csv → {source_relative_path: hash}."""
    index_path = root / HASH_INDEX_FILENAME
    index: dict[str, str] = {}
    if not index_path.exists():
        return index
    with open(index_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            index[row["file"]] = row["hash"]
    return index


def save_hash_index(root: Path, index: dict[str, str]) -> None:
    """Write .hash_index.csv from {source_relative_path: hash}."""
    atomic_write_text(
        root / HASH_INDEX_FILENAME, serialize_hash_index(index), newline=""
    )


def load_token_index(root: Path) -> dict[str, int]:
    """Load .token_index.csv → {sidecar_relative_path: token_count}.

    Rows with non-integer token values are dropped: an invalid token row is
    inconsistent certification and must trigger reconversion.
    """
    index_path = root / TOKEN_INDEX_FILENAME
    index: dict[str, int] = {}
    if not index_path.exists():
        return index
    with open(index_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                index[row["file"]] = int(row["tokens"])
            except (ValueError, TypeError):
                continue
    return index


def save_token_index(root: Path, index: dict[str, int]) -> None:
    """Write .token_index.csv from {sidecar_relative_path: token_count}."""
    atomic_write_text(
        root / TOKEN_INDEX_FILENAME, serialize_token_index(index), newline=""
    )

