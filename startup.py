"""Convert supported documents/emails to Markdown sidecars and maintain
the hash, token, and OCR indexes for the working folder.

Entry point only: `uv run startup.py`. The implementation lives in
startup_lib (common, indexes, discovery, migration, convert, ocr,
certify); routing and rendering live in document_conversion.py, PDF
classification in pdfcheck.py, atomic replacement in fsio.py.

Certification model: the hash index is written last and is the certification
marker and is withheld entirely when any preceding index write fails. A source
is "unchanged" only when its fresh CRC32 equals the previously certified hash
AND its sidecar exists AND it has a valid token row. Any hashing,
classification, conversion, tokenization, requested-OCR, sidecar-write, or
index-write failure leaves prior state in place, is reported, and produces a
nonzero exit. Pending OCR consent and skipped .mbx notices alone exit zero.
"""

import argparse
import os
import shutil
from pathlib import Path

import repo_settings
from document_conversion import route_for
from netdocs.env import load_repo_dotenv
from pdfcheck import (
    NEEDS_OCR_VERDICTS,
    OCR_INDEX_FILENAME,
    load_ocr_index,
    save_ocr_index,
)
from startup_lib import common
from startup_lib.certify import (
    is_certified_unchanged,
    persist_indexes,
    reconcile_indexes,
    stage_results,
    summarize,
)
from startup_lib.common import (
    CAPTION_OUTPUT_DIRNAME,
    HASH_INDEX_FILENAME,
    STATUS_DEFERRED_FOR_OCR,
    STATUS_FAILED,
    STATUS_UNCHANGED,
    TOKEN_INDEX_FILENAME,
    ProcessingResult,
    converted_path,
    _rel,
)
from startup_lib.convert import classify_pdfs, convert_sources, pending_ocr_rels
from startup_lib.discovery import discover_sources, hash_sources
from startup_lib.indexes import load_hash_index, load_token_index
from startup_lib.migration import MigrationStats, migrate_sidecars
from startup_lib.ocr import run_ocr

load_repo_dotenv(__file__)

# The names other tools may reach for on this module. Everything else lives
# in startup_lib; patch a function where its consumer looks it up, not here,
# because these bindings are copies rather than the live definitions.
__all__ = [
    "HASH_INDEX_FILENAME",
    "OCR_INDEX_FILENAME",
    "TOKEN_INDEX_FILENAME",
    "converted_path",
    "load_ocr_index",
    "main",
    "run_ocr",
    "save_ocr_index",
]

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
    args = parse_args()
    # Read the repo preferences before touching anything: defaulting on a
    # corrupted settings file in a dotfile-style repo would trigger a
    # silent mass rename, so fail loudly instead.
    try:
        common.SIDECAR_DOTFILES = repo_settings.read_sidecar_dotfiles()
        common.OCR_INT8 = repo_settings.read_ocr_int8()
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
                        tokens = common.count_tokens(sidecar)
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
