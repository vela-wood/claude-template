"""Convert supported documents/emails to Markdown sidecars and maintain
the hash, token, and OCR indexes for the working folder.

Orchestration only: routing and rendering live in document_conversion.py,
PDF classification in pdfcheck.py, atomic replacement in fsio.py.

Certification model: the hash index is written last and is the certification
marker; it is withheld entirely when any preceding index write fails, and it
carries the converter schema version so indexes written by an earlier
converter pipeline read as stale. A source is "unchanged" only when its
fresh CRC32 equals the previously certified hash AND its sidecar exists AND
it has a valid token row. Any hashing, classification, conversion,
tokenization, requested-OCR, sidecar-write, or index-write failure leaves
prior state in place, is reported, and produces a nonzero exit. Pending OCR
consent and skipped .mbx notices alone exit zero.
"""

import argparse
import csv
import io
import json
import os
import shutil
import subprocess
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tiktoken

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
)

load_repo_dotenv(__file__)

HASH_INDEX_FILENAME = ".hash_index.csv"
TOKEN_INDEX_FILENAME = ".token_index.csv"
CAPTION_OUTPUT_DIRNAME = "caption_cache"

# Converter schema version, persisted as the first line of .hash_index.csv
# ("#schema=N"). Bump whenever converter output changes (routing, renderer,
# or sidecar format) so every previously certified source reconverts once.
# Version 1 is the unversioned pre-migration (MarkItDown) index, whose plain
# "file,hash" file has no marker and therefore always reads as stale.
CONVERTER_SCHEMA_VERSION = 2

# Statuses for ProcessingResult
STATUS_UNCHANGED = "unchanged"
STATUS_CONVERTED = "converted"
STATUS_FAILED = "failed"
STATUS_DEFERRED_FOR_OCR = "deferred_for_ocr"

# Cap applies specifically to active converter calls; hashing and index
# work use separate default-sized pools.
CONVERSION_MAX_WORKERS = 4

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


def converted_path(source: Path) -> Path:
    """Return the expected converted-file path for a source file."""
    if source.suffix.lower() in SOURCE_SUFFIXES:
        return source.parent / f"{source.name}.md"
    raise ValueError(f"Unsupported source type: {source}")


def _rel(root: Path, path: Path) -> str:
    return str(path.relative_to(root))


# ---------------------------------------------------------------------------
# Index I/O (three typed loaders/serializers; all UTF-8, atomic replacement)
# ---------------------------------------------------------------------------


def load_hash_index(root: Path) -> dict[str, str]:
    """Load .hash_index.csv → {source_relative_path: hash}.

    The first line must be the current schema marker
    "#schema=<CONVERTER_SCHEMA_VERSION>". A missing or mismatched marker —
    including any index written by the pre-migration pipeline, which has no
    marker — makes the whole index stale: an empty index is returned, no
    source certifies as unchanged, and everything reconverts once.
    """
    index_path = root / HASH_INDEX_FILENAME
    index: dict[str, str] = {}
    if not index_path.exists():
        return index
    with open(index_path, newline="", encoding="utf-8") as f:
        if f.readline().rstrip("\r\n") != f"#schema={CONVERTER_SCHEMA_VERSION}":
            return index
        for row in csv.DictReader(f):
            index[row["file"]] = row["hash"]
    return index


def save_hash_index(root: Path, index: dict[str, str]) -> None:
    """Write .hash_index.csv from {source_relative_path: hash}.

    The first line is the schema marker "#schema=<CONVERTER_SCHEMA_VERSION>"
    that load_hash_index requires; the CSV header and rows follow.
    """
    buf = io.StringIO()
    buf.write(f"#schema={CONVERTER_SCHEMA_VERSION}\r\n")
    writer = csv.writer(buf)
    writer.writerow(["file", "hash"])
    for rel_path in sorted(index):
        writer.writerow([rel_path, index[rel_path]])
    atomic_write_text(root / HASH_INDEX_FILENAME, buf.getvalue(), newline="")


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
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["file", "tokens"])
    for rel_path in sorted(index):
        writer.writerow([rel_path, index[rel_path]])
    atomic_write_text(root / TOKEN_INDEX_FILENAME, buf.getvalue(), newline="")


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
) -> ProcessingResult:
    """Temp-file write, empty rejection, tokenize, rehash, atomic replace.

    Only a fully successful result may later have its hash/token rows
    staged by the orchestrator.
    """
    rel = _rel(root, src)
    out = converted_path(src)
    conv_rel = _rel(root, out)
    tmp = None
    try:
        if not text or not text.strip():
            raise ValueError(f"empty conversion output for {src.name}")
        tmp = stage_text(out, text)
        tokens = count_tokens(tmp)
        if hash_file(src) != pre_hash:
            raise ValueError("source changed during conversion")
        commit_staged(tmp, out)
        tmp = None
        return ProcessingResult(
            rel, STATUS_CONVERTED, route, conv_rel, pre_hash, tokens, ocr_done
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
    root: Path, to_convert: list[Path], hashes: dict[str, str]
) -> list[ProcessingResult]:
    """Convert sources through the router with at most 4 active converters."""
    if not to_convert:
        return []
    print(f"\nConverting {len(to_convert)} file(s)...")
    results: list[ProcessingResult] = []

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
        return _finalize_sidecar(root, src, hashes[rel], text, route_for(src))

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


