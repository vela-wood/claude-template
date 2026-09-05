"""Cloud OCR driver: rasterize pages, fan requests out, assemble sidecars.

Mirrors run_ocr's contract (startup_lib/ocr.py): one ProcessingResult per
PDF, all-or-nothing per file, each file finalized the moment its last page
lands, so finished files survive an interrupt.

    producer (loop thread)              fitz thread         Gemini
    ─────────────────────               ───────────         ──────
    for each PDF ──open_doc──────────►  fitz.open
      for each page:
        sem.acquire()   ◄── back-pressure: at most `concurrency` JPEGs alive
        render_page ─────────────────►  get_pixmap → jpeg
        create_task(_page_task) ─────────────────────────►  generate_content
                                                            ◄── markdown
        _page_task: store text, sem.release(), pending -= 1
        pending == 0 → _finish_file → _finalize_sidecar (worker thread)
"""

import asyncio
import functools
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol

from startup_lib.common import (
    STATUS_CONVERTED,
    STATUS_FAILED,
    ProcessingResult,
    converted_path,
)
from startup_lib.convert import _finalize_sidecar
from startup_lib.gemini_client import (
    MODEL,
    USD_PER_M_INPUT,
    USD_PER_M_OUTPUT,
    PageFailed,
    PageText,
)
from startup_lib.ocr import _format_duration

ROUTE = "gemini-ocr"

# Tokens are fixed by media_resolution, not pixels, so DPI and format only
# affect upload bytes: 200 dpi JPEG q85 is ~0.3-0.6 MB per scanned page.
# RGB is kept for stamps and ink colour.
RASTER_DPI = 200
_MAX_LONG_SIDE_PX = 4000  # oversize drawing pages: stay far under 20 MB
_RASTER_FORMAT = "jpeg"
_JPEG_QUALITY = 85

# In-flight requests across ALL files.
DEFAULT_CONCURRENCY = 32
_PROGRESS_MIN_SECONDS = 1.0

PAGE_MARKERS = True
_PROVENANCE_MARKER = (
    "<!-- OCR by {model} on {date}; {pages} page(s); media_resolution high -->"
)
_PAGE_MARKER = "<!-- page {number} -->"
_PAGE_SEPARATOR = "\n\n"

# Dry-run estimate only. Measured on the 10-page scan+ocr corpus
# (2026-09-05, thinking low): 1,277 in / 457 out per page.
_EST_INPUT_TOKENS_PER_PAGE = 1300
_EST_OUTPUT_TOKENS_PER_PAGE = 500


class PageOcr(Protocol):
    async def ocr_page(self, image: bytes) -> PageText: ...


@dataclass
class Usage:
    prompt_tokens: int = 0
    output_tokens: int = 0
    pages: int = 0

    def add(self, page: PageText) -> None:
        self.prompt_tokens += page.prompt_tokens
        self.output_tokens += page.output_tokens
        self.pages += 1

    def cost_usd(self) -> float:
        return _cost_usd(self.prompt_tokens, self.output_tokens)


@dataclass
class OcrRun:
    total_pages: int
    results: list[ProcessingResult] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    done_pages: int = 0
    aborted: bool = False
    started: float = field(default_factory=time.monotonic)
    _last_progress: float = 0.0


@dataclass
class _FileJob:
    rel: str
    pre_hash: str
    total: int = 0
    pages: list[str | None] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    pending: int = 0
    started: float = field(default_factory=time.monotonic)
    usage: Usage = field(default_factory=Usage)


def _cost_usd(prompt_tokens: int, output_tokens: int) -> float:
    return (
        prompt_tokens * USD_PER_M_INPUT + output_tokens * USD_PER_M_OUTPUT
    ) / 1_000_000


def estimate_cost_usd(pages: int) -> float:
    return _cost_usd(
        pages * _EST_INPUT_TOKENS_PER_PAGE, pages * _EST_OUTPUT_TOKENS_PER_PAGE
    )


# ---------------------------------------------------------------------------
# fitz (single thread only; see startup_lib/ocr.py on MuPDF thread safety)
# ---------------------------------------------------------------------------


def _open_doc(path: Path):
    import fitz

    doc = fitz.open(path)
    if doc.needs_pass:
        doc.close()
        raise ValueError("PDF is password protected")
    return doc


def _page_dpi(page) -> int:
    long_side_pt = max(page.rect.width, page.rect.height)
    if long_side_pt <= 0:
        return RASTER_DPI
    cap = int(_MAX_LONG_SIDE_PX * 72 / long_side_pt)
    return max(1, min(RASTER_DPI, cap))


def render_page(doc, index: int) -> bytes:
    page = doc[index]
    pixmap = page.get_pixmap(dpi=_page_dpi(page))
    return pixmap.tobytes(output=_RASTER_FORMAT, jpg_quality=_JPEG_QUALITY)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def assemble_markdown(pages: list[str], model: str, today: date) -> str:
    """Provenance line, then each page (optionally marked) joined by blank lines."""
    header = _PROVENANCE_MARKER.format(
        model=model, date=today.isoformat(), pages=len(pages)
    )
    blocks = [header]
    for number, text in enumerate(pages, 1):
        if PAGE_MARKERS:
            blocks.append(_PAGE_MARKER.format(number=number))
        blocks.append(text.strip())
    return _PAGE_SEPARATOR.join(blocks) + "\n"


