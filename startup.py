"""Convert supported documents/emails to Markdown sidecars and maintain
the hash, token, and OCR indexes for the working folder.

Orchestration only: routing and rendering live in document_conversion.py,
PDF classification in pdfcheck.py, atomic replacement in fsio.py.

Certification model: the hash index is written last and is the certification
marker and is withheld entirely when any preceding index write fails. A source
is "unchanged" only when its fresh CRC32 equals the previously certified hash
AND its sidecar exists AND it has a valid token row. Any hashing,
classification, conversion, tokenization, requested-OCR, sidecar-write, or
index-write failure leaves prior state in place, is reported, and produces a
nonzero exit. Pending OCR consent and skipped .mbx notices alone exit zero.
"""

import argparse
import csv
import io
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
import zlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tiktoken

import repo_settings
from document_conversion import (
    MBX_SUFFIX,
    SOURCE_SUFFIXES,
    convert_to_markdown,
    route_for,
)
from fsio import atomic_write_text, commit_staged, discard_staged, stage_text
from netdocs.env import load_repo_dotenv
from pdfcheck import (
    NEEDS_OCR_VERDICTS,
    OCR_INDEX_FILENAME,
    classify_pdf,
    index_row,
    load_ocr_index,
    save_ocr_index,
    serialize_ocr_index,
)

load_repo_dotenv(__file__)

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
        futures = {pool.submit(hash_file, src): src for src in sources}
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


# ---------------------------------------------------------------------------
# Sidecar-style migration and repair
# ---------------------------------------------------------------------------


@dataclass
class MigrationStats:
    renamed: int = 0
    repaired: int = 0
    retokenized: int = 0
    resolved_conflicts: int = 0
    conflicts: list["SidecarConflict"] = field(default_factory=list)
    skip_processing: set[str] = field(default_factory=set)
    defer_sidecar_commit: set[str] = field(default_factory=set)

    @property
    def total(self) -> int:
        return (
            self.renamed
            + self.repaired
            + self.retokenized
            + self.resolved_conflicts
            + len(self.conflicts)
        )

    @property
    def excluded_rels(self) -> set[str]:
        return self.skip_processing | {conflict.source_rel for conflict in self.conflicts}


@dataclass(frozen=True)
class SidecarConflict:
    source_rel: str
    preferred_rel: str
    alternate_rel: str
    reason: str


def _files_equal(first: Path, second: Path) -> bool:
    """Compare two files without loading either one wholly into memory."""
    if first.stat().st_size != second.stat().st_size:
        return False
    with first.open("rb") as left, second.open("rb") as right:
        while True:
            left_chunk = left.read(1024 * 1024)
            right_chunk = right.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _copy_to_unique_backup(path: Path) -> Path:
    """Copy ``path`` to a verified, exclusive, non-sidecar backup."""
    for _attempt in range(100):
        backup = path.with_name(
            f"{path.name}.conflict-preserved-{uuid.uuid4().hex}"
        )
        try:
            destination = backup.open("xb")
        except FileExistsError:
            continue
        try:
            with path.open("rb") as source, destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)
                destination.flush()
        except BaseException:
            destination.close()
            backup.unlink(missing_ok=True)
            raise
        try:
            if not _files_equal(path, backup):
                raise OSError("backup verification failed")
        except BaseException:
            backup.unlink(missing_ok=True)
            raise
        return backup
    raise FileExistsError(f"could not reserve a conflict backup for {path}")


def _preserve_candidates(
    root: Path,
    candidates: list[Path],
    *,
    remove: list[Path] | None = None,
) -> dict[Path, Path]:
    """Secure and verify every backup before removing any candidate name."""
    backups: dict[Path, Path] = {}
    try:
        for candidate in candidates:
            backups[candidate] = _copy_to_unique_backup(candidate)
    except BaseException:
        for backup in backups.values():
            backup.unlink(missing_ok=True)
        raise

    for candidate, backup in backups.items():
        print(f"\tPreserved {_rel(root, candidate)} at {_rel(root, backup)}")

    for candidate in candidates if remove is None else remove:
        candidate.unlink()
    return backups


def _record_conflict(
    stats: MigrationStats,
    source_rel: str,
    preferred_rel: str,
    alternate_rel: str,
    reason: str,
) -> None:
    conflict = SidecarConflict(
        source_rel, preferred_rel, alternate_rel, reason
    )
    stats.conflicts.append(conflict)
    print(
        f"\tERROR: unresolved sidecar conflict for {source_rel}: "
        f"{preferred_rel} and {alternate_rel}: {reason}"
    )


def _authoritative_count(
    token_index: dict[str, int], sidecar_rel: str, sidecar: Path
) -> int | None:
    count = token_index.get(sidecar_rel)
    if sidecar.exists() and type(count) is int:
        return count
    return None


def _canonicalize_authority(
    root: Path,
    authority: Path,
    preferred: Path,
    authority_rel: str,
    preferred_rel: str,
    alternate_rel: str,
    count: int,
    token_index: dict[str, int],
) -> bool:
    """Canonicalize file first, then move/collapse its token row."""
    renamed = authority != preferred
    collapsed_alternate = (
        authority_rel == preferred_rel and alternate_rel in token_index
    )
    if renamed:
        os.replace(authority, preferred)
        print(
            f"\tRenamed authoritative sidecar {authority_rel} -> {preferred_rel}"
        )
    token_index[preferred_rel] = count
    if alternate_rel != preferred_rel:
        token_index.pop(alternate_rel, None)
    if authority_rel != preferred_rel:
        print(f"\tMoved token row {authority_rel} -> {preferred_rel}")
    elif collapsed_alternate:
        print(f"\tCollapsed token rows to {preferred_rel}")
    return renamed


