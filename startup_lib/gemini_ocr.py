"""Cloud OCR driver: rasterize pages, fan requests out, assemble sidecars.

Mirrors run_ocr's contract (startup_lib/ocr.py): one ProcessingResult per
PDF, all-or-nothing per file, each file finalized the moment its last page
lands, so finished files survive an interrupt.

    producer (loop thread)         raster processes (N)        Gemini
    ─────────────────────          ────────────────────        ──────
    for each PDF ──page_count───►  fitz.open (cached per worker)
      for each page:
        alive.acquire()  ◄── back-pressure: at most
                             concurrency + prefetch JPEGs alive
        render_page_at ─────────►  get_pixmap → jpeg  (up to N in parallel)
        create_task(_page_task)
          _page_task: await jpeg, gemini.acquire() ─────────►  generate_content
                                                              ◄── markdown
          store text, gemini.release(), alive.release(), pending -= 1
          pending == 0 → _finish_file → _finalize_sidecar (worker thread)

Rendering runs in processes because PyMuPDF holds the GIL and is not
thread-safe; each worker keeps a few documents open since pages arrive in
file order. Rendering runs ahead of the request slots so a freed slot never
waits on a JPEG.
"""

import asyncio
import functools
import os
import signal
import time
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor
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
    ENV_KEY,
    KEY_URL,
    MODEL,
    USD_PER_M_INPUT,
    USD_PER_M_OUTPUT,
    PageBlocked,
    PageFailed,
    PageText,
)
from startup_lib.progress import format_duration

ROUTE = "gemini-ocr"

# Shown by every entry point when the key is absent. The agent must never
# create keys; the user pastes one via `uv run config.py` or by hand.
MISSING_KEY_MESSAGE = f"""[MISSING] {ENV_KEY} is not set.

Ask the user to run `uv run config.py` and pick "Scanned-document reader
(OCR)", or to add this line to the .env file at the repo root (the user
creates the key at {KEY_URL}; the agent must not create keys):
  {ENV_KEY}=...
Then re-run the command.
"""
INTERRUPTED_DETAIL = "interrupted before OCR finished"
ABORTED_DETAIL = "run aborted after a fatal error"

# Tokens are fixed by media_resolution, not pixels, so DPI and format only
# affect upload bytes: 200 dpi JPEG q85 is ~0.3-0.6 MB per scanned page.
# RGB is kept for stamps and ink colour.
RASTER_DPI = 200
_MAX_LONG_SIDE_PX = 4000  # oversize drawing pages: stay far under 20 MB
_RASTER_FORMAT = "jpeg"
_JPEG_QUALITY = 85

# In-flight requests across ALL files. Measured 2026-09-05 on 443 pages:
# 32 -> ~8.5 pages/s, 64 -> ~16 pages/s, 96 -> no further gain and closer
# to the account's rate limit.
DEFAULT_CONCURRENCY = 64

# Rasterizing: ~0.1 s/page per process, so 4 workers render ~40 pages/s,
# enough to feed ~160 in-flight requests at ~4 s per page.
_RASTER_WORKERS = max(1, min(4, os.cpu_count() or 1))
_WORKER_DOC_CACHE = 4  # open documents kept per worker (pages arrive in file order)
_PREFETCH_PAGES = 2 * _RASTER_WORKERS  # rendered JPEGs waiting for a request slot
_PROGRESS_MIN_SECONDS = 1.0

PAGE_MARKERS = True
_PROVENANCE_MARKER = (
    "<!-- OCR by {model} on {date}; {pages} page(s); media_resolution high -->"
)
_PAGE_MARKER = "<!-- page {number} -->"
# Stands in for a page Gemini declined to transcribe; visible on purpose.
_BLOCKED_PAGE_TEXT = "[page {number} not transcribed: Gemini declined ({reason})]"
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
class Timing:
    """Seconds spent per stage, summed across pages (not wall clock)."""

    open_s: float = 0.0
    raster_s: float = 0.0
    gemini_s: float = 0.0
    finalize_s: float = 0.0
    page_latencies: list[float] = field(default_factory=list)

    def local_s(self) -> float:
        return self.open_s + self.raster_s + self.finalize_s

    def latency_summary(self) -> str:
        """'median 3.1s · p90 6.0s · max 14.2s' over completed Gemini calls."""
        if not self.page_latencies:
            return "n/a"
        ordered = sorted(self.page_latencies)
        pick = lambda q: ordered[min(len(ordered) - 1, int(q * len(ordered)))]  # noqa: E731
        return f"median {pick(0.5):.1f}s · p90 {pick(0.9):.1f}s · max {ordered[-1]:.1f}s"


