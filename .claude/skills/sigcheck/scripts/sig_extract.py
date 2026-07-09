#!/usr/bin/env python3
"""Phase 1-2 of /sigcheck: locate the signature-block region of a converted
legal-agreement markdown file and write it out as <original-name>_sigs.md.

Detection is deterministic:
  1. Scan for short, standalone "start" marker lines ([Signature Page(s) Follow],
     IN WITNESS WHEREOF, "The parties have executed this ... "), preferring the
     first marker that appears in the tail of the document (default: last 40%).
  2. The region ends at the first short standalone EXHIBIT/SCHEDULE/ANNEX/APPENDIX
     heading (or Footnotes/Endnotes section) after the start, else EOF.

Usage:
  uv run sig_extract.py FILE.md [FILE2.md ...] [--outdir DIR] [--min-frac 0.4]
                                [--start-line N] [--dry-run]
"""

import argparse
import re
import sys
from pathlib import Path

START_RES = [
    re.compile(r"\[\s*(?:company\s+|counterpart\s+)?signature\s+pages?\s+(?:follow|to\s+follow)", re.I),
    re.compile(r"in\s+witness\s+whereof", re.I),
    re.compile(r"(?:parties|undersigned)\s+(?:has|have)\s+executed\s+this", re.I),
    re.compile(r"^signature\s+pages?\b", re.I),
]
STOP_RE = re.compile(r"^(?:#+\s*)?(exhibit|schedule|annex|appendix)\s+[a-z0-9]", re.I)
END_RE = re.compile(r"^#*\s*(footnotes|endnotes)$", re.I)


def plain(line: str) -> str:
    """Strip markdown table pipes/emphasis for pattern matching."""
    return re.sub(r"[|*_]", " ", line).strip()


def find_start(lines, min_frac):
    floor = int(len(lines) * min_frac)
    candidates = []
    for i, ln in enumerate(lines):
        p = plain(ln)
        if not p:
            continue
        # short standalone marker lines match anywhere; long lines only count
        # if the marker opens the line (e.g. "IN WITNESS WHEREOF, this ... has been executed ...")
        for rx in START_RES:
            m = rx.search(p)
            if m and (len(p) <= 160 or m.start() <= 40):
                candidates.append(i)
                break
    tail = [i for i in candidates if i >= floor]
    if tail:
        return tail[0], candidates
    if candidates:  # fall back to the last marker anywhere
        return candidates[-1], candidates
    return None, candidates


def find_stop(lines, start):
    for i in range(start + 1, len(lines)):
        p = plain(lines[i])
        if not p or len(p) > 60:
            continue
        if STOP_RE.match(p) or END_RE.match(p):
            return i
    return len(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="converted markdown files (.md)")
    ap.add_argument("--outdir", help="directory for *_sigs.md output (default: alongside input)")
    ap.add_argument("--min-frac", type=float, default=0.4,
                    help="prefer start markers at/after this fraction of the file (default 0.4)")
    ap.add_argument("--start-line", type=int, default=None,
                    help="override: 1-based line where the signature region begins (single file only)")
    ap.add_argument("--dry-run", action="store_true", help="report detection only, write nothing")
    args = ap.parse_args()

    if args.start_line is not None and len(args.files) > 1:
        ap.error("--start-line only makes sense with a single input file")

    failures = 0
    for f in args.files:
        path = Path(f)
        if not path.exists():
            print(f"ERROR: {f}: not found", file=sys.stderr)
            failures += 1
            continue
        lines = path.read_text(encoding="utf-8").splitlines()

        if args.start_line is not None:
            start = args.start_line - 1
            markers = []
        else:
            start, markers = find_start(lines, args.min_frac)
        if start is None:
            print(f"WARN: {path.name}: no signature-block marker found "
                  f"(searched: signature page follows / in witness whereof / parties have executed). "
                  f"Inspect the file tail manually and re-run with --start-line N.", file=sys.stderr)
            failures += 1
            continue

        stop = find_stop(lines, start)
        region = "\n".join(lines[start:stop]).strip() + "\n"

        stem = path.name
        for suffix in (".md",):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
        outdir = Path(args.outdir) if args.outdir else path.parent
        outdir.mkdir(parents=True, exist_ok=True)
        out = outdir / f"{stem}_sigs.md"

        print(f"{path.name}: lines {start + 1}-{stop} of {len(lines)} "
              f"({len(markers)} marker line(s) seen) -> {out}")
        if not args.dry_run:
            out.write_text(region, encoding="utf-8")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