def migrate_sidecars(
    root: Path,
    sources: list[Path],
    hashes: dict[str, str],
    hash_index: dict[str, str],
    token_index: dict[str, int],
    ocr_index: dict[str, dict[str, str]],
) -> MigrationStats:
    """Resolve sidecar naming from physical bytes and loaded token authority."""
    stats = MigrationStats()
    for src in sources:
        rel = _rel(root, src)
        preferred, alternate = converted_path(src), other_style_path(src)
        preferred_rel = _rel(root, preferred)
        alternate_rel = _rel(root, alternate)
        preferred_exists = preferred.exists()
        alternate_exists = alternate.exists()

        # Physical conflicts must be reported even when source hashing failed.
        if preferred_exists and alternate_exists and rel not in hashes:
            _record_conflict(
                stats,
                rel,
                preferred_rel,
                alternate_rel,
                "the source could not be hashed, so authority cannot be evaluated safely",
            )
            continue
        if rel not in hashes:
            continue

        preferred_count = _authoritative_count(
            token_index, preferred_rel, preferred
        )
        alternate_count = _authoritative_count(
            token_index, alternate_rel, alternate
        )
        matching_hash = hash_index.get(rel) == hashes[rel]
        ocr_row = ocr_index.get(rel, {})
        needs_ocr = ocr_row.get("verdict") in NEEDS_OCR_VERDICTS
        current_ocr_recovery = (
            needs_ocr
            and ocr_row.get("ocr_done") == "true"
            and ocr_row.get("hash") == hashes[rel]
        )
        retokenize_allowed = matching_hash and not needs_ocr

        try:
            if preferred_exists and alternate_exists:
                identical = _files_equal(preferred, alternate)
                authoritative = sum(
                    count is not None
                    for count in (preferred_count, alternate_count)
                )

                if identical and authoritative == 2:
                    if preferred_count != alternate_count:
                        _record_conflict(
                            stats,
                            rel,
                            preferred_rel,
                            alternate_rel,
                            "the two authoritative token rows disagree",
                        )
                        continue
                    _preserve_candidates(root, [alternate])
                    _canonicalize_authority(
                        root,
                        preferred,
                        preferred,
                        preferred_rel,
                        preferred_rel,
                        alternate_rel,
                        preferred_count,
                        token_index,
                    )
                    stats.resolved_conflicts += 1
                    continue

                if identical and authoritative == 1:
                    authority = (
                        preferred if preferred_count is not None else alternate
                    )
                    authority_rel = (
                        preferred_rel
                        if preferred_count is not None
                        else alternate_rel
                    )
                    count = (
                        preferred_count
                        if preferred_count is not None
                        else alternate_count
                    )
                    duplicate = alternate if authority == preferred else preferred
                    _preserve_candidates(root, [duplicate])
                    renamed = _canonicalize_authority(
                        root,
                        authority,
                        preferred,
                        authority_rel,
                        preferred_rel,
                        alternate_rel,
                        count,
                        token_index,
                    )
                    stats.renamed += int(renamed)
                    stats.resolved_conflicts += 1
                    continue

                if identical and authoritative == 0:
                    if retokenize_allowed:
                        _preserve_candidates(root, [alternate])
                        token_index[preferred_rel] = count_tokens(preferred)
                        token_index.pop(alternate_rel, None)
                        stats.retokenized += 1
                        stats.resolved_conflicts += 1
                    elif current_ocr_recovery:
                        _preserve_candidates(root, [alternate])
                        stats.resolved_conflicts += 1
                    else:
                        _preserve_candidates(
                            root,
                            [preferred],
                            remove=[preferred, alternate],
                        )
                        token_index.pop(preferred_rel, None)
                        token_index.pop(alternate_rel, None)
                        stats.resolved_conflicts += 1
                    continue

                if not identical and authoritative == 2:
                    _record_conflict(
                        stats,
                        rel,
                        preferred_rel,
                        alternate_rel,
                        "the index identifies two byte-different generated artifacts",
                    )
                    continue

                if not identical and authoritative == 1:
                    authority = (
                        preferred if preferred_count is not None else alternate
                    )
                    authority_rel = (
                        preferred_rel
                        if preferred_count is not None
                        else alternate_rel
                    )
                    count = (
                        preferred_count
                        if preferred_count is not None
                        else alternate_count
                    )
                    unindexed = alternate if authority == preferred else preferred
                    _preserve_candidates(root, [unindexed])
                    renamed = _canonicalize_authority(
                        root,
                        authority,
                        preferred,
                        authority_rel,
                        preferred_rel,
                        alternate_rel,
                        count,
                        token_index,
                    )
                    stats.renamed += int(renamed)
                    stats.resolved_conflicts += 1
                    stats.skip_processing.add(rel)
                    continue

                # Both files are byte-different and neither is indexed.
                _preserve_candidates(root, [preferred, alternate])
                token_index.pop(preferred_rel, None)
                token_index.pop(alternate_rel, None)
                stats.resolved_conflicts += 1
                continue

            existing = (
                preferred if preferred_exists else alternate if alternate_exists else None
            )
            if existing is None:
                if (
                    preferred_rel in token_index
                    or alternate_rel in token_index
                ):
                    token_index.pop(preferred_rel, None)
                    token_index.pop(alternate_rel, None)
                    stats.defer_sidecar_commit.add(rel)
                    stats.repaired += 1
                    print(
                        f"\tRemoved missing-candidate token rows for {rel}; "
                        "any regenerated sidecar will wait for token-index persistence"
                    )
                continue
            existing_rel = preferred_rel if existing == preferred else alternate_rel
            existing_count = (
                preferred_count if existing == preferred else alternate_count
            )

            # The row belonging to the physical file is authoritative. Any row
            # for the missing candidate is collapsed only after canonicalization.
            if existing_count is not None:
                renamed = _canonicalize_authority(
                    root,
                    existing,
                    preferred,
                    existing_rel,
                    preferred_rel,
                    alternate_rel,
                    existing_count,
                    token_index,
                )
                stats.renamed += int(renamed)
                continue

            # Only preferred-file/alternate-row can arise from file-first rename.
            if existing == preferred and type(token_index.get(alternate_rel)) is int:
                token_index[preferred_rel] = token_index.pop(alternate_rel)
                print(f"\tMoved token row {alternate_rel} -> {preferred_rel}")
                stats.repaired += 1
                continue

            no_candidate_rows = (
                preferred_rel not in token_index and alternate_rel not in token_index
            )
            if no_candidate_rows and (retokenize_allowed or current_ocr_recovery):
                if existing == alternate:
                    os.replace(alternate, preferred)
                    print(
                        f"\tRenamed sidecar for recovery {alternate_rel} -> {preferred_rel}"
                    )
                    stats.renamed += 1
                if retokenize_allowed:
                    token_index[preferred_rel] = count_tokens(preferred)
                    stats.retokenized += 1
                continue

            # The existing bytes are unindexed and cannot be overwritten safely.
            stale_candidate_row = (
                preferred_rel in token_index or alternate_rel in token_index
            )
            _preserve_candidates(root, [existing])
            token_index.pop(preferred_rel, None)
            token_index.pop(alternate_rel, None)
            if stale_candidate_row:
                stats.defer_sidecar_commit.add(rel)
        except Exception as exc:
            _record_conflict(
                stats,
                rel,
                preferred_rel,
                alternate_rel,
                f"automatic preservation or canonicalization failed: "
                f"{type(exc).__name__}: {exc}",
            )
    return stats


