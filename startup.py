import argparse
import csv
import os
import shutil
import subprocess
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import tiktoken

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
DOCX_CONVERTER_MARKITDOWN = "markitdown"
DEFAULT_DOCX_CONVERTER = DOCX_CONVERTER_MARKITDOWN
EMAIL_SUFFIXES = {
    ".eml",
    ".msg",
    ".emlx",
    ".mbox",
    ".mbx",
    ".mht",
    ".mhtml",
    ".oft",
}
NATIVE_EMAIL_SUFFIXES = {".eml", ".emlx"}
MARKITDOWN_MD_SUFFIXES = {".pdf"} | (EMAIL_SUFFIXES - NATIVE_EMAIL_SUFFIXES)
SOURCE_SUFFIXES = MARKITDOWN_MD_SUFFIXES | NATIVE_EMAIL_SUFFIXES | {".docx"}

# Create tiktoken encoding once at module level (thread-safe, Rust-backed)
_encoding = tiktoken.get_encoding("cl100k_base")


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
    """Count tokens in a text file using tiktoken cl100k_base."""
    text = path.read_text(errors="replace")
    return len(_encoding.encode(text))


def converted_path(source: Path, docx_converter: str = DEFAULT_DOCX_CONVERTER) -> Path:
    """Return the expected converted-file path for a source file."""
    suffix = source.suffix.lower()
    if suffix in MARKITDOWN_MD_SUFFIXES or suffix in NATIVE_EMAIL_SUFFIXES:
        return source.parent / f"{source.name}.md"
    if suffix == ".docx":
        return source.parent / f"{source.name}.md"
    raise ValueError(f"Unsupported source type: {source}")


def ocr_output_path(source: Path) -> Path:
    """Return the focr output path for a PDF (foo.pdf -> foo.pdf.ocr.md)."""
    return source.parent / f"{source.name}.ocr.md"


# ---------------------------------------------------------------------------
# Index I/O
# ---------------------------------------------------------------------------


def load_hash_index(root: Path) -> dict[str, str]:
    """Load .hash_index.csv → {native_relative_path: hash}."""
    index_path = root / HASH_INDEX_FILENAME
    index: dict[str, str] = {}
    if not index_path.exists():
        return index
    with open(index_path, newline="") as f:
        for row in csv.DictReader(f):
            index[row["file"]] = row["hash"]
    return index


def save_hash_index(root: Path, index: dict[str, str]) -> None:
    """Write .hash_index.csv from {native_relative_path: hash}."""
    index_path = root / HASH_INDEX_FILENAME
    with open(index_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["file", "hash"])
        for rel_path in sorted(index):
            writer.writerow([rel_path, index[rel_path]])


def load_token_index(root: Path) -> dict[str, int]:
    """Load .token_index.csv → {converted_relative_path: token_count}."""
    index_path = root / TOKEN_INDEX_FILENAME
    index: dict[str, int] = {}
    if not index_path.exists():
        return index
    with open(index_path, newline="") as f:
        for row in csv.DictReader(f):
            index[row["file"]] = int(row["tokens"])
    return index


def save_token_index(root: Path, index: dict[str, int]) -> None:
    """Write .token_index.csv from {converted_relative_path: token_count}."""
    index_path = root / TOKEN_INDEX_FILENAME
    with open(index_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["file", "tokens"])
        for rel_path in sorted(index):
            writer.writerow([rel_path, index[rel_path]])


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def _convert_one_markitdown(source: Path, md_path: Path) -> None:
    subprocess.run(
        ["uv", "run", "markitdown", str(source), "-o", str(md_path)],
        check=True,
    )


def _convert_one_email(source: Path, md_path: Path) -> None:
    """Convert .eml/.emlx to clean markdown using Python's email module."""
    import email as _email
    from email import policy as _policy

    with open(source, "rb") as f:
        msg = _email.message_from_binary_file(f, policy=_policy.default)

    lines = [
        f"# {msg['subject'] or '(no subject)'}",
        "",
        f"**From:** {msg['from']}",
        f"**To:** {msg['to']}",
    ]
    if msg["cc"]:
        lines.append(f"**CC:** {msg['cc']}")
    lines += [f"**Date:** {msg['date']}", "", "---", ""]

    body = None
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            try:
                body = part.get_content()
            except Exception:
                body = part.get_payload(decode=True).decode("utf-8", errors="replace")
            break
    lines.append(body if body else "(no text content)")
    md_path.write_text("\n".join(lines), encoding="utf-8")