@dataclass
class OcrRun:
    total_pages: int
    results: list[ProcessingResult] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    timing: Timing = field(default_factory=Timing)
    retries: dict[str, int] = field(default_factory=dict)
    blocked: list[str] = field(default_factory=list)  # "rel p5 (RECITATION)"
    done_pages: int = 0
    aborted: bool = False
    started: float = field(default_factory=time.monotonic)
    _last_progress: float = 0.0


@dataclass
class _FileJob:
    rel: str
    pre_hash: str
    defer_commit: bool = False
    total: int = 0
    pages: list[str | None] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)  # "p5 (RECITATION)"
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


# Per-process document cache; only ever touched inside a raster worker.
_worker_docs: "OrderedDict[str, object]" = OrderedDict()


def _worker_init() -> None:
    # The parent owns Ctrl+C: it cancels the run and shuts the pool down.
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _worker_doc(path: str):
    doc = _worker_docs.get(path)
    if doc is not None:
        _worker_docs.move_to_end(path)
        return doc

    doc = _open_doc(Path(path))
    _worker_docs[path] = doc
    while len(_worker_docs) > _WORKER_DOC_CACHE:
        _, old = _worker_docs.popitem(last=False)
        old.close()
    return doc


def page_count_of(path: str) -> int:
    """Worker entry: open (or reuse) the document and report its page count."""
    return _worker_doc(path).page_count


def render_page_at(path: str, index: int) -> tuple[bytes, float]:
    """Worker entry: (jpeg bytes, seconds spent rendering)."""
    t0 = time.monotonic()
    data = render_page(_worker_doc(path), index)
    return data, time.monotonic() - t0


def count_pages(root: Path, rels: list[str]) -> int:
    """Total page count for the progress denominator; unreadable PDFs count
    zero here and fail later, when they are opened for real."""
    total = 0
    for rel in rels:
        try:
            doc = _open_doc(root / rel)
        except Exception:
            continue
        try:
            total += doc.page_count
        finally:
            doc.close()
    return total


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


def interrupted_results(rels: list[str], run: OcrRun) -> list[ProcessingResult]:
    """Failed results for every PDF the run had not finished when Ctrl+C hit."""
    finished = {r.source_rel for r in run.results}
    return [
        ProcessingResult(rel, STATUS_FAILED, ROUTE, detail=INTERRUPTED_DETAIL)
        for rel in rels
        if rel not in finished
    ]


def format_totals(run: OcrRun) -> str:
    usage = run.usage
    elapsed = format_duration(time.monotonic() - run.started)
    t = run.timing
    return (
        f"Gemini OCR: {usage.pages} page(s) in {elapsed}; "
        f"{usage.prompt_tokens:,} in / {usage.output_tokens:,} out tokens; "
        f"${usage.cost_usd():.3f}\n"
        f"\ttiming (summed per page): gemini {t.gemini_s:.1f}s; local "
        f"{t.local_s():.1f}s (open {t.open_s:.1f}s, raster {t.raster_s:.1f}s, "
        f"finalize {t.finalize_s:.1f}s)\n"
        f"\tgemini per page: {t.latency_summary()}; transport retries: "
        f"{_format_retries(run.retries)}"
        + _format_blocked(run.blocked)
    )


def _format_blocked(blocked: list[str]) -> str:
    if not blocked:
        return ""
    return (
        f"\n\tNotice: {len(blocked)} page(s) not transcribed (Gemini declined); "
        "placeholder text written: " + "; ".join(blocked)
    )


def _format_retries(retries: dict[str, int]) -> str:
    if not retries:
        return "none"
    return ", ".join(f"{n} x HTTP {code}" for code, n in sorted(retries.items()))


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


@dataclass
class _Slots:
    gemini: asyncio.Semaphore  # requests in flight
    alive: asyncio.Semaphore  # JPEGs rendered or rendering, incl. those in flight


async def ocr_pdfs(
    root: Path,
    rels: list[str],
    hashes: dict[str, str],
    client: PageOcr,
    concurrency: int,
    run: OcrRun,
    defer_commit_rels: set[str] | None = None,
) -> None:
    defer_commit_rels = defer_commit_rels or set()
    loop = asyncio.get_running_loop()
    slots = _Slots(
        gemini=asyncio.Semaphore(concurrency),
        alive=asyncio.Semaphore(concurrency + _PREFETCH_PAGES),
    )
    pool = ProcessPoolExecutor(max_workers=_RASTER_WORKERS, initializer=_worker_init)

    def raster(fn, *args):
        return loop.run_in_executor(pool, fn, *args)

    try:
        async with asyncio.TaskGroup() as tg:
            for rel in rels:
                if run.aborted:
                    break
                job = _FileJob(rel, hashes[rel], defer_commit=rel in defer_commit_rels)
                await _submit_file(root, job, client, slots, tg, raster, run)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        # Fakes in tests have no retry counter.
        retries = getattr(client, "retries", None)
        run.retries = retries() if callable(retries) else {}


