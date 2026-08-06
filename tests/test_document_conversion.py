"""Routing and rendering tests for document_conversion.py."""

import datetime
import zoneinfo

import pytest
from conftest import (
    BAD_CHARSET_EML,
    HTML_ONLY_EML,
    MULTIPART_EML,
    SIMPLE_EML,
    make_digital_pdf,
    make_docx,
    make_encrypted_pdf,
    make_malformed_pdf,
    make_mbox,
    make_mht,
    make_msg,
    reference_eml_markdown,
)

import document_conversion as dc

TZ = zoneinfo.ZoneInfo("America/Chicago")


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def test_route_for_all_suffixes(tmp_path):
    assert dc.route_for(tmp_path / "a.docx") == "anydoc"
    assert dc.route_for(tmp_path / "a.pdf") == "markitdown-pdf"
    assert dc.route_for(tmp_path / "a.eml") == "email"
    assert dc.route_for(tmp_path / "a.EMLX") == "email"
    assert dc.route_for(tmp_path / "a.msg") == "extract-msg"
    assert dc.route_for(tmp_path / "a.oft") == "extract-msg"
    assert dc.route_for(tmp_path / "a.mht") == "mht"
    assert dc.route_for(tmp_path / "a.mhtml") == "mht"
    assert dc.route_for(tmp_path / "a.mbox") == "mbox"


def test_mbx_is_not_routable(tmp_path):
    assert ".mbx" not in dc.SOURCE_SUFFIXES
    with pytest.raises(ValueError):
        dc.route_for(tmp_path / "a.mbx")
    with pytest.raises(ValueError):
        dc.convert_to_markdown(tmp_path / "a.mbx")


def test_empty_output_rejected(tmp_path, monkeypatch):
    src = tmp_path / "blank.eml"
    src.write_bytes(SIMPLE_EML)
    monkeypatch.setitem(dc._CONVERTERS, ".eml", lambda s: "   \n\t  ")
    with pytest.raises(dc.ConversionError):
        dc.convert_to_markdown(src)


# ---------------------------------------------------------------------------
# .eml / .emlx — byte-identical to the pre-migration renderer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,raw",
    [
        ("simple.eml", SIMPLE_EML),
        ("multipart.eml", MULTIPART_EML),
        ("htmlonly.eml", HTML_ONLY_EML),
        ("badcharset.eml", BAD_CHARSET_EML),
        ("simple.emlx", SIMPLE_EML),
    ],
)
def test_eml_byte_identical_to_reference(tmp_path, name, raw):
    src = tmp_path / name
    src.write_bytes(raw)
    assert dc.convert_to_markdown(src).encode("utf-8") == reference_eml_markdown(
        src
    ).encode("utf-8")


def test_eml_bad_charset_uses_replacement_not_locale(tmp_path):
    src = tmp_path / "bad.eml"
    src.write_bytes(BAD_CHARSET_EML)
    out = dc.convert_to_markdown(src)
    assert "body with raw bytes �� end" in out


def test_eml_multipart_prefers_plain_text(tmp_path):
    src = tmp_path / "multi.eml"
    src.write_bytes(MULTIPART_EML)
    out = dc.convert_to_markdown(src)
    assert "# Résumé review" in out
    assert "**CC:** carol@example.com" in out
    assert "Café ménu attached." in out
    assert "<b>" not in out  # HTML part not selected


def test_eml_html_only_reports_no_text_content(tmp_path):
    src = tmp_path / "h.eml"
    src.write_bytes(HTML_ONLY_EML)
    assert dc.convert_to_markdown(src).endswith("(no text content)")


# ---------------------------------------------------------------------------
# .mht / .mhtml — HTML part is authoritative
# ---------------------------------------------------------------------------


def test_mht_html_part_wins_over_plain_text(tmp_path):
    src = tmp_path / "page.mht"
    src.write_bytes(
        make_mht(
            html_part=b"<h1>Big Heading</h1><p>Web body.</p>",
            text_part=b"plain text fallback that must lose",
        )
    )
    out = dc.convert_to_markdown(src)
    assert "# Big Heading" in out  # markdownify ATX headings
    assert "Web body." in out
    assert "plain text fallback that must lose" not in out