def convert_files(
    root: Path,
    sources: list[Path],
    hashes: dict[str, str],
    hash_index: dict[str, str],
    docx_converter: str,
) -> list[str]:
    """Convert source files whose hash is new or changed. Returns list of native relative paths that were converted."""
    to_convert: list[Path] = []
    for src in sources:
        rel = str(src.relative_to(root))
        old_hash = hash_index.get(rel)
        out = converted_path(src, docx_converter)
        if old_hash is None or old_hash != hashes[rel] or not out.exists():
            to_convert.append(src)

    if not to_convert:
        return []

    converted_rels: list[str] = []
    print(f"\nConverting {len(to_convert)} file(s)...")

    def _do_convert(src: Path) -> str:
        rel = str(src.relative_to(root))
        out = converted_path(src, docx_converter)
        print(f"\t{rel} -> {out.name}")
        suffix = src.suffix.lower()
        if suffix in NATIVE_EMAIL_SUFFIXES:
            _convert_one_email(src, out)
        elif suffix in MARKITDOWN_MD_SUFFIXES:
            _convert_one_markitdown(src, out)
        elif suffix == ".docx":
            _convert_one_markitdown(src, out)
        return rel

    with ThreadPoolExecutor() as pool:
        futures = {pool.submit(_do_convert, src): src for src in to_convert}
        for future in as_completed(futures):
            src = futures[future]
            try:
                converted_rels.append(future.result())
            except Exception as exc:
                print(f"\tERROR converting {src.relative_to(root)}: {exc}")

    return converted_rels


# ---------------------------------------------------------------------------
# PDF classification (scanned vs digital → needs_ocr)
# ---------------------------------------------------------------------------


def classify_pdfs(
    root: Path,
    sources: list[Path],
    hashes: dict[str, str],
    ocr_index: dict[str, dict[str, str]],
) -> None:
    """Classify new/changed PDFs and update ocr_index in-place.

    Files verdicted scanned-image-only or mixed/other get needs_ocr=True;
    markitdown conversion will produce nothing useful for those.
    """
    pdf_rels = [
        str(src.relative_to(root)) for src in sources if src.suffix.lower() == ".pdf"
    ]
    to_classify = [
        rel
        for rel in pdf_rels
        if rel in hashes and ocr_index.get(rel, {}).get("hash") != hashes[rel]
    ]

    if to_classify:
        print(f"\nClassifying {len(to_classify)} PDF(s) (scanned vs digital)...")
        for rel in to_classify:
            result = classify_pdf(root / rel)
            ocr_index[rel] = index_row(rel, hashes[rel], result)
            if result.needs_ocr:
                print(f"\t{rel}: {result.verdict} -> needs_ocr")

    current = set(pdf_rels)
    for rel in [rel for rel in ocr_index if rel not in current]:
        del ocr_index[rel]


# ---------------------------------------------------------------------------
# OCR (focr) for PDFs flagged needs_ocr
# ---------------------------------------------------------------------------

# The model resizes pages to a 1024px global view, so 150 dpi is ample.
_OCR_RASTER_DPI = 150


def run_ocr(root: Path, ocr_index: dict[str, dict[str, str]]) -> None:
    """Run focr on PDFs flagged needs_ocr that lack a successful conversion.

    All pages of all pending PDFs are rasterized to a temp dir and fed to a
    single `focr ocr-batch` invocation, so the multi-GB model is loaded once
    for the whole run instead of once per file. Per-PDF output is written to
    foo.pdf.ocr.md (pages joined with a blank line, matching focr's native
    PDF handling), and ocr_done is set only when every page of a PDF OCRed
    successfully.

    A row is skipped when ocr_done == "true" and its .ocr.md output still
    exists; classify_pdfs() resets ocr_done whenever a PDF's hash changes,
    so changed files are re-OCRed automatically. Updates ocr_index in-place.
    """
    import json
    import tempfile

    import fitz

    to_ocr = [
        rel
        for rel, row in sorted(ocr_index.items())
        if row["verdict"] in NEEDS_OCR_VERDICTS
        and not (row.get("ocr_done") == "true" and ocr_output_path(root / rel).exists())
    ]
    if not to_ocr:
        print("\nOCR: nothing to do (all flagged PDFs already converted).")
        return

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
                print(f"\tERROR rasterizing {rel}: {exc}")
                continue
            if not paths:
                print(f"\tERROR rasterizing {rel}: PDF has no pages")
                continue
            pages_by_rel[rel] = paths

        page_paths = [p for paths in pages_by_rel.values() for p in paths]
        if not page_paths:
            return
        print(f"\tRasterized {len(page_paths)} page(s) from {len(pages_by_rel)} PDF(s); running focr ocr-batch...")

        # 2. One focr process for the whole batch. stderr passes through so
        # focr's per-page progress stays visible; stdout carries the JSON.
        proc = subprocess.run(
            ["focr", "ocr-batch", *map(str, page_paths), "--json"],
            stdout=subprocess.PIPE,
            text=True,
        )
        if proc.returncode != 0:
            print(f"\tERROR: focr ocr-batch exited {proc.returncode}")
            return
        try:
            results = {r["image"]: r for r in json.loads(proc.stdout)["results"]}
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            print(f"\tERROR parsing focr ocr-batch JSON: {exc}")
            return

        # 3. Reassemble per-PDF markdown; mark done only on full success.
        for rel, paths in pages_by_rel.items():
            page_md: list[str] = []
            failed: list[str] = []
            for png in paths:
                r = results.get(str(png))
                if r is not None and r.get("ok") and r.get("markdown") is not None:
                    page_md.append(r["markdown"])
                else:
                    err = (r or {}).get("error", "no result returned")
                    failed.append(f"{png.name}: {err}")
            if failed:
                print(f"\tERROR OCRing {rel} ({len(failed)}/{len(paths)} page(s) failed): {failed[0]}")
                continue
            out = ocr_output_path(root / rel)
            out.write_text("\n\n".join(page_md), encoding="utf-8")
            ocr_index[rel]["ocr_done"] = "true"
            print(f"\t{rel} -> {out.name}")