async def _submit_file(root, job, client, slots, tg, raster, run) -> None:
    path = str(root / job.rel)
    t0 = time.monotonic()
    try:
        job.total = await raster(page_count_of, path)
    except Exception as exc:
        job.failures.append(f"opening failed: {exc}")
        await _finish_file(root, job, run)
        return
    run.timing.open_s += time.monotonic() - t0

    job.pending = job.total
    job.pages = [None] * job.total
    if job.total == 0:
        job.failures.append("PDF has no pages")
        await _finish_file(root, job, run)
        return

    # Rendering is kicked off here and awaited by the page task, so the
    # producer only ever blocks on back-pressure, never on fitz.
    created = 0
    for index in range(job.total):
        _note_abort(job, run, index)
        if job.failures:
            break
        await slots.alive.acquire()
        image = raster(render_page_at, path, index)
        tg.create_task(_page_task(client, slots, job, index, image, run, root))
        created += 1

    # Pages never submitted can never report back.
    job.pending -= job.total - created
    if job.pending == 0:
        await _finish_file(root, job, run)


async def _page_task(client, slots, job, index, image, run, root) -> None:
    try:
        data = await _rendered(image, job, index, run)
        if data is not None:
            async with slots.gemini:
                _note_abort(job, run, index)
                # A sibling page already failed: stop paying for this file.
                if not job.failures:
                    await _ocr_one(client, job, index, data, run)
    finally:
        slots.alive.release()

    run.done_pages += 1
    _print_progress(run, job, index)
    job.pending -= 1
    if job.pending == 0:
        await _finish_file(root, job, run)


def _note_abort(job: _FileJob, run: OcrRun, index: int) -> None:
    """A fatal error elsewhere must fail this file too, or a file whose
    pages were skipped would be written as blanks."""
    if run.aborted and not job.failures:
        job.failures.append(f"p{index + 1}: {ABORTED_DETAIL}")


async def _rendered(image, job, index, run) -> bytes | None:
    """JPEG bytes from the raster future, or None after recording the failure."""
    try:
        data, seconds = await image
    except Exception as exc:
        job.failures.append(f"p{index + 1}: rasterizing failed: {exc}")
        return None
    run.timing.raster_s += seconds
    return data


async def _ocr_one(client, job, index, data, run) -> None:
    t0 = time.monotonic()
    try:
        page = await client.ocr_page(data)
    except PageBlocked as exc:
        # Deterministic content block: keep the file, mark the gap.
        job.pages[index] = _BLOCKED_PAGE_TEXT.format(number=index + 1, reason=exc.reason)
        job.blocked.append(f"p{index + 1} ({exc.reason})")
        run.blocked.append(f"{job.rel} p{index + 1} ({exc.reason})")
        return
    except PageFailed as exc:
        job.failures.append(f"p{index + 1}: {exc.detail}")
        run.aborted = run.aborted or exc.fatal
        return
    except Exception as exc:
        # An uncaught exception would make the TaskGroup cancel the run.
        job.failures.append(f"p{index + 1}: {type(exc).__name__}: {exc}")
        return
    finally:
        seconds = time.monotonic() - t0
        run.timing.gemini_s += seconds
        run.timing.page_latencies.append(seconds)
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
        eta = format_duration(remaining / per_second)
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
        _finalize_sidecar,
        root,
        root / job.rel,
        job.pre_hash,
        text,
        ROUTE,
        ocr_done=True,
        defer_commit=job.defer_commit,
    )
    t0 = time.monotonic()
    result = await asyncio.to_thread(finalize)
    run.timing.finalize_s += time.monotonic() - t0
    run.results.append(result)
    if result.status != STATUS_CONVERTED:
        print(f"\tERROR OCRing {job.rel}: {result.detail}", flush=True)
        return

    elapsed = format_duration(time.monotonic() - job.started)
    usage = job.usage
    print(
        f"\t{job.rel}: {job.total} page(s) -> {converted_path(root / job.rel).name} "
        f"({elapsed}; {usage.prompt_tokens:,} in / {usage.output_tokens:,} out tokens; "
        f"${usage.cost_usd():.3f})",
        flush=True,
    )
    if job.blocked:
        print(
            f"\tNotice: {job.rel}: Gemini declined {', '.join(job.blocked)}; "
            "placeholder text written for those page(s)",
            flush=True,
        )