def format_totals(run: OcrRun) -> str:
    usage = run.usage
    elapsed = _format_duration(time.monotonic() - run.started)
    return (
        f"Gemini OCR: {usage.pages} page(s) in {elapsed}; "
        f"{usage.prompt_tokens:,} in / {usage.output_tokens:,} out tokens; "
        f"${usage.cost_usd():.3f}"
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def ocr_pdfs(
    root: Path,
    rels: list[str],
    hashes: dict[str, str],
    client: PageOcr,
    concurrency: int,
    run: OcrRun,
) -> None:
    loop = asyncio.get_running_loop()
    sem = asyncio.Semaphore(concurrency)
    fitz_pool = ThreadPoolExecutor(max_workers=1)

    def raster(fn, *args):
        return loop.run_in_executor(fitz_pool, fn, *args)

    try:
        async with asyncio.TaskGroup() as tg:
            for rel in rels:
                if run.aborted:
                    break
                await _submit_file(root, rel, hashes[rel], client, sem, tg, raster, run)
    finally:
        fitz_pool.shutdown(wait=True)


async def _submit_file(root, rel, pre_hash, client, sem, tg, raster, run) -> None:
    job = _FileJob(rel, pre_hash)
    try:
        doc = await raster(_open_doc, root / rel)
    except Exception as exc:
        job.failures.append(f"opening failed: {exc}")
        await _finish_file(root, job, run)
        return

    job.total = job.pending = doc.page_count
    job.pages = [None] * job.total
    if job.total == 0:
        job.failures.append("PDF has no pages")
    created = 0
    try:
        for index in range(job.total):
            if job.failures or run.aborted:
                break
            await sem.acquire()
            try:
                data = await raster(render_page, doc, index)
            except Exception as exc:
                sem.release()
                job.failures.append(f"p{index + 1}: rasterizing failed: {exc}")
                break
            tg.create_task(_page_task(client, sem, job, index, data, run, root))
            created += 1
    finally:
        await raster(doc.close)

    # Pages never submitted can never report back.
    job.pending -= job.total - created
    if job.pending == 0:
        await _finish_file(root, job, run)


async def _page_task(client, sem, job, index, data, run, root) -> None:
    try:
        # A sibling page already failed: stop paying for this file.
        if not job.failures and not run.aborted:
            await _ocr_one(client, job, index, data, run)
    finally:
        sem.release()

    run.done_pages += 1
    _print_progress(run, job, index)
    job.pending -= 1
    if job.pending == 0:
        await _finish_file(root, job, run)


async def _ocr_one(client, job, index, data, run) -> None:
    try:
        page = await client.ocr_page(data)
    except PageFailed as exc:
        job.failures.append(f"p{index + 1}: {exc.detail}")
        run.aborted = run.aborted or exc.fatal
        return
    except Exception as exc:
        # An uncaught exception would make the TaskGroup cancel the run.
        job.failures.append(f"p{index + 1}: {type(exc).__name__}: {exc}")
        return
    job.pages[index] = page.markdown
    job.usage.add(page)
    run.usage.add(page)


def _print_progress(run: OcrRun, job: _FileJob, index: int) -> None:
    now = time.monotonic()
    last_page = run.done_pages >= run.total_pages
    if now - run._last_progress < _PROGRESS_MIN_SECONDS and not last_page:
        return
    run._last_progress = now
    elapsed = now - run.started
    rate = ""
    if run.done_pages > 1 and elapsed > 0:
        per_second = run.done_pages / elapsed
        remaining = max(0, run.total_pages - run.done_pages)
        eta = _format_duration(remaining / per_second)
        rate = f" · {per_second:.1f} pages/s · ETA {eta}"
    print(
        f"\tpage {run.done_pages}/{run.total_pages} ({job.rel} p{index + 1}){rate}",
        flush=True,
    )


async def _finish_file(root: Path, job: _FileJob, run: OcrRun) -> None:
    if job.failures:
        detail = f"{len(job.failures)}/{job.total} page(s) failed: {job.failures[0]}"
        run.results.append(ProcessingResult(job.rel, STATUS_FAILED, ROUTE, detail=detail))
        print(f"\tERROR OCRing {job.rel} ({detail})", flush=True)
        return

    text = assemble_markdown([p or "" for p in job.pages], MODEL, date.today())
    finalize = functools.partial(
        _finalize_sidecar, root, root / job.rel, job.pre_hash, text, ROUTE, ocr_done=True
    )
    result = await asyncio.to_thread(finalize)
    run.results.append(result)
    if result.status != STATUS_CONVERTED:
        print(f"\tERROR OCRing {job.rel}: {result.detail}", flush=True)
        return

    elapsed = _format_duration(time.monotonic() - job.started)
    usage = job.usage
    print(
        f"\t{job.rel}: {job.total} page(s) -> {converted_path(root / job.rel).name} "
        f"({elapsed}; {usage.prompt_tokens:,} in / {usage.output_tokens:,} out tokens; "
        f"${usage.cost_usd():.3f})",
        flush=True,
    )