# ---------------------------------------------------------------------------
# PDF classification (scanned vs digital → needs_ocr)
# ---------------------------------------------------------------------------


def classify_pdfs(
    root: Path,
    pdf_rels: list[str],
    hashes: dict[str, str],
    ocr_index: dict[str, dict[str, str]],
) -> list[ProcessingResult]:
    """Classify new/changed PDFs, updating ocr_index rows in place.

    A classification error preserves the prior OCR row and returns a failed
    result so the source is excluded from OCR selection and conversion.
    """
    failures: list[ProcessingResult] = []
    to_classify = [
        rel for rel in pdf_rels if ocr_index.get(rel, {}).get("hash") != hashes[rel]
    ]
    if not to_classify:
        return failures

    print(f"\nClassifying {len(to_classify)} PDF(s) (scanned vs digital)...")
    for rel in to_classify:
        result = classify_pdf(root / rel)
        if result.verdict.startswith("error:"):
            failures.append(
                ProcessingResult(
                    rel, STATUS_FAILED, "classify", detail=result.verdict
                )
            )
            print(f"\tERROR classifying {rel}: {result.verdict}")
            continue
        ocr_index[rel] = index_row(rel, hashes[rel], result)
        if result.needs_ocr:
            print(f"\t{rel}: {result.verdict} -> needs_ocr")
    return failures


def pending_ocr_rels(root: Path, ocr_index: dict[str, dict[str, str]]) -> list[str]:
    """Return PDFs that are flagged for OCR and do not have current OCR output."""
    return [
        rel
        for rel, row in sorted(ocr_index.items())
        if row["verdict"] in NEEDS_OCR_VERDICTS
        and not (
            row.get("ocr_done") == "true" and converted_path(root / rel).exists()
        )
    ]


# ---------------------------------------------------------------------------
# Sidecar production (shared by conversion and OCR reassembly)
# ---------------------------------------------------------------------------


