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

INDEX_FILENAME = ".file_index.csv"

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


def converted_path(source: Path) -> Path:
    """Return the expected converted-file path for a source file."""
    if source.suffix == ".pdf":
        return source.parent / f"{source.name}.md"
    if source.suffix == ".docx":
        return source.parent / f"{source.name}.json"
    raise ValueError(f"Unsupported source type: {source}")


# ---------------------------------------------------------------------------
# Index I/O
# ---------------------------------------------------------------------------


def load_index(root: Path) -> dict[str, tuple[str, int]]:
    """Load .file_index.csv → {relative_path: (hash, token_count)}."""
    index_path = root / INDEX_FILENAME
    index: dict[str, tuple[str, int]] = {}
    if not index_path.exists():
        return index
    with open(index_path, newline="") as f:
        for row in csv.DictReader(f):
            index[row["file"]] = (row["hash"], int(row["tokens"]))
    return index


def save_index(root: Path, index: dict[str, tuple[str, int]]) -> None:
    """Write .file_index.csv from {relative_path: (hash, token_count)}."""
    index_path = root / INDEX_FILENAME
    with open(index_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["file", "hash", "tokens"])
        for rel_path in sorted(index):
            h, t = index[rel_path]
            writer.writerow([rel_path, h, t])


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def _convert_one_pdf(pdf: Path, md_path: Path) -> None:
    subprocess.run(
        ["uv", "run", "markitdown", str(pdf), "-o", str(md_path)],
        check=True,
    )


def _convert_one_docx(docx: Path, json_path: Path) -> None:
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
    index: dict[str, tuple[str, int]],
) -> list[str]:
    """Convert source files whose hash is new or changed. Returns list of converted relative paths."""
    to_convert: list[Path] = []
    for src in sources:
        rel = str(src.relative_to(root))
        old = index.get(rel)
        if old is None or old[0] != hashes[rel]:
            to_convert.append(src)

    if not to_convert:
        return []

    converted_rels: list[str] = []
    print(f"\nConverting {len(to_convert)} file(s)...")

    def _do_convert(src: Path) -> str:
        rel = str(src.relative_to(root))
        out = converted_path(src)
        print(f"\t{rel} -> {out.name}")
        if src.suffix == ".pdf":
            _convert_one_pdf(src, out)
        elif src.suffix == ".docx":
            _convert_one_docx(src, out)
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
    index: dict[str, tuple[str, int]],
    converted_rels: list[str],
) -> None:
    """Count tokens for new/changed converted files and update index in-place."""
    # Determine which files need (re)counting:
    # - files that were just converted
    # - files present in hashes but missing from index
    needs_count: list[str] = []
    for rel in hashes:
        if rel in converted_rels or rel not in index:
            out = converted_path(root / rel)
            if out.exists():
                needs_count.append(rel)

    if not needs_count:
        return

    print(f"\nCounting tokens for {len(needs_count)} file(s)...")

    def _count(rel: str) -> tuple[str, int]:
        out = converted_path(root / rel)
        tokens = count_tokens(out)
        return rel, tokens

    with ThreadPoolExecutor() as pool:
        futures = {pool.submit(_count, rel): rel for rel in needs_count}
        for future in as_completed(futures):
            rel = futures[future]
            try:
                rel_key, tokens = future.result()
                index[rel_key] = (hashes[rel_key], tokens)
            except Exception as exc:
                print(f"\tERROR counting tokens for {rel}: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("Python environment configured.")
    print("Tools available to agent...")
    nd_vars = ("MATTERS_DB", "ND_API_KEY", "NDHELPER_URL")
    if all(os.getenv(var) for var in nd_vars):
        print("\tNetdocs access")

    artifact_vars = ("ARTIFACT_API_TOKEN", "ARTIFACT_URL")
    if all(os.getenv(var) for var in artifact_vars):
        print("\tPDF artifact removal")

    root = Path.cwd()

    # 1. Load existing index
    index = load_index(root)

    # 2. Discover source files
    pdfs = sorted(root.rglob("*.pdf"))
    docx_files = sorted(root.rglob("*.docx"))
    sources = pdfs + docx_files

    if not sources:
        print("\nNo PDF or DOCX files found.")
        save_index(root, index)
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
    converted_rels = convert_files(root, sources, hashes, index)

    # 5. Update index hashes for unchanged files (keep existing token counts)
    for rel, h in hashes.items():
        old = index.get(rel)
        if old is not None and old[0] == h:
            # Hash unchanged — keep existing entry as-is
            pass
        elif rel not in [r for r in converted_rels]:
            # Hash changed but conversion failed or was skipped — update hash, clear tokens
            index[rel] = (h, index.get(rel, (h, 0))[1])

    # 6. Count tokens for new/changed converted files
    index_tokens(root, hashes, index, converted_rels)

    # 7. Prune index entries for files that no longer exist
    stale = [rel for rel in index if rel not in hashes]
    for rel in stale:
        del index[rel]

    # 8. Save index
    save_index(root, index)

    # 9. Summary
    total_tokens = sum(t for _, t in index.values())
    skipped = len(sources) - len(converted_rels)
    print(f"\nDone. {len(sources)} files indexed, {len(converted_rels)} converted, {skipped} unchanged.")
    print(f"Total tokens across converted files: {total_tokens:,}")
    print(f"Index written to {INDEX_FILENAME}")


if __name__ == "__main__":
    main()
