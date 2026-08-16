"""Certification, staging, index reconciliation, persistence, summary."""

from pathlib import Path

from fsio import atomic_write_text, commit_staged, discard_staged
from pdfcheck import NEEDS_OCR_VERDICTS, OCR_INDEX_FILENAME, serialize_ocr_index
from startup_lib.common import (
    HASH_INDEX_FILENAME,
    STATUS_CONVERTED,
    STATUS_DEFERRED_FOR_OCR,
    STATUS_FAILED,
    STATUS_UNCHANGED,
    TOKEN_INDEX_FILENAME,
    ProcessingResult,
    converted_path,
    other_style_path,
    _rel,
)
from startup_lib.indexes import serialize_hash_index, serialize_token_index
from startup_lib.migration import MigrationStats

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
