#!/usr/bin/env python3
"""Verify an `adeu apply` result against the text it was meant to produce.

Extracts the output DOCX in clean view (all tracked changes accepted), strips
extractor noise, and diffs it paragraph by paragraph against the expected
text (a markdown sidecar or a plain-text file). adeu's per-edit previews and
"applied" ticks are not proof; this is.

    verify_apply.py OUT.docx EXPECTED.md [--strict-spacing]

Exit 0 when every paragraph matches, 1 otherwise.
"""
from __future__ import annotations

import difflib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

STRICT_FLAG = "--strict-spacing"
PAGE_BREAK = '<w:br w:type="page"/>'
# Lines adeu emits around the body that are not document text.
EXTRACTOR_NOISE = re.compile(r"^(## ?|Page\s+of\s*|---|## Footnotes|## Endnotes|> \*\*.*)$")
MARKERS = re.compile(r"^(#{1,6} |\* |- |\d+\. )")
CROSS_REF = re.compile(r"\[~?(\d+)~?\]\(#[^)]*\)")
ANCHOR = re.compile(r"\{#[^}]*\}")
NBSP = " "


def normalize(line: str, strict_spacing: bool) -> str:
    s = line.replace(PAGE_BREAK, "").replace(NBSP, " ").strip()
    s = MARKERS.sub("", s)
    s = CROSS_REF.sub(r"\1", s)
    s = ANCHOR.sub("", s)
    s = s.replace("\\_", "_").replace("**", "").replace("*", "")
    s = re.sub(r"^_(.*)_$", r"\1", s.strip())
    if not strict_spacing:
        s = re.sub(r"\s+", " ", s)
    return s.strip()


def paragraphs(text: str, strict_spacing: bool) -> list[str]:
    out = []
    for line in text.splitlines():
        if not line.strip() or EXTRACTOR_NOISE.match(line.strip()):
            continue
        n = normalize(line, strict_spacing)
        if n:
            out.append(n)
    return out


def extract_clean(adeu_bin: Path, docx: Path) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "clean.md"
        cmd = [str(adeu_bin), "extract", str(docx), "--clean-view", "--page", "all", "--no-chrome", "-o", str(out)]
        subprocess.run(cmd, check=True, capture_output=True)
        return out.read_text(encoding="utf-8")


def find_adeu() -> Path:
    root = Path(__file__).resolve().parents[4]
    for candidate in (root / ".venv" / "bin" / "adeu", root / ".venv" / "Scripts" / "adeu.exe"):
        if candidate.exists():
            return candidate
    raise SystemExit("adeu binary not found; run 'uv sync'")


def main(argv: list[str]) -> int:
    strict = STRICT_FLAG in argv
    args = [a for a in argv if a != STRICT_FLAG]
    if len(args) != 2:
        print(__doc__, file=sys.stderr)
        return 2

    docx, expected = Path(args[0]), Path(args[1])
    got = paragraphs(extract_clean(find_adeu(), docx), strict)
    want = paragraphs(expected.read_text(encoding="utf-8"), strict)

    problems = 0
    sm = difflib.SequenceMatcher(None, got, want, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        problems += 1
        print(f"\n[{tag}]")
        for g in got[i1:i2]:
            print("  DOCX:    ", g)
        for w_ in want[j1:j2]:
            print("  EXPECTED:", w_)

    print(f"\nparagraphs: docx={len(got)} expected={len(want)} mismatched blocks={problems}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
