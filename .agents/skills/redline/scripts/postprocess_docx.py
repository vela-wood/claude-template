#!/usr/bin/env python3
"""Post-pass for `adeu apply` output.

adeu's Markdown parser eats underscore blanks in `new_text`, and a wholly
deleted paragraph keeps its paragraph mark when a whitespace-only run
survives. Both are repaired here, on the document XML:

    adeu apply  ->  raw.docx  ->  postprocess_docx.py  ->  final.docx
                    XBLANK30X                             ______________________________
                    <w:p><w:del>..</w:del><w:r> </w:r>    <w:p><w:pPr><w:rPr><w:del/>...

Usage: postprocess_docx.py RAW.docx OUT.docx
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DOCUMENT_PART = "word/document.xml"
BLANK_TOKEN = re.compile(r"XBLANK(\d+)X")
BLANK_RUN = re.compile(r"_{2,}")
TRACK_ATTRS = ("id", "author", "date")


def w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def encode_blanks(text: str) -> str:
    """`____` -> `XBLANK4X`, a letters-only token adeu will not parse."""
    return BLANK_RUN.sub(lambda m: f"XBLANK{len(m.group())}X", text)


def restore_blanks(root: etree._Element) -> int:
    count = 0
    for t in root.iter(w("t"), w("delText")):
        if t.text and "XBLANK" in t.text:
            t.text = BLANK_TOKEN.sub(lambda m: "_" * int(m.group(1)), t.text)
            count += 1
    return count


def _is_blank_run(run: etree._Element) -> bool:
    return not any((t.text or "").strip() for t in run.findall(w("t")))


def _copy_track_attrs(src: etree._Element, dst: etree._Element) -> None:
    for attr in TRACK_ATTRS:
        value = src.get(w(attr))
        if value is not None:
            dst.set(w(attr), value)


def delete_empty_marks(root: etree._Element) -> int:
    """Mark the paragraph mark deleted when every run is deleted or blank."""
    count = 0
    for p in root.iter(w("p")):
        runs = p.findall(f".//{w('r')}")
        dels = p.findall(f".//{w('del')}")
        if not runs or not dels or p.findall(f".//{w('ins')}"):
            continue
        if p.find(f"{w('pPr')}/{w('rPr')}/{w('del')}") is not None:
            continue

        leftovers = [r for r in runs if r.getparent().tag != w("del")]
        if not all(_is_blank_run(r) for r in leftovers):
            continue

        # Fold whitespace-only leftovers into a deletion of their own.
        template = dels[0]
        for run in leftovers:
            wrap = etree.Element(w("del"))
            _copy_track_attrs(template, wrap)
            run.addprevious(wrap)
            wrap.append(run)
            for t in run.findall(w("t")):
                t.tag = w("delText")

        ppr = p.find(w("pPr"))
        if ppr is None:
            ppr = etree.Element(w("pPr"))
            p.insert(0, ppr)
        rpr = ppr.find(w("rPr"))
        if rpr is None:
            rpr = etree.SubElement(ppr, w("rPr"))
        mark = etree.SubElement(rpr, w("del"))
        _copy_track_attrs(template, mark)
        count += 1
    return count


def postprocess(raw: Path, out: Path) -> tuple[int, int]:
    with zipfile.ZipFile(raw) as zin:
        root = etree.fromstring(zin.read(DOCUMENT_PART))
        blanks = restore_blanks(root)
        marks = delete_empty_marks(root)
        xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = xml if item.filename == DOCUMENT_PART else zin.read(item.filename)
                zout.writestr(item, data)
    return blanks, marks


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    blanks, marks = postprocess(Path(argv[0]), Path(argv[1]))
    print(f"postprocess: blanks restored={blanks}, paragraph marks deleted={marks}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