def _finalize_sidecar(
    root: Path,
    src: Path,
    pre_hash: str,
    text: str,
    route: str,
    ocr_done: bool = False,
    defer_commit: bool = False,
) -> ProcessingResult:
    """Temp-file write, empty rejection, tokenize, rehash, atomic replace.

    Only a fully successful result may later have its hash/token rows
    staged by the orchestrator.
    """
    rel = _rel(root, src)
    out = converted_path(src)
    conv_rel = _rel(root, out)
    tmp = None
    staged_sidecar = None
    try:
        if not text or not text.strip():
            raise ValueError(f"empty conversion output for {src.name}")
        tmp = stage_text(out, text)
        tokens = count_tokens(tmp)
        if hash_file(src) != pre_hash:
            raise ValueError("source changed during conversion")
        if defer_commit:
            staged_sidecar = tmp
            tmp = None
        else:
            commit_staged(tmp, out)
            tmp = None
        return ProcessingResult(
            rel,
            STATUS_CONVERTED,
            route,
            conv_rel,
            pre_hash,
            tokens,
            ocr_done,
            staged_sidecar=staged_sidecar,
        )
    except Exception as exc:
        return ProcessingResult(
            rel,
            STATUS_FAILED,
            route,
            conv_rel,
            detail=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if tmp is not None:
            discard_staged(tmp)


def convert_sources(
    root: Path,
    to_convert: list[Path],
    hashes: dict[str, str],
    defer_commit_rels: set[str] | None = None,
) -> list[ProcessingResult]:
    """Convert sources through the router with at most 4 active converters."""
    if not to_convert:
        return []
    print(f"\nConverting {len(to_convert)} file(s)...")
    results: list[ProcessingResult] = []
    defer_commit_rels = defer_commit_rels or set()

    def _do_convert(src: Path) -> ProcessingResult:
        rel = _rel(root, src)
        out = converted_path(src)
        print(f"\t{rel} -> {out.name}")
        try:
            text = convert_to_markdown(src)
        except Exception as exc:
            return ProcessingResult(
                rel,
                STATUS_FAILED,
                route_for(src),
                _rel(root, out),
                detail=f"{type(exc).__name__}: {exc}",
            )
        return _finalize_sidecar(
            root,
            src,
            hashes[rel],
            text,
            route_for(src),
            defer_commit=rel in defer_commit_rels,
        )

    with ThreadPoolExecutor(max_workers=CONVERSION_MAX_WORKERS) as pool:
        futures = {pool.submit(_do_convert, src): src for src in to_convert}
        for future in as_completed(futures):
            results.append(future.result())
    for r in results:
        if r.status == STATUS_FAILED:
            print(f"\tERROR converting {r.source_rel}: {r.detail}")
    return results


# ---------------------------------------------------------------------------
# OCR (focr) for PDFs flagged needs_ocr
# ---------------------------------------------------------------------------

# The model resizes pages to a 1024px global view, so 150 dpi is ample.
_OCR_RASTER_DPI = 150

# Rasterizing is independent per PDF and spends its time inside MuPDF and
# zlib with the GIL released; same cap as the converter pool.
_OCR_RASTER_MAX_WORKERS = 4

# Windows' CreateProcess rejects a command line longer than 32,767
# characters. Every additional chunk re-pays focr's multi-GB model load,
# so the budget sits just under that ceiling: real runs stay single-chunk.
_ARGV_CHAR_BUDGET = 28_000

# focr 0.7.2 emits its whole --json payload at exit and prints nothing to
# stderr when stderr is a pipe, so without a heartbeat a long batch shows
# the user nothing at all for minutes.
_OCR_HEARTBEAT_SECONDS = 30.0

# Throttle for per-page lines: when focr streams, one line per page is
# useful; when it delivers every record at once, this collapses the burst.
_OCR_PROGRESS_MIN_SECONDS = 1.0

# Applied to focr's environment only for keys the user has not set.
_FOCR_ENV_DEFAULTS: dict[str, str] = {
    # Our own progress lines replace focr's spinner; stderr stays inherited
    # so real warnings still reach the user.
    "FOCR_NO_PROGRESS": "1",
}

# focr's experimental all-int8 decoder: both env keys and the flag are
# required together. Measured on the 10-page OCR corpus (focr 0.7.2,
# two runs): conservative recipe 141/156 s, all-int8 102/101 s, all-int8
# plus continuous batching 79/82 s — ~1.9x — at 0.9993 token similarity
# to the conservative output, where both differing tokens were all-int8
# reading the page correctly. FOCR_BATCH_SPINE belongs to this bundle: on
# its own it was ~5% SLOWER than baseline (152/162 s) and only pays off
# once the decoder is int8. settings.json "ocr_int8": false reverts to
# focr's conservative recipe.
_FOCR_INT8_ENV: dict[str, str] = {
    "FOCR_BATCH_SPINE": "1",
    "FOCR_INT8_ATTN": "1",
    "FOCR_INT8_LMHEAD": "1",
}
_FOCR_INT8_FLAG = "--experimental-full-int8"

# Set from settings.json by main() before any OCR run.
OCR_INT8 = True

_FOCR_NOT_FOUND = (
    "focr command not found. Ask the user to run `uv run config.py` "
    'and choose "Scanned-document reader (OCR)" to install it '
    "(Franken OCR, https://github.com/Dicklesworthstone/franken_ocr)."
)


def _format_duration(seconds: float) -> str:
    """Compact m/s duration for progress lines ('45s', '2m05s')."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m{seconds % 60:02d}s"


class _Heartbeat:
    """Print a periodic 'still working' line for a step with no output.

    A daemon thread waiting on an Event behaves identically on Windows and
    POSIX (no select, no signals, no fork), and the Event makes shutdown
    immediate rather than waiting out the interval.
    """

    def __init__(self, label: str, interval: float = _OCR_HEARTBEAT_SECONDS):
        self._label = label
        self._interval = interval
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        start = time.monotonic()
        while not self._done.wait(self._interval):
            elapsed = _format_duration(time.monotonic() - start)
            print(f"\t{self._label} ({elapsed} elapsed)", flush=True)

    def __enter__(self) -> "_Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._done.set()
        self._thread.join(timeout=1.0)


def _focr_command() -> str:
    """The focr executable to run: PATH first, then the directories its
    installer uses. `uv run config.py` can install focr into ~/.local/bin
    (or %LOCALAPPDATA%\\Programs\\focr) without that directory being on PATH
    in an already-open terminal, and OCR should work in that session anyway.
    Falls back to the bare name so the not-found message stays the same."""
    try:
        from config.ocr import find_focr
    except Exception:
        return "focr"
    return find_focr() or "focr"


def _focr_env() -> dict[str, str]:
    """focr's environment: our defaults, never overriding the user's."""
    env = {**os.environ}
    defaults = dict(_FOCR_ENV_DEFAULTS)
    if OCR_INT8:
        defaults.update(_FOCR_INT8_ENV)
    for key, value in defaults.items():
        env.setdefault(key, value)
    return env


def _focr_argv(command: str, chunk: list[Path]) -> list[str]:
    """The `focr ocr-batch` command line for one chunk of page images."""
    argv = [command, "ocr-batch", *map(str, chunk), "--json"]
    if OCR_INT8:
        argv.append(_FOCR_INT8_FLAG)
    return argv


def _records_from_payload(payload: Any) -> list[dict[str, Any]]:
    """Per-image records inside one focr JSON value (wrapper/array/record)."""
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return [r for r in payload["results"] if isinstance(r, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("image"), str):
        return [payload]
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    return []


def _chunk_pages(
    pages_by_rel: dict[str, list[Path]], budget: int | None = None
) -> list[list[Path]]:
    """Split page images into command lines that fit the argv budget.

    Boundaries land between PDFs whenever a whole PDF still fits, because
    each extra chunk re-pays focr's multi-GB model load. A single PDF whose
    pages exceed the budget on their own is split across chunks.
    """
    # Late binding so tests can shrink the budget on the module.
    budget = _ARGV_CHAR_BUDGET if budget is None else budget
    chunks: list[list[Path]] = []
    current: list[Path] = []
    used = 0
    for paths in pages_by_rel.values():
        cost = sum(len(str(p)) + 1 for p in paths)
        if current and used + cost > budget:
            chunks.append(current)
            current, used = [], 0
        if cost <= budget:
            current.extend(paths)
            used += cost
            continue
        for path in paths:
            item = len(str(path)) + 1
            if current and used + item > budget:
                chunks.append(current)
                current, used = [], 0
            current.append(path)
            used += item
    if current:
        chunks.append(current)
    return chunks


def _stream_focr_chunk(
    command: str,
    chunk: list[Path],
    on_record: Callable[[dict[str, Any]], None],
) -> tuple[int, str]:
    """Run one `focr ocr-batch`, feeding records to on_record as they land.

    Returns (returncode, raw stdout) so the caller can fall back to whole-
    payload parsing: focr 0.7.2 buffers everything until exit, and only a
    future streaming build makes the incremental path pay off.

    encoding="utf-8" is explicit because the Windows default (cp1252)
    mangles non-ASCII OCR text. The read loop is a plain blocking
    iteration, so there is no select/fcntl (neither works on Windows
    pipes), and the process is always reaped before the caller removes the
    temporary directory holding its input.
    """
    proc = subprocess.Popen(
        _focr_argv(command, chunk),
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_focr_env(),
    )
    lines: list[str] = []
    try:
        for line in proc.stdout:
            lines.append(line)
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue  # partial or pretty-printed JSON: the caller re-parses
            for record in _records_from_payload(payload):
                if isinstance(record.get("image"), str):
                    on_record(record)
    except BaseException:
        proc.terminate()
        raise
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
        proc.wait()
    return proc.returncode, "".join(lines)


def _parse_focr_batch_results(stdout: str) -> dict[str, dict[str, Any]]:
    """Parse focr ocr-batch JSON from wrapper, array, or NDJSON output."""
    text = stdout.strip()
    if not text:
        raise ValueError("empty stdout from focr")

    payloads: list[Any] = []
    try:
        payloads.append(json.loads(text))
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip()
            if line:
                payloads.append(json.loads(line))

    records = [
        record
        for payload in payloads
        for record in _records_from_payload(payload)
    ]
    results = {
        record["image"]: record
        for record in records
        if isinstance(record.get("image"), str)
    }
    if not results:
        raise ValueError("no per-image results in focr JSON")
    return results


def run_ocr(
    root: Path,
    to_ocr: list[str],
    hashes: dict[str, str],
    defer_commit_rels: set[str] | None = None,
) -> list[ProcessingResult]:
    """Run focr on pending PDFs; return one ProcessingResult per PDF.

    All pages of all pending PDFs are rasterized to a temp dir and fed to
    as few `focr ocr-batch` invocations as the argv limit allows, so the
    multi-GB model is loaded once for the whole run instead of once per
    file. Each PDF is finalized the moment its last page comes back, so an
    interrupt, a crash, or a nonzero exit costs only the PDFs that had not
    finished: their sidecars go through the same temp-file, empty-
    rejection, tokenize, rehash, atomic-replace path as every other
    conversion, and ocr_done is staged by the orchestrator only from a
    successful result. A requested-OCR failure is a failed result and never
    falls through to the generic converter.
    """
    import tempfile

    import fitz

    defer_commit_rels = defer_commit_rels or set()

    if not to_ocr:
        print("\nOCR: nothing to do (all flagged PDFs already converted).")
        return []

    results: list[ProcessingResult] = []
    print(f"\nOCRing {len(to_ocr)} PDF(s) with focr...")
    with tempfile.TemporaryDirectory(prefix="focr-batch-") as tmp:
        tmp_dir = Path(tmp)

        # 1. Rasterize every page of every pending PDF, one thread per PDF
        # (each thread owns its own fitz document).
        def _rasterize(di: int, rel: str) -> list[Path]:
            print(f"\tRasterizing {rel}...", flush=True)
            paths: list[Path] = []
            doc = fitz.open(root / rel)
            try:
                for pi, page in enumerate(doc):
                    png = tmp_dir / f"d{di:04d}-p{pi:04d}.png"
                    page.get_pixmap(dpi=_OCR_RASTER_DPI).save(png)
                    paths.append(png)
            finally:
                doc.close()
            return paths

        def _fail_raster(rel: str, detail: str, shown: str) -> None:
            results.append(
                ProcessingResult(rel, STATUS_FAILED, "ocr", detail=detail)
            )
            print(f"\tERROR rasterizing {rel}: {shown}", flush=True)

        rasterized: dict[str, list[Path]] = {}
        with ThreadPoolExecutor(max_workers=_OCR_RASTER_MAX_WORKERS) as pool:
            futures = {
                pool.submit(_rasterize, di, rel): rel
                for di, rel in enumerate(to_ocr)
            }
            for future in as_completed(futures):
                rel = futures[future]
                try:
                    paths = future.result()
                except Exception as exc:
                    _fail_raster(rel, f"rasterizing failed: {exc}", str(exc))
                    continue
                if not paths:
                    _fail_raster(rel, "PDF has no pages", "PDF has no pages")
                    continue
                rasterized[rel] = paths

        # Page order follows to_ocr, not completion order, so the argv and
        # every progress line stay deterministic.
        pages_by_rel = {rel: rasterized[rel] for rel in to_ocr if rel in rasterized}
        page_paths = [p for paths in pages_by_rel.values() for p in paths]
        if not page_paths:
            return results

        chunks = _chunk_pages(pages_by_rel)
        print(
            f"\tRasterized {len(page_paths)} page(s) from {len(pages_by_rel)} "
            f"PDF(s); running focr ocr-batch "
            f"({len(chunks)} batch(es), model loaded once per batch)...",
            flush=True,
        )

        # 2. Feed the pages to focr, finalizing each PDF as it completes.
        page_of = {
            str(png): (rel, pi + 1, fi + 1)
            for fi, (rel, paths) in enumerate(pages_by_rel.items())
            for pi, png in enumerate(paths)
        }
        total_pages = len(page_paths)
        total_files = len(pages_by_rel)
        remaining = list(pages_by_rel)
        seen: dict[str, dict[str, Any]] = {}
        first_record_at = 0.0
        last_progress = 0.0

        def _remaining_failed(detail: str) -> list[ProcessingResult]:
            print(f"\tERROR: {detail}", flush=True)
            return results + [
                ProcessingResult(rel, STATUS_FAILED, "ocr", detail=detail)
                for rel in remaining
            ]

        def _finalize(rel: str) -> None:
            """Reassemble one PDF; it succeeds only if every page OCRed."""
            paths = pages_by_rel[rel]
            page_md: list[str] = []
            failed: list[str] = []
            for png in paths:
                r = seen.get(str(png))
                if r is not None and r.get("ok") and r.get("markdown") is not None:
                    page_md.append(r["markdown"])
                else:
                    err = (r or {}).get("error", "no result returned")
                    failed.append(f"{png.name}: {err}")
            remaining.remove(rel)
            if failed:
                detail = f"{len(failed)}/{len(paths)} page(s) failed: {failed[0]}"
                results.append(
                    ProcessingResult(rel, STATUS_FAILED, "ocr", detail=detail)
                )
                print(f"\tERROR OCRing {rel} ({detail})", flush=True)
                return
            result = _finalize_sidecar(
                root,
                root / rel,
                hashes[rel],
                "\n\n".join(page_md),
                "ocr",
                ocr_done=True,
                defer_commit=rel in defer_commit_rels,
            )
            results.append(result)
            if result.status == STATUS_CONVERTED:
                print(f"\t{rel} -> {converted_path(root / rel).name}", flush=True)
            else:
                print(f"\tERROR OCRing {rel}: {result.detail}", flush=True)

        def on_record(record: dict[str, Any]) -> None:
            """One page came back: report progress, finalize finished PDFs."""
            nonlocal first_record_at, last_progress
            image = record["image"]
            if image in seen:
                return
            seen[image] = record
            located = page_of.get(image)
            if located is None:
                return
            rel, page_number, file_number = located
            now = time.monotonic()
            if len(seen) == 1:
                first_record_at = now
            if (
                now - last_progress >= _OCR_PROGRESS_MIN_SECONDS
                or len(seen) == total_pages
            ):
                last_progress = now
                # Rate from page-to-page arrivals, not from the start of the
                # run: charging the one-time model load to page 1 would put
                # a wildly pessimistic ETA on the very first line.
                rate = ""
                if len(seen) > 1:
                    per_page = (now - first_record_at) / (len(seen) - 1)
                    eta = _format_duration(per_page * (total_pages - len(seen)))
                    rate = f" · {per_page:.0f}s/page · ETA {eta}"
                print(
                    f"\tpage {len(seen)}/{total_pages} (file {file_number}/"
                    f"{total_files}: {rel} p{page_number}){rate}",
                    flush=True,
                )
            if rel in remaining and all(str(p) in seen for p in pages_by_rel[rel]):
                _finalize(rel)

        command = _focr_command()
        chunk_errors: list[str] = []
        for index, chunk in enumerate(chunks, 1):
            if len(chunks) > 1:
                print(
                    f"\tfocr batch {index}/{len(chunks)}: {len(chunk)} page(s)",
                    flush=True,
                )
            try:
                with _Heartbeat(f"focr still running on {len(chunk)} page(s)"):
                    returncode, raw = _stream_focr_chunk(command, chunk, on_record)
            except FileNotFoundError:
                return _remaining_failed(_FOCR_NOT_FOUND)
            except KeyboardInterrupt:
                # Completed PDFs keep their sidecars; the orchestrator still
                # persists them, and the run exits nonzero for the rest.
                return _remaining_failed("interrupted before OCR finished")
            if returncode != 0:
                chunk_errors.append(f"focr ocr-batch exited {returncode}")
            if raw.strip() and any(str(png) not in seen for png in chunk):
                # focr 0.7.2 buffers the payload and may pretty-print it
                # across lines, so the streaming loop can see nothing.
                try:
                    for record in _parse_focr_batch_results(raw).values():
                        on_record(record)
                except (json.JSONDecodeError, ValueError, TypeError) as exc:
                    chunk_errors.append(f"parsing focr ocr-batch JSON: {exc}")

        # 3. Anything still unfinished failed. With a process-level error,
        # report it; otherwise report exactly which pages went missing.
        for rel in list(remaining):
            if chunk_errors:
                detail = "; ".join(dict.fromkeys(chunk_errors))
                remaining.remove(rel)
                results.append(
                    ProcessingResult(rel, STATUS_FAILED, "ocr", detail=detail)
                )
                print(f"\tERROR OCRing {rel}: {detail}", flush=True)
            else:
                _finalize(rel)
    return results


# ---------------------------------------------------------------------------
# Certification, staging, reconciliation, persistence
# ---------------------------------------------------------------------------


def is_certified_unchanged(
    root: Path,
    rel: str,
    fresh_hash: str,
    hash_index: dict[str, str],
    token_index: dict[str, int],
) -> bool:
    """True only for: fresh CRC32 equal to the certified hash, an existing
    sidecar, and a valid token row. File metadata is never a substitute."""
    if hash_index.get(rel) != fresh_hash:
        return False
    sidecar = converted_path(root / rel)
    if not sidecar.exists():
        return False
    return _rel(root, sidecar) in token_index


def stage_results(
    results: list[ProcessingResult],
    hash_index: dict[str, str],
    token_index: dict[str, int],
    ocr_index: dict[str, dict[str, str]],
) -> None:
    """Stage hash/token/OCR rows from successful conversions only."""
    for r in results:
        if r.status != STATUS_CONVERTED:
            continue
        hash_index[r.source_rel] = r.file_hash
        token_index[r.sidecar_rel] = r.tokens
        try:
            alternate_rel = str(other_style_path(Path(r.source_rel)))
        except ValueError:
            alternate_rel = None
        if alternate_rel is not None and alternate_rel != r.sidecar_rel:
            token_index.pop(alternate_rel, None)
        if r.ocr_done and r.source_rel in ocr_index:
            ocr_index[r.source_rel]["ocr_done"] = "true"


def reconcile_indexes(
    root: Path,
    discovered_rels: set[str],
    hash_index: dict[str, str],
    token_index: dict[str, int],
    ocr_index: dict[str, dict[str, str]],
    *,
    hashable_rels: set[str] | None = None,
    migration: MigrationStats | None = None,
    pending_sidecar_rels: set[str] | None = None,
) -> None:
    """Prune stale rows without deleting recoverable sidecar authority."""
    if hashable_rels is None:
        hashable_rels = set(discovered_rels)
    conflict_rels = (
        {conflict.source_rel for conflict in migration.conflicts}
        if migration is not None
        else set()
    )
    protected_rels = (discovered_rels - hashable_rels) | conflict_rels
    pending_sidecar_rels = pending_sidecar_rels or set()

    expected_sidecars: set[str] = set(pending_sidecar_rels)
    for rel in discovered_rels:
        try:
            preferred = converted_path(root / rel)
            alternate = other_style_path(root / rel)
        except ValueError:
            continue
        preferred_rel = _rel(root, preferred)
        alternate_rel = _rel(root, alternate)
        if rel in protected_rels:
            # Preserve both prior rows even when a candidate is missing: an
            # unhashable/unresolved source cannot be canonicalized safely.
            expected_sidecars.update((preferred_rel, alternate_rel))
            continue
        if preferred.exists():
            expected_sidecars.add(preferred_rel)
        if alternate.exists():
            expected_sidecars.add(alternate_rel)

    for rel in [r for r in hash_index if r not in discovered_rels]:
        del hash_index[rel]
    for rel in [r for r in token_index if r not in expected_sidecars]:
        del token_index[rel]
    discovered_pdfs = {
        rel for rel in discovered_rels if Path(rel).suffix.lower() == ".pdf"
    }
    for rel in [r for r in ocr_index if r not in discovered_pdfs]:
        del ocr_index[rel]


def _matches_disk(path: Path, text: str) -> bool:
    """True when path already holds exactly text. Content comparison only,
    never mtime; any read problem counts as a mismatch (so we write)."""
    try:
        return path.read_bytes() == text.encode("utf-8")
    except OSError:
        return False


def _write_if_changed(root: Path, name: str, text: str) -> str | None:
    """Write one prepared index serialization unless disk already matches."""
    if _matches_disk(root / name, text):
        return None
    try:
        atomic_write_text(root / name, text, newline="")
    except Exception as exc:
        return f"writing {name} failed: {type(exc).__name__}: {exc}"
    return None


def persist_indexes(
    root: Path,
    hash_index: dict[str, str],
    token_index: dict[str, int],
    ocr_index: dict[str, dict[str, str]],
    staged_results: list[ProcessingResult] | None = None,
) -> list[str]:
    """Atomically write the indexes: token, then OCR, then hash last.

    A write whose serialized content is byte-identical to the on-disk file
    is skipped (a skip is a success, not a failure). The hash index is the
    certification marker and is published only if both preceding writes
    succeeded: whether a run is interrupted between writes or a token/OCR
    write fails outright, the old hash index stays in place, so the next
    run reconverts instead of certifying sidecars whose token/OCR rows are
    stale. A withheld hash write is itself recorded in the returned errors.
    """
    errors: list[str] = []
    staged_results = [
        result
        for result in (staged_results or [])
        if result.staged_sidecar is not None
    ]

    def discard_uncommitted() -> None:
        for result in staged_results:
            if result.staged_sidecar is not None:
                discard_staged(result.staged_sidecar)

    def withhold_hash() -> list[str]:
        errors.append(
            f"withheld {HASH_INDEX_FILENAME} write (certification marker) "
            "because a preceding index write failed"
        )
        return errors

    token_text = serialize_token_index(token_index)
    token_error = _write_if_changed(root, TOKEN_INDEX_FILENAME, token_text)
    if token_error is not None:
        errors.append(token_error)

    ocr_text = serialize_ocr_index(ocr_index)
    ocr_error = _write_if_changed(root, OCR_INDEX_FILENAME, ocr_text)
    if ocr_error is not None:
        errors.append(ocr_error)

    if errors:
        discard_uncommitted()
        return withhold_hash()

    for result in staged_results:
        try:
            commit_staged(
                result.staged_sidecar,
                root / result.sidecar_rel,
            )
        except Exception as exc:
            errors.append(
                f"committing {result.sidecar_rel} failed: "
                f"{type(exc).__name__}: {exc}"
            )
    if errors:
        discard_uncommitted()
        return withhold_hash()

    hash_text = serialize_hash_index(hash_index)
    hash_error = _write_if_changed(root, HASH_INDEX_FILENAME, hash_text)
    if hash_error is not None:
        errors.append(hash_error)
    return errors


def summarize(
    results: list[ProcessingResult],
    token_index: dict[str, int],
    ocr_index: dict[str, dict[str, str]],
    index_errors: list[str],
    migration: MigrationStats | None = None,
) -> int:
    """Print the summary from ProcessingResult statuses; return exit code."""
    counts = {
        status: sum(1 for r in results if r.status == status)
        for status in (
            STATUS_UNCHANGED,
            STATUS_CONVERTED,
            STATUS_FAILED,
            STATUS_DEFERRED_FOR_OCR,
        )
    }
    needs_ocr = sum(
        1 for row in ocr_index.values() if row["verdict"] in NEEDS_OCR_VERDICTS
    )
    ocr_done = sum(1 for row in ocr_index.values() if row.get("ocr_done") == "true")
    total_tokens = sum(token_index.values())

    print(
        f"\nDone. {len(results)} office documents indexed: "
        f"{counts[STATUS_CONVERTED]} converted, {counts[STATUS_UNCHANGED]} unchanged, "
        f"{counts[STATUS_FAILED]} failed, {counts[STATUS_DEFERRED_FOR_OCR]} deferred for OCR."
    )
    print(
        f"PDFs classified: {len(ocr_index)}, of which {needs_ocr} flagged needs_ocr "
        f"({ocr_done} OCR-converted)."
    )
    if migration is not None and migration.total:
        print(
            f"Sidecar naming migration: {migration.renamed} renamed, "
            f"{migration.repaired} repaired, {migration.retokenized} re-tokenized, "
            f"{migration.resolved_conflicts} automatically resolved conflict(s), "
            f"{len(migration.conflicts)} unresolved conflict(s)."
        )
    print(f"Total tokens across converted files: {total_tokens:,}")
    print(
        f"Indices written to {HASH_INDEX_FILENAME}, {TOKEN_INDEX_FILENAME}, "
        f"and {OCR_INDEX_FILENAME}"
    )
    for r in results:
        if r.status == STATUS_FAILED:
            print(f"\tFAILED {r.source_rel}: {r.detail}")
    for err in index_errors:
        print(f"\tFAILED {err}")
    if counts[STATUS_FAILED] or index_errors or (
        migration is not None and migration.conflicts
    ):
        return 1
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert supported documents/email files and maintain hash/token indexes."
    )
    parser.add_argument(
        "--ocr",
        action="store_true",
        help="run focr on PDFs flagged needs_ocr (writes foo.pdf.md, tracked in "
        f"{OCR_INDEX_FILENAME})",
    )
    return parser.parse_args()


def main() -> int:
    global SIDECAR_DOTFILES, OCR_INT8

    args = parse_args()
    # Read the repo preferences before touching anything: defaulting on a
    # corrupted settings file in a dotfile-style repo would trigger a
    # silent mass rename, so fail loudly instead.
    try:
        SIDECAR_DOTFILES = repo_settings.read_sidecar_dotfiles()
        OCR_INT8 = repo_settings.read_ocr_int8()
    except repo_settings.RepoSettingsError as exc:
        print(f"ERROR: invalid repo settings: {exc}")
        print("Fix or delete the repo-root settings.json, then re-run.")
        return 1
    root = Path.cwd()
    caption_output_dir = root / CAPTION_OUTPUT_DIRNAME
    caption_output_dir.mkdir(parents=True, exist_ok=True)
    for child in caption_output_dir.iterdir():
        if child.is_file() or child.is_symlink():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)

    print("Additional features...")
    print(f"Caption cache directory: {caption_output_dir}")
    nd_vars = ("MATTERS_DB", "ND_API_KEY", "NDHELPER_URL")
    if all(os.getenv(var) for var in nd_vars):
        print("Netdocs access with\n\tuv run python nd.py -h")

    # 1. Load existing indices
    hash_index = load_hash_index(root)
    token_index = load_token_index(root)
    ocr_index = load_ocr_index(root)

    # 2. Discover source files; notice (but never convert) .mbx files
    sources, mbx_files = discover_sources(root)
    if mbx_files:
        print(
            f"\nNotice: {len(mbx_files)} .mbx file(s) found. The .mbx extension is "
            "ambiguous (Eudora/Outlook Express binary formats are incompatible "
            "with Unix mbox), so these are not converted. Convert them manually "
            "(e.g., export to .mbox or .eml) if their contents are needed:"
        )
        for p in mbx_files:
            print(f"\t{_rel(root, p)}")

    discovered_rels = {_rel(root, src) for src in sources}
    results: list[ProcessingResult] = []
    migration = MigrationStats()

    if not sources:
        print("\nNo office documents found.")
    else:
        # 3. Hash all source files in parallel; failures preserve prior state
        print(f"\nHashing {len(sources)} source file(s)...")
        hashes, hash_failures = hash_sources(root, sources)
        results.extend(hash_failures)
        for r in hash_failures:
            print(f"\tERROR hashing {r.source_rel}: {r.detail}")

        # 3b. Migrate/repair sidecar naming before classification so
        # classify_pdfs/pending_ocr_rels and the certify loop see
        # post-migration names.
        migration = migrate_sidecars(
            root, sources, hashes, hash_index, token_index, ocr_index
        )
        migration_excluded = migration.excluded_rels

        # 4. Classify new/changed PDFs (flags needs_ocr in .ocr_index.csv)
        pdf_rels = sorted(
            rel
            for rel in hashes
            if Path(rel).suffix.lower() == ".pdf"
            and rel not in migration_excluded
        )
        classify_failures = classify_pdfs(root, pdf_rels, hashes, ocr_index)
        results.extend(classify_failures)
        excluded = {r.source_rel for r in results} | migration_excluded

        pending_ocr = [
            rel
            for rel in pending_ocr_rels(root, ocr_index)
            if rel in hashes and rel not in excluded
        ]
        print(f"\nDocuments that may need OCR: {len(pending_ocr)}")
        for rel in pending_ocr:
            print(f"\t{rel}")

        # 4b. Pending-OCR PDFs never reach the generic converter. Without
        # --ocr they are deferred (consent required); with --ocr they go
        # through focr, and a requested-OCR failure stays a failure.
        if pending_ocr and not args.ocr:
            print(
                "\tAsk the user before running `uv run startup.py --ocr`; OCR can take a long time."
            )
            results.extend(
                ProcessingResult(rel, STATUS_DEFERRED_FOR_OCR, "ocr")
                for rel in pending_ocr
            )
        elif args.ocr and pending_ocr:
            results.extend(
                run_ocr(
                    root,
                    pending_ocr,
                    hashes,
                    migration.defer_sidecar_commit,
                )
            )
        handled = {r.source_rel for r in results} | migration_excluded

        # 5. Certify unchanged sources; convert the rest through the router.
        # Invariant: a PDF whose OCR verdict is in NEEDS_OCR_VERDICTS is
        # NEVER appended to to_convert, so the generic AnyDoc converter can
        # never overwrite OCR output regardless of index state.
        to_convert: list[Path] = []
        for src in sources:
            rel = _rel(root, src)
            if rel in handled or rel not in hashes:
                continue
            if is_certified_unchanged(root, rel, hashes[rel], hash_index, token_index):
                results.append(
                    ProcessingResult(
                        rel,
                        STATUS_UNCHANGED,
                        route_for(src),
                        _rel(root, converted_path(src)),
                        hashes[rel],
                    )
                )
                continue
            row = ocr_index.get(rel)
            if row is not None and row.get("verdict") in NEEDS_OCR_VERDICTS:
                sidecar = converted_path(src)
                sidecar_rel = _rel(root, sidecar)
                if (
                    sidecar.exists()
                    and row.get("ocr_done") == "true"
                    and row.get("hash") == hashes[rel]
                ):
                    # OCR output is current but uncertified (migrated
                    # sidecar or index drift): re-tokenize + certify;
                    # ocr_done stays "true", sidecar bytes untouched.
                    try:
                        tokens = count_tokens(sidecar)
                    except Exception as exc:
                        results.append(
                            ProcessingResult(
                                rel,
                                STATUS_FAILED,
                                "ocr",
                                sidecar_rel,
                                detail=(
                                    "re-tokenizing OCR sidecar failed: "
                                    f"{type(exc).__name__}: {exc}"
                                ),
                            )
                        )
                        continue
                    hash_index[rel] = hashes[rel]
                    token_index[sidecar_rel] = tokens
                    results.append(
                        ProcessingResult(
                            rel,
                            STATUS_UNCHANGED,
                            "ocr",
                            sidecar_rel,
                            hashes[rel],
                            tokens,
                        )
                    )
                else:
                    results.append(
                        ProcessingResult(rel, STATUS_DEFERRED_FOR_OCR, "ocr")
                    )
                continue
            to_convert.append(src)
        results.extend(
            convert_sources(
                root,
                to_convert,
                hashes,
                migration.defer_sidecar_commit,
            )
        )

        # 6. Stage successful results into the indexes
        stage_results(results, hash_index, token_index, ocr_index)

    # 7. Prune rows only for sources no longer discovered, then persist all
    # three indexes (token, OCR, hash last) even when nothing was found.
    reconcile_indexes(
        root,
        discovered_rels,
        hash_index,
        token_index,
        ocr_index,
        hashable_rels=set(hashes) if sources else set(),
        migration=migration,
        pending_sidecar_rels={
            result.sidecar_rel
            for result in results
            if result.staged_sidecar is not None
            and result.sidecar_rel is not None
        },
    )
    index_errors = persist_indexes(
        root, hash_index, token_index, ocr_index, results
    )

    # 8. Summary and exit status from ProcessingResult statuses
    return summarize(results, token_index, ocr_index, index_errors, migration)


if __name__ == "__main__":
    raise SystemExit(main())
