"""OCR classification and UTF-8 OCR-index round-trip tests for pdfcheck.py."""

from conftest import (
    make_digital_pdf,
    make_malformed_pdf,
    make_mixed_pdf,
    make_scanned_pdf,
)

import pdfcheck


def test_digital_pdf_classified(tmp_path):
    pdf = make_digital_pdf(
        tmp_path / "digital.pdf",
        ["This page has plenty of extractable digital text on it."],
    )
    result = pdfcheck.classify_pdf(pdf)
    assert result.verdict == "digital-text"
    assert not result.needs_ocr


def test_scanned_pdf_classified_needs_ocr(tmp_path):
    pdf = make_scanned_pdf(tmp_path / "scan.pdf", pages=2)
    result = pdfcheck.classify_pdf(pdf)
    assert result.verdict == "scanned-image-only"
    assert result.needs_ocr


def test_mixed_pdf_classified_needs_ocr(tmp_path):
    pdf = make_mixed_pdf(tmp_path / "mixed.pdf")
    result = pdfcheck.classify_pdf(pdf)
    assert result.verdict == "mixed/other"
    assert result.needs_ocr


def test_unreadable_pdf_gets_error_verdict(tmp_path):
    pdf = make_malformed_pdf(tmp_path / "broken.pdf")
    result = pdfcheck.classify_pdf(pdf)
    assert result.verdict.startswith("error:")
    assert not result.needs_ocr  # error is a failure, not an OCR flag


def test_ocr_index_utf8_round_trip(tmp_path):
    row = pdfcheck.index_row(
        "dossiers juridiques/契約書 – münchen № 5.pdf",
        "0abc1234",
        pdfcheck.PdfClassification(3, 3, 0, 0, 0, "scanned-image-only", "Prödücer™"),
    )
    index = {row["file"]: row}
    pdfcheck.save_ocr_index(tmp_path, index)
    raw = (tmp_path / pdfcheck.OCR_INDEX_FILENAME).read_bytes()
    assert "契約書".encode("utf-8") in raw  # UTF-8 on every platform

    loaded = pdfcheck.load_ocr_index(tmp_path)
    assert loaded == index

    # round-trip again: identical bytes
    pdfcheck.save_ocr_index(tmp_path, loaded)
    assert (tmp_path / pdfcheck.OCR_INDEX_FILENAME).read_bytes() == raw