def test_mht_plain_text_fallback_without_html(tmp_path):
    src = tmp_path / "page.mhtml"
    src.write_bytes(make_mht(html_part=None, text_part=b"only text here"))
    out = dc.convert_to_markdown(src)
    assert "only text here" in out


def test_mht_uses_shared_renderer_header_block(tmp_path):
    src = tmp_path / "page.mht"
    src.write_bytes(make_mht(html_part=b"<p>x</p>", text_part=None))
    out = dc.convert_to_markdown(src)
    assert out.startswith("# Saved Page\n\n**From:**")
    assert "\n---\n" in out


# ---------------------------------------------------------------------------
# .mbox — each message identical to the same message as a standalone .eml
# ---------------------------------------------------------------------------


def test_mbox_messages_render_identically_to_standalone_eml(tmp_path):
    box = tmp_path / "archive.mbox"
    box.write_bytes(make_mbox([SIMPLE_EML, MULTIPART_EML]))
    eml1 = tmp_path / "m1.eml"
    eml1.write_bytes(SIMPLE_EML)
    eml2 = tmp_path / "m2.eml"
    eml2.write_bytes(MULTIPART_EML)

    out = dc.convert_to_markdown(box)
    expected = (
        dc.convert_to_markdown(eml1)
        + dc.MBOX_MESSAGE_SEPARATOR
        + dc.convert_to_markdown(eml2)
    )
    assert out == expected


def test_mbox_order_preserved(tmp_path):
    msgs = []
    for i in range(4):
        msgs.append(
            f"From: a@x.com\r\nTo: b@x.com\r\nSubject: msg {i}\r\n"
            f"Date: today\r\n\r\nbody {i}\r\n".encode()
        )
    box = tmp_path / "ordered.mbox"
    box.write_bytes(make_mbox(msgs))
    out = dc.convert_to_markdown(box)
    positions = [out.index(f"# msg {i}") for i in range(4)]
    assert positions == sorted(positions)


def test_malformed_mbox_raises(tmp_path):
    box = tmp_path / "broken.mbox"
    box.write_bytes(b"this file has no mbox From_ lines at all\n")
    with pytest.raises(dc.ConversionError):
        dc.convert_to_markdown(box)


# ---------------------------------------------------------------------------
# .msg / .oft — extract-msg with body priority plain → HTML → RTF
# ---------------------------------------------------------------------------

RECIPIENTS = [("Bob Reader", "bob@example.com", 1), ("Carol Copy", "carol@example.com", 2)]
DATE = datetime.datetime(2026, 8, 4, 9, 30, tzinfo=datetime.timezone.utc)


def test_msg_plain_text_body(tmp_path):
    src = make_msg(
        tmp_path / "plain.msg",
        subject="Plain subject",
        sender="Alice Author",
        recipients=RECIPIENTS,
        body="Plain body text.",
        date=DATE,
    )
    out = dc.convert_to_markdown(src)
    assert out.startswith("# Plain subject\n")
    assert "**From:** Alice Author" in out
    assert "**To:** Bob Reader <bob@example.com>" in out
    assert "**CC:** Carol Copy <carol@example.com>" in out
    assert "**Date:** 2026-08-04" in out
    assert out.endswith("Plain body text.")


def test_msg_html_only_body(tmp_path):
    src = make_msg(
        tmp_path / "html.msg",
        subject="Html subject",
        sender="A",
        recipients=RECIPIENTS[:1],
        html_body=b"<html><body><h1>Heading One</h1><p>Html paragraph.</p></body></html>",
    )
    out = dc.convert_to_markdown(src)
    assert "# Heading One" in out  # ATX via markdownify
    assert "Html paragraph." in out


def test_msg_compressed_rtf_only_body(tmp_path):
    import compressed_rtf

    rtf = (
        rb"{\rtf1\ansi\ansicpg1252\fromtext{\fonttbl{\f0\fswiss Arial;}}"
        rb"Confidential RTF body text.\par}"
    )
    src = make_msg(
        tmp_path / "rtf.msg",
        subject="Rtf subject",
        sender="A",
        recipients=RECIPIENTS[:1],
        compressed_rtf=compressed_rtf.compress(rtf),
    )
    out = dc.convert_to_markdown(src)
    assert "Confidential RTF body text." in out


