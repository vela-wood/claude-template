"""Sidecar-style migration and repair.

Resolves which of the two possible sidecar names (foo.docx.md vs
.foo.docx.md) is authoritative from the physical bytes plus the stored
token rows, and preserves anything it cannot decide.
"""

import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from pdfcheck import NEEDS_OCR_VERDICTS
from startup_lib import common
from startup_lib.common import converted_path, other_style_path, _rel

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
                        token_index[preferred_rel] = common.count_tokens(preferred)
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
                    token_index[preferred_rel] = common.count_tokens(preferred)
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

