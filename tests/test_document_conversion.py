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
    assert dc.route_for(tmp_path / "a.pdf") == "anydoc"
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


def make_related_mht(start: str | None, parts: list[tuple[bytes, str, bytes]]) -> bytes:
    """multipart/related with per-part Content-IDs; parts = (type, cid, body).

    Local builder: conftest's make_mht cannot express start/Content-ID.
    """
    ctype = b"Content-Type: multipart/related; boundary=RELBOUND"
    if start is not None:
        ctype += f'; start="<{start}>"'.encode()
    rendered = []
    for content_type, cid, body in parts:
        rendered.append(
            b"--RELBOUND\r\nContent-Type: " + content_type
            + f"\r\nContent-ID: <{cid}>\r\n\r\n".encode()
            + body
            + b"\r\n"
        )
    return (
        b"From: <Saved by Test>\r\n"
        b"Subject: Saved Page\r\n"
        b"Date: Tue, 04 Aug 2026 14:00:00 -0500\r\n"
        b"MIME-Version: 1.0\r\n" + ctype + b"\r\n\r\n"
        + b"".join(rendered)
        + b"--RELBOUND--\r\n"
    )


def test_mht_start_parameter_selects_root_html_part(tmp_path):
    src = tmp_path / "framed.mht"
    src.write_bytes(
        make_related_mht(
            start="root@page",
            parts=[
                (b"text/html; charset=utf-8", "frame@page", b"<p>frame wrapper only</p>"),
                (b"text/html; charset=utf-8", "root@page", b"<h1>Real Root</h1><p>Actual page content.</p>"),
            ],
        )
    )
    out = dc.convert_to_markdown(src)
    assert "# Real Root" in out
    assert "Actual page content." in out
    assert "frame wrapper only" not in out


def test_mht_without_start_parameter_keeps_first_html_part(tmp_path):
    src = tmp_path / "nostart.mht"
    src.write_bytes(
        make_related_mht(
            start=None,
            parts=[
                (b"text/html; charset=utf-8", "one@page", b"<p>first part wins</p>"),
                (b"text/html; charset=utf-8", "two@page", b"<p>second part loses</p>"),
            ],
        )
    )
    out = dc.convert_to_markdown(src)
    assert "first part wins" in out
    assert "second part loses" not in out


def test_mht_unmatched_start_falls_back_to_first_html_part(tmp_path):
    src = tmp_path / "badstart.mht"
    src.write_bytes(
        make_related_mht(
            start="missing@page",
            parts=[
                (b"text/html; charset=utf-8", "one@page", b"<p>first part wins</p>"),
                (b"text/html; charset=utf-8", "two@page", b"<p>second part loses</p>"),
            ],
        )
    )
    out = dc.convert_to_markdown(src)
    assert "first part wins" in out
    assert "second part loses" not in out


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
# HTML byte decoding — declared <meta> charset honored, fallbacks preserved
# ---------------------------------------------------------------------------


def test_decode_html_declared_shift_jis():
    html = (
        '<html><head><meta charset="shift_jis"></head>'
        "<body><p>日本語の本文です。</p></body></html>"
    ).encode("shift_jis")
    assert "日本語の本文です。" in dc._decode_html_bytes(html)


def test_decode_html_declared_windows_1251_http_equiv():
    html = (
        '<html><head><meta http-equiv="Content-Type" '
        'content="text/html; charset=windows-1251"></head>'
        "<body><p>Привет, мир!</p></body></html>"
    ).encode("cp1251")
    assert "Привет, мир!" in dc._decode_html_bytes(html)


def test_decode_html_undeclared_utf8_unchanged():
    assert dc._decode_html_bytes("<p>café ménu</p>".encode("utf-8")) == "<p>café ménu</p>"


def test_decode_html_undeclared_cp1252_unchanged():
    assert dc._decode_html_bytes(b"<p>caf\xe9</p>") == "<p>café</p>"


def test_decode_html_unknown_declared_charset_falls_back():
    assert (
        dc._decode_html_bytes(b'<meta charset="not-a-charset"><p>caf\xe9</p>')
        == '<meta charset="not-a-charset"><p>café</p>'
    )


def test_msg_html_body_declared_shift_jis(tmp_path):
    html = (
        '<html><head><meta charset="shift_jis"></head>'
        "<body><h1>契約書</h1><p>本文はここにあります。</p></body></html>"
    ).encode("shift_jis")
    src = make_msg(
        tmp_path / "sjis.msg",
        subject="Sjis subject",
        sender="A",
        recipients=RECIPIENTS[:1],
        html_body=html,
    )
    out = dc.convert_to_markdown(src)
    assert "# 契約書" in out
    assert "本文はここにあります。" in out


# ---------------------------------------------------------------------------
# RTF escape decoding — last-resort path for non-encapsulated RTF bodies
# ---------------------------------------------------------------------------


def test_rtf_hex_escapes_decode_with_ansicpg():
    rtf = rb"{\rtf1\ansi\ansicpg1252 caf\'e9 au lait\par}"
    assert dc._rtf_bytes_to_text(rtf) == "café au lait\n"


def test_rtf_hex_escapes_decode_multibyte_codepage():
    # cp932 (Shift-JIS): 0x93 0xFA = 日, 0x96 0x7B = 本
    rtf = rb"{\rtf1\ansi\ansicpg932 \'93\'fa\'96\'7b}"
    assert dc._rtf_bytes_to_text(rtf) == "日本"


def test_rtf_unicode_escapes_skip_fallback_chars():
    # 26085 = 日, 26412 = 本
    rtf = b"{\\rtf1\\ansi\\ansicpg1252\\uc1 \\u26085?\\u26412? end}"
    out = dc._rtf_bytes_to_text(rtf)
    assert "日本 end" in out
    assert "?" not in out  # fallback char must not be duplicated


def test_rtf_unicode_uc2_skips_two_fallback_bytes():
    # \uc2: the two \'xx fallback bytes after the unicode escape must be
    # skipped, not decoded
    rtf = b"{\\rtf1\\ansi\\ansicpg1252\\uc2 \\u26085\\'93\\'fa done}"
    out = dc._rtf_bytes_to_text(rtf)
    assert "日done" in out.replace(" ", "")
    assert out.count("日") == 1


def test_rtf_negative_unicode_values():
    # -1279 + 65536 = 64257 = U+FB01 (LATIN SMALL LIGATURE FI)
    rtf = rb"{\rtf1\ansi\ansicpg1252\uc1 \u-1279?le}"
    out = dc._rtf_bytes_to_text(rtf)
    assert "ﬁle" in out
    assert "?" not in out


def test_rtf_destination_groups_still_dropped():
    rtf = (
        rb"{\rtf1\ansi\ansicpg1252{\fonttbl{\f0\fswiss Arial;}}"
        rb"{\colortbl;\red0\green0\blue0;}{\*\generator Hidden Tool}"
        rb"Visible body.\tab tabbed\line next}"
    )
    out = dc._rtf_bytes_to_text(rtf)
    assert "Visible body.\ttabbed\nnext" in out
    assert "Arial" not in out
    assert "Hidden Tool" not in out


def test_rtf_escaped_braces_and_backslash():
    rtf = rb"{\rtf1\ansi a \{b\} c\\d}"
    assert dc._rtf_bytes_to_text(rtf) == "a {b} c\\d"


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
# .pdf via AnyDoc
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
