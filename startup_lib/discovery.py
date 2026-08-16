"""Walking the working folder and hashing what it finds."""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from document_conversion import MBX_SUFFIX, SOURCE_SUFFIXES
from startup_lib import common
from startup_lib.common import (
    CAPTION_OUTPUT_DIRNAME,
    STATUS_FAILED,
    ProcessingResult,
    _rel,
)

# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_sources(root: Path) -> tuple[list[Path], list[Path]]:
    """Walk root and return (supported sources, noticed .mbx files).

    Dot-prefixed directories and the caption cache are pruned before
    descent. Dot-prefixed *files* outside pruned trees stay eligible;
    '~'-prefixed temporary files are skipped. .mbx files are collected for
    the user notice but are never sources (ambiguous Eudora/Outlook Express
    binary formats are incompatible with Unix mbox).
    """
    sources: list[Path] = []
    mbx_files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [
            d
            for d in dirnames
            if not d.startswith(".") and d != CAPTION_OUTPUT_DIRNAME
        ]
        for name in filenames:
            if name.startswith("~"):
                continue
            path = Path(dirpath) / name
            suffix = path.suffix.lower()
            if suffix == MBX_SUFFIX:
                mbx_files.append(path)
            elif suffix in SOURCE_SUFFIXES:
                sources.append(path)
    return sorted(sources), sorted(mbx_files)


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def hash_sources(
    root: Path, sources: list[Path]
) -> tuple[dict[str, str], list[ProcessingResult]]:
    """Hash all discovered sources in parallel.

    Returns ({rel: hash} for successes, failed ProcessingResults). A hash
    failure means the source is not classified, converted, or tokenized;
    its prior index rows and sidecar are preserved by reconciliation.
    """
    hashes: dict[str, str] = {}
    failures: list[ProcessingResult] = []
    with ThreadPoolExecutor() as pool:
        futures = {pool.submit(common.hash_file, src): src for src in sources}
        for future in as_completed(futures):
            src = futures[future]
            rel = _rel(root, src)
            try:
                hashes[rel] = future.result()
            except Exception as exc:
                failures.append(
                    ProcessingResult(
                        rel,
                        STATUS_FAILED,
                        "hash",
                        detail=f"hashing failed: {type(exc).__name__}: {exc}",
                    )
                )
    return hashes, failures

