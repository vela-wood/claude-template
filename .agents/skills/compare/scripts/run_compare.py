#!/usr/bin/env python3
"""Compare two DOCX files and produce a Word-native track-changes DOCX."""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path


def _default_output(original: Path, modified: Path) -> Path:
    stamp = date.today().strftime("%Y%m%d")
    base = f"{modified.stem}_vs_{original.stem}_compare_{stamp}"
    candidate = modified.parent / f"{base}.docx"
    # Never clobber an earlier comparison of the same pair on the same day.
    counter = 2
    while candidate.exists():
        candidate = modified.parent / f"{base}_{counter}.docx"
        counter += 1
    return candidate


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

    if args.original.resolve() == args.modified.resolve():
        print(
            "Original and Modified are the same file; nothing to compare.",
            file=sys.stderr,
        )
        return 1

    try:
        from python_redlines import DocxodusEngine, EngineNotInstalledError
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

    try:
        engine = DocxodusEngine()
    except EngineNotInstalledError as exc:
        print(f"Compare engine unavailable: {exc}", file=sys.stderr)
        print(
            "Run 'uv sync' at the repo root. The default 'compare' group installs "
            "python-redlines[docxodus] into the shared .venv.",
            file=sys.stderr,
        )
        return 1

    # Hand the engine bytes, not paths: run_redline() unconditionally deletes the
    # paths it is given, so passing Path objects destroys both source documents.
    original_bytes = args.original.read_bytes()
    modified_bytes = args.modified.read_bytes()
    try:
        redline_bytes, _stdout, stderr = engine.run_redline(
            args.author, original_bytes, modified_bytes
        )
    except subprocess.CalledProcessError as exc:
        # Docxodus bug: format-only differences can surface an unhandled
        # "FormatChanged" status and abort. Retry without format-change
        # detection; text changes are still fully tracked.
        engine_output = "\n".join(s for s in (exc.stderr, exc.stdout) if s)
        if "FormatChanged" not in engine_output:
            print(f"Compare failed: engine exited {exc.returncode}.", file=sys.stderr)
            if engine_output:
                print(engine_output.strip(), file=sys.stderr)
            return 1
        print(
            "Engine hit the FormatChanged bug; retrying with format-change "
            "detection disabled. Format-only changes (e.g. bolding, borders) "
            "will not appear as tracked changes.",
            file=sys.stderr,
        )
        try:
            redline_bytes, _stdout, stderr = engine.run_redline(
                args.author,
                original_bytes,
                modified_bytes,
                detect_format_changes=False,
            )
        except subprocess.CalledProcessError as exc2:
            print(f"Compare failed: engine exited {exc2.returncode}.", file=sys.stderr)
            for stream in (exc2.stderr, exc2.stdout):
                if stream:
                    print(stream.strip(), file=sys.stderr)
            return 1

    if stderr:
        print(stderr, file=sys.stderr)
    if not redline_bytes:
        print("Compare failed: engine returned no output.", file=sys.stderr)
        return 1

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(redline_bytes)
    print(f"Wrote track-changes comparison: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
