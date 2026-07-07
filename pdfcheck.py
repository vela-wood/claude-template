"""Deterministic PDF classification: born-digital text vs scanned raster.

Inspects PDF object structure (no rendering, no OCR). Per page:
  - chars: extractable text characters
  - cov:   max fraction of page area covered by a single raster image
Page classes:
  - image-only scan: cov >= 0.8 and chars < 20
  - scan + OCR layer: cov >= 0.8 and chars >= 20
  - digital text: cov < 0.8 and chars >= 20
  - blank/other: neither
File verdict = majority of pages (scan classes win ties). Files whose
verdict is "scanned-image-only" or "mixed/other" need OCR before text
conversion will produce anything useful.

Importable API: classify_pdf(), PdfClassification, load/save_class_index().
CLI: uv run pdf_classifier.py [root] [-o output.csv]
"""

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path

import fitz

fitz.TOOLS.mupdf_display_errors(False)

PDF_CLASS_INDEX_FILENAME = ".pdf_class_index.csv"
NEEDS_OCR_VERDICTS = {"scanned-image-only", "mixed/other"}
_FULL_PAGE_IMAGE_COVERAGE = 0.8
_MIN_TEXT_CHARS = 20


@dataclass
class PdfClassification:
    pages: int
    pg_image_only: int
    pg_scan_ocr: int
    pg_digital: int
    pg_other: int
    verdict: str
    producer: str

    @property
    def needs_ocr(self) -> bool:
        return self.verdict in NEEDS_OCR_VERDICTS


def classify_pdf(path: Path) -> PdfClassification:
    """Classify a single PDF. Unreadable files get verdict 'error: ...'."""
    try:
        doc = fitz.open(path)
    except Exception as exc:
        return PdfClassification(0, 0, 0, 0, 0, f"error: {exc}", "")

    img_only = scan_ocr = digital = other = 0
    for page in doc:
        page_area = abs(page.rect)
        chars = len(page.get_text("text").strip())
        cov = 0.0
        try:
            for info in page.get_image_info():
                bbox = fitz.Rect(info["bbox"])
                if page_area:
                    cov = max(cov, abs(bbox & page.rect) / page_area)
        except Exception:
            pass
        if cov >= _FULL_PAGE_IMAGE_COVERAGE and chars < _MIN_TEXT_CHARS:
            img_only += 1
        elif cov >= _FULL_PAGE_IMAGE_COVERAGE:
            scan_ocr += 1
        elif chars >= _MIN_TEXT_CHARS:
            digital += 1
        else:
            other += 1

    n = doc.page_count or 1
    if img_only + scan_ocr >= n / 2:
        verdict = "scan+ocr" if scan_ocr > img_only else "scanned-image-only"
    elif digital >= n / 2:
        verdict = "digital-text"
    else:
        verdict = "mixed/other"
    producer = ((doc.metadata or {}).get("producer") or "").strip()
    result = PdfClassification(
        doc.page_count, img_only, scan_ocr, digital, other, verdict, producer
    )
    doc.close()
    return result


# ---------------------------------------------------------------------------
# Index I/O (same shape as startup.py's hash/token indexes)
# ---------------------------------------------------------------------------

_INDEX_COLUMNS = [
    "file",
    "hash",
    "pages",
    "pg_image_only",
    "pg_scan_ocr",
    "pg_digital",
    "pg_other",
    "verdict",
    "needs_ocr",
    "producer",
]


def load_class_index(root: Path) -> dict[str, dict[str, str]]:
    """Load .pdf_class_index.csv → {pdf_relative_path: row dict}."""
    index_path = root / PDF_CLASS_INDEX_FILENAME
    index: dict[str, dict[str, str]] = {}
    if not index_path.exists():
        return index
    with open(index_path, newline="") as f:
        for row in csv.DictReader(f):
            index[row["file"]] = row
    return index


def save_class_index(root: Path, index: dict[str, dict[str, str]]) -> None:
    """Write .pdf_class_index.csv from {pdf_relative_path: row dict}."""
    index_path = root / PDF_CLASS_INDEX_FILENAME
    with open(index_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_INDEX_COLUMNS)
        writer.writeheader()
        for rel_path in sorted(index):
            writer.writerow(index[rel_path])


def index_row(rel: str, file_hash: str, c: PdfClassification) -> dict[str, str]:
    """Build a class-index row for one classified PDF."""
    return {
        "file": rel,
        "hash": file_hash,
        "pages": str(c.pages),
        "pg_image_only": str(c.pg_image_only),
        "pg_scan_ocr": str(c.pg_scan_ocr),
        "pg_digital": str(c.pg_digital),
        "pg_other": str(c.pg_other),
        "verdict": c.verdict,
        "needs_ocr": str(c.needs_ocr),
        "producer": c.producer[:80],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", default=".", help="folder to scan")
    parser.add_argument("-o", "--output", help="write per-file CSV here")
    args = parser.parse_args()

    root = Path(args.root)
    rows = []
    counts: dict[str, int] = {}
    for pdf in sorted(root.rglob("*.pdf")):
        c = classify_pdf(pdf)
        rows.append(index_row(str(pdf.relative_to(root)), "", c))
        counts[c.verdict] = counts.get(c.verdict, 0) + 1

    if args.output:
        with open(args.output, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_INDEX_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

    needs = sum(1 for r in rows if r["needs_ocr"] == "True")
    print("Summary:")
    for verdict, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {verdict}: {count}")
    print(f"  needs_ocr: {needs} of {len(rows)}")


if __name__ == "__main__":
    main()
