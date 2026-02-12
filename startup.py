import argparse
import csv
import hashlib
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import tiktoken
from dotenv import load_dotenv

load_dotenv()

SUPERDOC = (
    Path(__file__).resolve().parent
    / ".claude"
    / "skills"
    / "superdoc-redlines"
    / "superdoc-redline.mjs"
)

HASH_INDEX_FILENAME = ".hash_index.csv"
TOKEN_INDEX_FILENAME = ".token_index.csv"
DOCX_CONVERTER_MARKITDOWN = "markitdown"
DOCX_CONVERTER_SUPERDOC = "superdoc-redlines"
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
MARKITDOWN_MD_SUFFIXES = {".pdf"} | EMAIL_SUFFIXES
SOURCE_SUFFIXES = MARKITDOWN_MD_SUFFIXES | {".docx"}

# Create tiktoken encoding once at module level (thread-safe, Rust-backed)
_encoding = tiktoken.get_encoding("cl100k_base")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def hash_file(path: Path) -> str:
    """Return blake2b hex digest of a file, streaming to avoid large allocations."""
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "blake2b").hexdigest()


def count_tokens(path: Path) -> int:
    """Count tokens in a text file using tiktoken cl100k_base."""
    text = path.read_text(errors="replace")
    return len(_encoding.encode(text))


def converted_path(source: Path, docx_converter: str = DEFAULT_DOCX_CONVERTER) -> Path:
    """Return the expected converted-file path for a source file."""
    suffix = source.suffix.lower()
    if suffix in MARKITDOWN_MD_SUFFIXES:
        return source.parent / f"{source.name}.md"
    if suffix == ".docx":
        suffix = "md" if docx_converter == DOCX_CONVERTER_MARKITDOWN else "json"
        return source.parent / f"{source.name}.{suffix}"
    raise ValueError(f"Unsupported source type: {source}")


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


def _convert_one_docx_superdoc(docx: Path, json_path: Path) -> None:
    result = subprocess.run(
        ["node", str(SUPERDOC), "read", "--input", str(docx), "--no-metadata"],
        capture_output=True,
        text=True,
        check=True,
    )
    json_path.write_text(result.stdout)


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
        if suffix in MARKITDOWN_MD_SUFFIXES:
            _convert_one_markitdown(src, out)
        elif suffix == ".docx":
            if docx_converter == DOCX_CONVERTER_MARKITDOWN:
                _convert_one_markitdown(src, out)
            else:
                _convert_one_docx_superdoc(src, out)
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
        "--docx_converter",
        choices=[DOCX_CONVERTER_MARKITDOWN, DOCX_CONVERTER_SUPERDOC],
        default=DEFAULT_DOCX_CONVERTER,
        help="Tool used for DOCX conversion (default: markitdown).",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    print("Python environment configured.")
    print("Tools available to agent...")
    nd_vars = ("MATTERS_DB", "ND_API_KEY", "NDHELPER_URL")
    if all(os.getenv(var) for var in nd_vars):
        print("\tNetdocs access")

    artifact_vars = ("ARTIFACT_API_TOKEN", "ARTIFACT_URL")
    if all(os.getenv(var) for var in artifact_vars):
        print("\tPDF artifact removal")
    print(f"\tDOCX converter: {args.docx_converter}")

    root = Path.cwd()

    # 1. Load existing indices
    hash_index = load_hash_index(root)
    token_index = load_token_index(root)

    # 2. Discover source files (skip dot-prefixed directories)
    def _visible(p: Path) -> bool:
        return not any(part.startswith(".") for part in p.relative_to(root).parts[:-1])

    sources = sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and _visible(p) and p.suffix.lower() in SOURCE_SUFFIXES
    )

    if not sources:
        print("\nNo supported source files found.")
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

    # 4. Convert files with changed/new hashes
    converted_rels = convert_files(root, sources, hashes, hash_index, args.docx_converter)

    # 5. Update hash index for all current files
    for rel, h in hashes.items():
        hash_index[rel] = h

    # 6. Count tokens for new/changed converted files
    index_tokens(root, hashes, token_index, converted_rels, args.docx_converter)

    # 7. Prune stale entries
    stale_hash = [rel for rel in hash_index if rel not in hashes]
    for rel in stale_hash:
        del hash_index[rel]

    valid_converted = set()
    for rel in hashes:
        try:
            conv_rel = str(converted_path(root / rel, args.docx_converter).relative_to(root))
            valid_converted.add(conv_rel)
        except ValueError:
            pass
    stale_token = [rel for rel in token_index if rel not in valid_converted]
    for rel in stale_token:
        del token_index[rel]

    # 8. Save indices
    save_hash_index(root, hash_index)
    save_token_index(root, token_index)

    # 9. Summary
    total_tokens = sum(token_index.values())
    skipped = len(sources) - len(converted_rels)
    print(f"\nDone. {len(sources)} files indexed, {len(converted_rels)} converted, {skipped} unchanged.")
    print(f"Total tokens across converted files: {total_tokens:,}")
    print(f"Indices written to {HASH_INDEX_FILENAME} and {TOKEN_INDEX_FILENAME}")


if __name__ == "__main__":
    main()
