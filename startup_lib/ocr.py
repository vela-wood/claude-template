"""OCR (focr) for the PDFs classification flagged needs_ocr."""

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from startup_lib import common
from startup_lib.common import STATUS_CONVERTED, STATUS_FAILED, ProcessingResult, converted_path
from startup_lib.convert import _finalize_sidecar

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
    if common.OCR_INT8:
        defaults.update(_FOCR_INT8_ENV)
    for key, value in defaults.items():
        env.setdefault(key, value)
    return env


def _focr_argv(command: str, chunk: list[Path]) -> list[str]:
    """The `focr ocr-batch` command line for one chunk of page images."""
    argv = [command, "ocr-batch", *map(str, chunk), "--json"]
    if common.OCR_INT8:
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
