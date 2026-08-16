"""PDF classification, sidecar production, and the converter pool."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from document_conversion import convert_to_markdown, route_for
from fsio import commit_staged, discard_staged, stage_text
from pdfcheck import NEEDS_OCR_VERDICTS, classify_pdf, index_row
from startup_lib import common
from startup_lib.common import (
    CONVERSION_MAX_WORKERS,
    STATUS_CONVERTED,
    STATUS_FAILED,
    ProcessingResult,
    converted_path,
    _rel,
)

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
        tokens = common.count_tokens(tmp)
        if common.hash_file(src) != pre_hash:
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
