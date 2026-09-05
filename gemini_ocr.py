"""Cloud OCR with Gemini for one or many PDFs, in parallel.

Writes the same `.pdf.md` sidecars and index rows as `startup.py --ocr`,
so a later `uv run startup.py` reports the files unchanged and never
sends them through the generic PDF converter.

Every page image is uploaded to Google. Get the user's consent per matter.

    uv run gemini_ocr.py PATH [PATH ...] [--concurrency N]
                         [--thinking low|medium|high] [--force] [--all]
                         [--dry-run]
"""

import argparse
import asyncio
import os
import sys
from enum import Enum
from pathlib import Path

import repo_settings
from netdocs.env import load_repo_dotenv
from pdfcheck import NEEDS_OCR_VERDICTS, load_ocr_index
from startup_lib import common
from startup_lib.certify import persist_indexes, stage_results
from startup_lib.common import STATUS_FAILED, converted_path, other_style_path
from startup_lib.convert import classify_pdfs
from startup_lib.discovery import discover_sources, hash_sources
from startup_lib.gemini_client import ENV_KEY, MODEL, GeminiPageOcr, ThinkingLevel
from startup_lib.gemini_ocr import (
    DEFAULT_CONCURRENCY,
    MISSING_KEY_MESSAGE,
    OcrRun,
    estimate_cost_usd,
    format_totals,
    interrupted_results,
    ocr_pdfs,
)
from startup_lib.indexes import load_hash_index, load_token_index
from startup_lib.progress import Heartbeat

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NO_KEY = 2

PDF_SUFFIX = ".pdf"
_RATE_LIMIT_CODE = "429"

class Scope(Enum):
    NEEDS_OCR = "needs_ocr"  # default: only PDFs classified as scans
    ALL = "all"


class Rerun(Enum):
    SKIP_DONE = "skip_done"
    FORCE = "force"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            f"OCR PDFs with {MODEL}, many pages in parallel. Page images are "
            "uploaded to Google. Writes the same sidecars and indexes as "
            "`startup.py --ocr`."
        )
    )
    parser.add_argument("paths", nargs="+", help="PDF files or folders (under cwd)")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"in-flight page requests across all files (default {DEFAULT_CONCURRENCY})",
    )
    parser.add_argument(
        "--thinking",
        choices=[level.value for level in ThinkingLevel],
        default=ThinkingLevel.LOW.value,
        help="model reasoning depth (default low)",
    )
    parser.add_argument(
        "--force", action="store_true", help="re-OCR PDFs that already have OCR output"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="in folders, OCR every PDF, not only those classified as scans",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be sent and the estimated cost; contact nothing",
    )
    return parser.parse_args(argv)


def _load_env() -> None:
    """Test seam: the real .env has the key and load_dotenv overrides."""
    load_repo_dotenv(__file__)


def _make_client(key: str, level: ThinkingLevel) -> GeminiPageOcr:
    """Test seam: replaced with a fake page OCR."""
    return GeminiPageOcr(key, level)


def collect_pdfs(root: Path, paths: list[str]) -> tuple[list[Path], set[str], list[str]]:
    """(pdf paths, rels named explicitly as files, errors)."""
    pdfs: dict[str, Path] = {}
    explicit: set[str] = set()
    errors: list[str] = []
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            errors.append(f"{raw}: does not exist")
            continue
        resolved = path.resolve()
        try:
            rel = str(resolved.relative_to(root.resolve()))
        except ValueError:
            errors.append(
                f"{raw}: outside the working folder; run from the matter folder "
                "that contains the file"
            )
            continue

        if resolved.is_dir():
            sources, _ = discover_sources(resolved)
            for src in sources:
                if src.suffix.lower() == PDF_SUFFIX:
                    pdfs[str(src.relative_to(root.resolve()))] = src
            continue
        if resolved.suffix.lower() != PDF_SUFFIX:
            errors.append(f"{raw}: not a PDF")
            continue
        pdfs[rel] = resolved
        explicit.add(rel)
    return [root / rel for rel in sorted(pdfs)], explicit, errors