def _parse_focr_batch_results(stdout: str) -> dict[str, dict[str, Any]]:
    """Parse focr ocr-batch JSON from wrapper, array, or NDJSON output."""

    def records_from_payload(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict) and isinstance(payload.get("results"), list):
            return [r for r in payload["results"] if isinstance(r, dict)]
        if isinstance(payload, dict) and isinstance(payload.get("image"), str):
            return [payload]
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        return []

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
        for record in records_from_payload(payload)
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
    root: Path, to_ocr: list[str], hashes: dict[str, str]
) -> list[ProcessingResult]:
    """Run focr on pending PDFs; return one ProcessingResult per PDF.

    All pages of all pending PDFs are rasterized to a temp dir and fed to a
    single `focr ocr-batch` invocation, so the multi-GB model is loaded once
    for the whole run instead of once per file. Per-PDF output goes through
    the same temp-file, empty-rejection, tokenize, rehash, atomic-replace
    path as every other conversion; ocr_done is staged by the orchestrator
    only from a successful result. A requested-OCR failure is a failed
    result and never falls through to the generic converter.
    """
    import tempfile

    import fitz

    if not to_ocr:
        print("\nOCR: nothing to do (all flagged PDFs already converted).")
        return []

    def _all_failed(detail: str) -> list[ProcessingResult]:
        return [
            ProcessingResult(rel, STATUS_FAILED, "ocr", detail=detail)
            for rel in to_ocr
        ]

    results: list[ProcessingResult] = []
    print(f"\nOCRing {len(to_ocr)} PDF(s) with focr (one batched model load)...")
    with tempfile.TemporaryDirectory(prefix="focr-batch-") as tmp:
        tmp_dir = Path(tmp)

        # 1. Rasterize every page of every pending PDF.
        pages_by_rel: dict[str, list[Path]] = {}
        for di, rel in enumerate(to_ocr):
            try:
                doc = fitz.open(root / rel)
                paths: list[Path] = []
                for pi, page in enumerate(doc):
                    png = tmp_dir / f"d{di:04d}-p{pi:04d}.png"
                    page.get_pixmap(dpi=_OCR_RASTER_DPI).save(png)
                    paths.append(png)
                doc.close()
            except Exception as exc:
                results.append(
                    ProcessingResult(
                        rel, STATUS_FAILED, "ocr", detail=f"rasterizing failed: {exc}"
                    )
                )
                print(f"\tERROR rasterizing {rel}: {exc}")
                continue
            if not paths:
                results.append(
                    ProcessingResult(
                        rel, STATUS_FAILED, "ocr", detail="PDF has no pages"
                    )
                )
                print(f"\tERROR rasterizing {rel}: PDF has no pages")
                continue
            pages_by_rel[rel] = paths

        page_paths = [p for paths in pages_by_rel.values() for p in paths]
        if not page_paths:
            return results
        print(
            f"\tRasterized {len(page_paths)} page(s) from {len(pages_by_rel)} PDF(s); running focr ocr-batch..."
        )

        # 2. One focr process for the whole batch. stderr passes through so
        # focr's per-page progress stays visible; stdout carries the JSON.
        remaining = list(pages_by_rel)

        def _remaining_failed(detail: str) -> list[ProcessingResult]:
            print(f"\tERROR: {detail}")
            return results + [
                ProcessingResult(rel, STATUS_FAILED, "ocr", detail=detail)
                for rel in remaining
            ]

        try:
            proc = subprocess.run(
                ["focr", "ocr-batch", *map(str, page_paths), "--json"],
                stdout=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError:
            return _remaining_failed(
                "focr command not found. Install Franken OCR from "
                "https://github.com/Dicklesworthstone/franken_ocr and make sure "
                "`focr` is on PATH."
            )
        if proc.returncode != 0:
            return _remaining_failed(f"focr ocr-batch exited {proc.returncode}")
        try:
            focr_results = _parse_focr_batch_results(proc.stdout)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            return _remaining_failed(f"parsing focr ocr-batch JSON: {exc}")

        # 3. Reassemble per-PDF markdown; a PDF succeeds only when every
        # page OCRed, then follows the standard sidecar-finalization path.
        for rel, paths in pages_by_rel.items():
            page_md: list[str] = []
            failed: list[str] = []
            for png in paths:
                r = focr_results.get(str(png))
                if r is not None and r.get("ok") and r.get("markdown") is not None:
                    page_md.append(r["markdown"])
                else:
                    err = (r or {}).get("error", "no result returned")
                    failed.append(f"{png.name}: {err}")
            if failed:
                detail = f"{len(failed)}/{len(paths)} page(s) failed: {failed[0]}"
                results.append(
                    ProcessingResult(rel, STATUS_FAILED, "ocr", detail=detail)
                )
                print(f"\tERROR OCRing {rel} ({detail})")
                continue
            result = _finalize_sidecar(
                root,
                root / rel,
                hashes[rel],
                "\n\n".join(page_md),
                "ocr",
                ocr_done=True,
            )
            results.append(result)
            if result.status == STATUS_CONVERTED:
                print(f"\t{rel} -> {converted_path(root / rel).name}")
            else:
                print(f"\tERROR OCRing {rel}: {result.detail}")
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
    sidecar, and a valid token row. File metadata is never a substitute.

    hash_index must come from load_hash_index, which returns an empty index
    for any file lacking the current CONVERTER_SCHEMA_VERSION marker, so
    sidecars produced by an earlier converter pipeline never certify and
    every source reconverts once after a converter migration."""
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
        if r.ocr_done and r.source_rel in ocr_index:
            ocr_index[r.source_rel]["ocr_done"] = "true"


def reconcile_indexes(
    root: Path,
    discovered_rels: set[str],
    hash_index: dict[str, str],
    token_index: dict[str, int],
    ocr_index: dict[str, dict[str, str]],
) -> None:
    """Prune rows only for sources that are no longer discovered.

    Discovery determines existence; a discovered-but-unhashable source keeps
    its prior hash, token, and OCR rows and any existing sidecar.
    """
    expected_sidecars = set()
    for rel in discovered_rels:
        try:
            expected_sidecars.add(_rel(root, converted_path(root / rel)))
        except ValueError:
            pass
    for rel in [r for r in hash_index if r not in discovered_rels]:
        del hash_index[rel]
    for rel in [r for r in token_index if r not in expected_sidecars]:
        del token_index[rel]
    discovered_pdfs = {
        rel for rel in discovered_rels if Path(rel).suffix.lower() == ".pdf"
    }
    for rel in [r for r in ocr_index if r not in discovered_pdfs]:
        del ocr_index[rel]


def persist_indexes(
    root: Path,
    hash_index: dict[str, str],
    token_index: dict[str, int],
    ocr_index: dict[str, dict[str, str]],
) -> list[str]:
    """Atomically write the indexes: token, then OCR, then hash last.

    The hash index is the certification marker and is published only if
    both preceding writes succeeded: whether a run is interrupted between
    writes or a token/OCR write fails outright, the old hash index stays in
    place, so the next run reconverts instead of certifying sidecars whose
    token/OCR rows are stale. A withheld hash write is itself recorded in
    the returned errors.
    """
    errors: list[str] = []
    for name, save in (
        (TOKEN_INDEX_FILENAME, lambda: save_token_index(root, token_index)),
        (OCR_INDEX_FILENAME, lambda: save_ocr_index(root, ocr_index)),
    ):
        try:
            save()
        except Exception as exc:
            errors.append(f"writing {name} failed: {type(exc).__name__}: {exc}")
    if errors:
        errors.append(
            f"withheld {HASH_INDEX_FILENAME} write (certification marker) "
            "because a preceding index write failed"
        )
        return errors
    try:
        save_hash_index(root, hash_index)
    except Exception as exc:
        errors.append(
            f"writing {HASH_INDEX_FILENAME} failed: {type(exc).__name__}: {exc}"
        )
    return errors


def summarize(
    results: list[ProcessingResult],
    token_index: dict[str, int],
    ocr_index: dict[str, dict[str, str]],
    index_errors: list[str],
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
    if counts[STATUS_FAILED] or index_errors:
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
    args = parse_args()
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

    if not sources:
        print("\nNo office documents found.")
    else:
        # 3. Hash all source files in parallel; failures preserve prior state
        print(f"\nHashing {len(sources)} source file(s)...")
        hashes, hash_failures = hash_sources(root, sources)
        results.extend(hash_failures)
        for r in hash_failures:
            print(f"\tERROR hashing {r.source_rel}: {r.detail}")

        # 4. Classify new/changed PDFs (flags needs_ocr in .ocr_index.csv)
        pdf_rels = sorted(
            rel for rel in hashes if Path(rel).suffix.lower() == ".pdf"
        )
        classify_failures = classify_pdfs(root, pdf_rels, hashes, ocr_index)
        results.extend(classify_failures)
        excluded = {r.source_rel for r in results}

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
        elif args.ocr:
            results.extend(run_ocr(root, pending_ocr, hashes))
        handled = {r.source_rel for r in results}

        # 5. Certify unchanged sources; convert the rest through the router
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
            else:
                to_convert.append(src)
        results.extend(convert_sources(root, to_convert, hashes))

        # 6. Stage successful results into the indexes
        stage_results(results, hash_index, token_index, ocr_index)

    # 7. Prune rows only for sources no longer discovered, then persist all
    # three indexes (token, OCR, hash last) even when nothing was found.
    reconcile_indexes(root, discovered_rels, hash_index, token_index, ocr_index)
    index_errors = persist_indexes(root, hash_index, token_index, ocr_index)

    # 8. Summary and exit status from ProcessingResult statuses
    return summarize(results, token_index, ocr_index, index_errors)


if __name__ == "__main__":
    raise SystemExit(main())