# ---------------------------------------------------------------------------
# Token indexing
# ---------------------------------------------------------------------------


def index_tokens(
    root: Path,
    hashes: dict[str, str],
    token_index: dict[str, int],
    converted_rels: list[str],
    docx_converter: str,
) -> None:
    """Count tokens for new/changed converted files and update token_index in-place."""
    needs_count: list[str] = []
    for rel in hashes:
        out = converted_path(root / rel, docx_converter)
        conv_rel = str(out.relative_to(root))
        if rel in converted_rels or conv_rel not in token_index:
            if out.exists():
                needs_count.append(rel)

    if not needs_count:
        return

    print(f"\nCounting tokens for {len(needs_count)} file(s)...")

    def _count(rel: str) -> tuple[str, int]:
        out = converted_path(root / rel, docx_converter)
        conv_rel = str(out.relative_to(root))
        tokens = count_tokens(out)
        return conv_rel, tokens

    with ThreadPoolExecutor() as pool:
        futures = {pool.submit(_count, rel): rel for rel in needs_count}
        for future in as_completed(futures):
            rel = futures[future]
            try:
                conv_rel, tokens = future.result()
                token_index[conv_rel] = tokens
            except Exception as exc:
                print(f"\tERROR counting tokens for {rel}: {exc}")


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
        help="run focr on PDFs flagged needs_ocr (writes foo.pdf.ocr.md, tracked in "
        f"{OCR_INDEX_FILENAME})",
    )
    return parser.parse_args()


def main():
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

    # 2. Discover source files (skip dot-prefixed directories and temp files)
    def _eligible_source(p: Path) -> bool:
        rel_parts = p.relative_to(root).parts
        return (
            not any(part.startswith(".") for part in rel_parts[:-1])
            and not p.name.startswith("~")
            and p.suffix.lower() in SOURCE_SUFFIXES
        )

    sources = sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and _eligible_source(p)
    )

    if not sources:
        print("\nNo office documents found.")
        save_hash_index(root, hash_index)
        save_token_index(root, token_index)
        return

    # 3. Hash all source files in parallel
    print(f"\nHashing {len(sources)} source file(s)...")
    hashes: dict[str, str] = {}
    with ThreadPoolExecutor() as pool:
        futures = {
            pool.submit(hash_file, src): src for src in sources
        }
        for future in as_completed(futures):
            src = futures[future]
            rel = str(src.relative_to(root))
            try:
                hashes[rel] = future.result()
            except Exception as exc:
                print(f"\tERROR hashing {rel}: {exc}")

    # 4. Classify new/changed PDFs (flags needs_ocr in .ocr_index.csv)
    classify_pdfs(root, sources, hashes, ocr_index)

    # 4b. With --ocr, run focr on flagged PDFs not yet successfully converted
    if args.ocr:
        run_ocr(root, ocr_index)

    # 5. Convert files with changed/new hashes
    converted_rels = convert_files(root, sources, hashes, hash_index, DEFAULT_DOCX_CONVERTER)

    # 6. Update hash index for all current files
    for rel, h in hashes.items():
        hash_index[rel] = h

    # 7. Count tokens for new/changed converted files
    index_tokens(root, hashes, token_index, converted_rels, DEFAULT_DOCX_CONVERTER)

    # 8. Prune stale entries
    stale_hash = [rel for rel in hash_index if rel not in hashes]
    for rel in stale_hash:
        del hash_index[rel]

    valid_converted = set()
    for rel in hashes:
        try:
            conv_rel = str(converted_path(root / rel, DEFAULT_DOCX_CONVERTER).relative_to(root))
            valid_converted.add(conv_rel)
        except ValueError:
            pass
    stale_token = [rel for rel in token_index if rel not in valid_converted]
    for rel in stale_token:
        del token_index[rel]

    # 9. Save indices
    save_hash_index(root, hash_index)
    save_token_index(root, token_index)
    save_ocr_index(root, ocr_index)

    # 10. Summary
    total_tokens = sum(token_index.values())
    skipped = len(sources) - len(converted_rels)
    needs_ocr = sum(
        1 for row in ocr_index.values() if row["verdict"] in NEEDS_OCR_VERDICTS
    )
    print(f"\nDone. {len(sources)} office documents indexed, {len(converted_rels)} converted, {skipped} unchanged.")
    ocr_done = sum(1 for row in ocr_index.values() if row.get("ocr_done") == "true")
    print(f"PDFs classified: {len(ocr_index)}, of which {needs_ocr} flagged needs_ocr ({ocr_done} OCR-converted).")
    print(f"Total tokens across converted files: {total_tokens:,}")
    print(f"Indices written to {HASH_INDEX_FILENAME}, {TOKEN_INDEX_FILENAME}, and {OCR_INDEX_FILENAME}")


if __name__ == "__main__":
    main()
