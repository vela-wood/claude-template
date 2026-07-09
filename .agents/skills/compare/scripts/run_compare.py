#!/usr/bin/env python3
"""Compare two DOCX files and produce a Word-native track-changes DOCX."""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path


def _default_output(original: Path, modified: Path) -> Path:
    stamp = date.today().strftime("%Y%m%d")
    name = f"{modified.stem}_vs_{original.stem}_compare_{stamp}.docx"
    return modified.parent / name


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare ORIGINAL and MODIFIED .docx files and write a third .docx "
            "showing MODIFIED's changes as Word track changes."
        )
    )
    parser.add_argument("original", type=Path, help="Original DOCX")
    parser.add_argument("modified", type=Path, help="Modified DOCX")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output DOCX path (default: <modified>_vs_<original>_compare_YYYYMMDD.docx)",
    )
    parser.add_argument(
        "--author",
        default="Velawood",
        help="Author name for the track changes (default: 'Velawood')",
    )
    args = parser.parse_args(argv)

    for label, path in (("Original", args.original), ("Modified", args.modified)):
        if not path.is_file():
            print(f"{label} file not found: {path}", file=sys.stderr)
            return 1
        if path.suffix.lower() != ".docx":
            print(f"{label} file must be a .docx: {path}", file=sys.stderr)
            return 1

    try:
        from python_redlines import DocxodusEngine
    except ImportError:
        print("python_redlines is not installed in .venv.", file=sys.stderr)
        print(
            "Run 'uv sync' at the repo root. The default 'compare' group installs "
            "python-redlines[docxodus] into the shared .venv.",
            file=sys.stderr,
        )
        return 1

    output = args.output or _default_output(args.original, args.modified)
    if output.resolve() in (args.original.resolve(), args.modified.resolve()):
        print("Output path must not overwrite an input file.", file=sys.stderr)
        return 1

    engine = DocxodusEngine()
    redline_bytes, stdout, stderr = engine.run_redline(
        args.author, args.original, args.modified
    )
    if stderr:
        print(stderr, file=sys.stderr)
    if not redline_bytes:
        print("Compare failed: engine returned no output.", file=sys.stderr)
        return 1

    output.write_bytes(redline_bytes)
    print(f"Wrote track-changes comparison: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