def test_msg_ansi_properties(tmp_path):
    src = make_msg(
        tmp_path / "ansi.msg",
        subject="Ansi café",
        sender="Añdrés",
        recipients=RECIPIENTS[:1],
        body="Cuerpo café ANSI.",
        ansi=True,
    )
    out = dc.convert_to_markdown(src)
    assert "# Ansi café" in out
    assert "Cuerpo café ANSI." in out


def test_msg_encoded_nonascii_headers(tmp_path):
    src = make_msg(
        tmp_path / "unicode.msg",
        subject="Übersicht – Vertragsprüfung 契約",
        sender="Grüße GmbH",
        recipients=[("Bö", "bo@example.com", 1)],
        body="b",
    )
    out = dc.convert_to_markdown(src)
    assert "# Übersicht – Vertragsprüfung 契約" in out
    assert "**From:** Grüße GmbH" in out


def test_oft_template(tmp_path):
    src = make_msg(
        tmp_path / "template.oft",
        subject="Template subject",
        sender="A",
        recipients=RECIPIENTS[:1],
        body="Template body.",
    )
    out = dc.convert_to_markdown(src)
    assert "# Template subject" in out
    assert "Template body." in out


def test_msg_attachment_names_listed_only(tmp_path):
    src = make_msg(
        tmp_path / "attach.msg",
        subject="With attachment",
        sender="A",
        recipients=RECIPIENTS[:1],
        body="See attached.",
        attachment_names=["Exhibit A.docx"],
    )
    out = dc.convert_to_markdown(src)
    assert "**Attachments:** Exhibit A.docx" in out
    assert "attachment-bytes" not in out  # contents are out of scope


def test_msg_filename_with_spaces_and_nonascii(tmp_path):
    src = make_msg(
        tmp_path / "MOU réview – draft (2).msg",
        subject="s",
        sender="a",
        recipients=RECIPIENTS[:1],
        body="b",
    )
    assert "# s" in dc.convert_to_markdown(src)


# ---------------------------------------------------------------------------
# .docx via AnyDoc
# ---------------------------------------------------------------------------


def test_docx_headings_lists_tables_image(tmp_path):
    src = make_docx(
        tmp_path / "agreement.docx",
        heading="Master Agreement",
        paragraphs=["This Agreement is made by the parties."],
        nested_list=True,
        table=[["Term", "Value"], ["Governing Law", "Delaware"]],
        image=True,
    )
    out = dc.convert_to_markdown(src)
    assert "Master Agreement" in out
    assert "This Agreement is made by the parties." in out
    assert "First numbered item" in out
    assert "Nested numbered item" in out
    assert "Governing Law" in out and "Delaware" in out


def test_docx_nonascii_content(tmp_path):
    src = make_docx(
        tmp_path / "münchen vertrag.docx",
        heading="Vertragsübersicht",
        paragraphs=["§ 5 Kündigung — außerordentlich."],
        nested_list=False,
    )
    out = dc.convert_to_markdown(src)
    assert "Vertragsübersicht" in out
    assert "§ 5 Kündigung — außerordentlich." in out


# ---------------------------------------------------------------------------
# .pdf (gated MarkItDown route)
# ---------------------------------------------------------------------------


def test_pdf_digital_text(tmp_path):
    src = make_digital_pdf(tmp_path / "digital.pdf", ["Recital one.\nRecital two."])
    out = dc.convert_to_markdown(src)
    assert "Recital one." in out
    assert "Recital two." in out


def test_pdf_malformed_raises(tmp_path):
    src = make_malformed_pdf(tmp_path / "broken.pdf")
    with pytest.raises(Exception):
        dc.convert_to_markdown(src)


def test_pdf_encrypted_raises(tmp_path):
    src = make_encrypted_pdf(tmp_path / "locked.pdf")
    with pytest.raises(Exception):
        dc.convert_to_markdown(src)