def select_targets(
    root: Path,
    rels: list[str],
    explicit: set[str],
    hashes: dict[str, str],
    ocr_index: dict[str, dict[str, str]],
    scope: Scope,
    rerun: Rerun,
) -> list[str]:
    """Filter candidates, printing one reason per skipped file."""
    targets: list[str] = []
    for rel in rels:
        row = ocr_index.get(rel)
        if row is None:
            continue  # classification failed; reported as a failure already
        done = (
            row.get("ocr_done") == "true"
            and row.get("hash") == hashes[rel]
            and converted_path(root / rel).exists()
        )
        if rerun is Rerun.SKIP_DONE and done:
            print(f"\tskip {rel}: OCR output already current (use --force)")
            continue
        if (
            rel not in explicit
            and scope is Scope.NEEDS_OCR
            and row["verdict"] not in NEEDS_OCR_VERDICTS
        ):
            print(f"\tskip {rel}: {row['verdict']} (use --all or name the file)")
            continue
        if other_style_path(root / rel).exists():
            print(
                f"\tskip {rel}: other-style sidecar exists; run `uv run startup.py` "
                "first to migrate"
            )
            continue
        targets.append(rel)
    return targets


def _page_count(row: dict[str, str]) -> int:
    try:
        return int(row.get("pages") or 0)
    except ValueError:
        return 0


def _print_dry_run(targets: list[str], ocr_index: dict[str, dict[str, str]]) -> None:
    total = 0
    for rel in targets:
        pages = _page_count(ocr_index[rel])
        total += pages
        print(f"\t{rel}: {pages} page(s)")
    print(
        f"Dry run: {len(targets)} PDF(s), {total} page(s) would be processed with "
        f"{MODEL}; estimated cost ${estimate_cost_usd(total):.2f}."
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        common.SIDECAR_DOTFILES = repo_settings.read_sidecar_dotfiles()
    except repo_settings.RepoSettingsError as exc:
        print(f"ERROR: invalid repo settings: {exc}")
        print("Fix or delete the repo-root settings.json, then re-run.")
        return EXIT_FAIL

    _load_env()
    key = os.environ.get(ENV_KEY, "").strip() or None
    if key is None and not args.dry_run:
        print(MISSING_KEY_MESSAGE, file=sys.stderr)
        return EXIT_NO_KEY

    root = Path.cwd()
    pdfs, explicit, path_errors = collect_pdfs(root, args.paths)
    for err in path_errors:
        print(f"ERROR: {err}")
    if path_errors:
        return EXIT_FAIL
    if not pdfs:
        print("No PDFs found.")
        return EXIT_OK

    hashes, hash_failures = hash_sources(root, pdfs)
    hash_index = load_hash_index(root)
    token_index = load_token_index(root)
    ocr_index = load_ocr_index(root)
    classify_failures = classify_pdfs(root, sorted(hashes), hashes, ocr_index)

    scope = Scope.ALL if args.all else Scope.NEEDS_OCR
    rerun = Rerun.FORCE if args.force else Rerun.SKIP_DONE
    targets = select_targets(root, sorted(hashes), explicit, hashes, ocr_index, scope, rerun)

    if args.dry_run:
        _print_dry_run(targets, ocr_index)
        return EXIT_OK

    run = OcrRun(total_pages=sum(_page_count(ocr_index[rel]) for rel in targets))
    interrupted = []
    if targets:
        level = ThinkingLevel(args.thinking)
        print(
            f"\nOCRing {len(targets)} PDF(s), {run.total_pages} page(s) with {MODEL} "
            f"(concurrency {args.concurrency})..."
        )
        try:
            with Heartbeat("Gemini OCR still running"):
                asyncio.run(
                    ocr_pdfs(root, targets, hashes, _make_client(key, level), args.concurrency, run)
                )
        except KeyboardInterrupt:
            interrupted = interrupted_results(targets, run)
    else:
        print("Nothing to OCR.")

    # Never reconcile_indexes here: it prunes rows for anything outside the
    # discovered set, and this CLI did not walk the whole tree.
    results = hash_failures + classify_failures + run.results + interrupted
    stage_results(results, hash_index, token_index, ocr_index)
    index_errors = persist_indexes(root, hash_index, token_index, ocr_index)

    print(format_totals(run))
    failures = [r for r in results if r.status == STATUS_FAILED]
    for r in failures:
        print(f"\tFAILED {r.source_rel}: {r.detail}")
    for err in index_errors:
        print(f"\tFAILED {err}")
    if any(_RATE_LIMIT_CODE in r.detail for r in failures):
        print("Hint: 429 rate limits seen; lower --concurrency.")

    if failures or index_errors:
        return EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
