"""OCR for the PDFs classification flagged needs_ocr: `startup.py --ocr`.

Thin glue over the Gemini driver (startup_lib/gemini_ocr.py), which owns
rasterizing, fan-out, and sidecar assembly. This module only adapts the
driver to run_ocr's contract: one ProcessingResult per pending PDF, a key
check that fails every PDF with the same setup message, and Ctrl+C that
keeps the PDFs already finished.
"""

import asyncio
import os
from pathlib import Path

from startup_lib.common import STATUS_FAILED, ProcessingResult
from startup_lib.gemini_client import ENV_KEY, GeminiPageOcr, ThinkingLevel
from startup_lib.gemini_ocr import (
    DEFAULT_CONCURRENCY,
    MISSING_KEY_MESSAGE,
    MODEL,
    ROUTE,
    OcrRun,
    count_pages,
    format_totals,
    interrupted_results,
    ocr_pdfs,
)
from startup_lib.progress import Heartbeat

# Low is enough for transcription and is the cheapest; the standalone
# gemini_ocr.py CLI exposes the other levels for hard scans.
THINKING_LEVEL = ThinkingLevel.LOW


def _make_client(key: str, level: ThinkingLevel) -> GeminiPageOcr:
    """Test seam: replaced with a fake page OCR."""
    return GeminiPageOcr(key, level)


def _all_failed(to_ocr: list[str], detail: str) -> list[ProcessingResult]:
    return [ProcessingResult(rel, STATUS_FAILED, ROUTE, detail=detail) for rel in to_ocr]


def run_ocr(
    root: Path,
    to_ocr: list[str],
    hashes: dict[str, str],
    defer_commit_rels: set[str] | None = None,
) -> list[ProcessingResult]:
    """OCR pending PDFs with Gemini; return one ProcessingResult per PDF.

    Pages are rasterized one at a time and uploaded concurrently; each PDF
    is finalized the moment its last page comes back, so an interrupt or a
    failure costs only the PDFs that had not finished. Their sidecars go
    through the same temp-file, empty-rejection, tokenize, rehash,
    atomic-replace path as every other conversion, and ocr_done is staged
    by the orchestrator only from a successful result. A requested-OCR
    failure is a failed result and never falls through to the generic
    converter.
    """
    if not to_ocr:
        print("\nOCR: nothing to do (all flagged PDFs already converted).")
        return []

    key = os.environ.get(ENV_KEY, "").strip()
    if not key:
        print(f"\n{MISSING_KEY_MESSAGE}")
        return _all_failed(to_ocr, f"{ENV_KEY} is not set")

    run = OcrRun(total_pages=count_pages(root, to_ocr))
    print(
        f"\nOCRing {len(to_ocr)} PDF(s), {run.total_pages} page(s) with {MODEL} "
        f"(concurrency {DEFAULT_CONCURRENCY}); page images are uploaded to Google..."
    )
    interrupted: list[ProcessingResult] = []
    try:
        with Heartbeat("Gemini OCR still running"):
            asyncio.run(
                ocr_pdfs(
                    root,
                    to_ocr,
                    hashes,
                    _make_client(key, THINKING_LEVEL),
                    DEFAULT_CONCURRENCY,
                    run,
                    defer_commit_rels,
                )
            )
    except KeyboardInterrupt:
        # Completed PDFs keep their sidecars; the orchestrator still
        # persists them, and the run exits nonzero for the rest.
        interrupted = interrupted_results(to_ocr, run)
        for result in interrupted:
            print(f"\tERROR OCRing {result.source_rel}: {result.detail}", flush=True)

    print(f"\t{format_totals(run)}", flush=True)
    return run.results + interrupted
