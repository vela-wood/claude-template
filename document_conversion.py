"""Suffix-routed document-to-Markdown conversion.

Routes (see AGENTS.md / README.md):
  .docx/.pdf     -> AnyDoc (firecrawl-anydoc, local Python API)
  .eml/.emlx     -> Python email module (existing rendering, unchanged)
  .msg/.oft      -> extract-msg + shared renderer
  .mht/.mhtml    -> Python email module + markdownify (HTML part first)
  .mbox          -> mailbox.mbox, each message through the shared renderer

Adapters return Markdown text and never touch the hash/token/OCR indexes;
orchestration (temp files, atomic replacement, index staging) lives in
startup.py. Empty or whitespace-only output is rejected. All output is
serialized by the caller as UTF-8 with LF line endings.

.mbx is intentionally unsupported: the extension is ambiguous between Unix
mbox and incompatible Eudora/Outlook Express binary formats. startup.py
notices .mbx files so the user can convert them manually.
"""

import email as _email
import mailbox
import re
from email import policy as _policy
from pathlib import Path

NATIVE_EMAIL_SUFFIXES = {".eml", ".emlx"}
MSG_SUFFIXES = {".msg", ".oft"}
MHT_SUFFIXES = {".mht", ".mhtml"}
MBOX_SUFFIXES = {".mbox"}
EMAIL_SUFFIXES = NATIVE_EMAIL_SUFFIXES | MSG_SUFFIXES | MHT_SUFFIXES | MBOX_SUFFIXES
ANYDOC_SUFFIXES = {".docx", ".pdf"}
PDF_SUFFIXES = {".pdf"}
SOURCE_SUFFIXES = ANYDOC_SUFFIXES | PDF_SUFFIXES | EMAIL_SUFFIXES
MBX_SUFFIX = ".mbx"

MBOX_MESSAGE_SEPARATOR = "\n\n---\n\n"


class ConversionError(Exception):
    """A converter produced no usable output or failed outright."""


# ---------------------------------------------------------------------------
# Shared email renderer
# ---------------------------------------------------------------------------


def render_email_markdown(
    subject, sender, to, cc, date, body, attachments=None
) -> str:
    """Render one email's headers + chosen body as Markdown.

    This is the single renderer shared by every email route. For .eml/.emlx
    it is byte-identical to the previous _convert_one_email output.
    """
    lines = [
        f"# {subject or '(no subject)'}",
        "",
        f"**From:** {sender}",
        f"**To:** {to}",
    ]
    if cc:
        lines.append(f"**CC:** {cc}")
    lines.append(f"**Date:** {date}")
    if attachments:
        lines.append(f"**Attachments:** {', '.join(attachments)}")
    lines += ["", "---", ""]
    lines.append(body if body else "(no text content)")
    return "\n".join(lines)


def _ensure_str(value) -> str | None:
    """Normalize an adapter's selected body to str before rendering."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


# ---------------------------------------------------------------------------
# .eml / .emlx (existing implementation, moved here unchanged)
# ---------------------------------------------------------------------------


def _parsed_message_to_markdown(msg) -> str:
    """Render a parsed RFC-822 message: plain-text body first."""
    body = None
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            try:
                body = part.get_content()
            except Exception:
                body = part.get_payload(decode=True).decode("utf-8", errors="replace")
            break
    return render_email_markdown(
        msg["subject"], msg["from"], msg["to"], msg["cc"], msg["date"], _ensure_str(body)
    )


def _convert_eml(source: Path) -> str:
    with open(source, "rb") as f:
        msg = _email.message_from_binary_file(f, policy=_policy.default)
    return _parsed_message_to_markdown(msg)


# ---------------------------------------------------------------------------
# .mht / .mhtml — saved web page: the HTML part is authoritative
# ---------------------------------------------------------------------------


def _convert_mht(source: Path) -> str:
    from markdownify import markdownify

    with open(source, "rb") as f:
        msg = _email.message_from_binary_file(f, policy=_policy.default)

    body = None
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            try:
                html = part.get_content()
            except Exception:
                html = part.get_payload(decode=True).decode("utf-8", errors="replace")
            body = markdownify(_ensure_str(html), heading_style="ATX")
            break
    if body is None:
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    body = part.get_content()
                except Exception:
                    body = part.get_payload(decode=True).decode(
                        "utf-8", errors="replace"
                    )
                break
    return render_email_markdown(
        msg["subject"], msg["from"], msg["to"], msg["cc"], msg["date"], _ensure_str(body)
    )


# ---------------------------------------------------------------------------
# .mbox — each message rendered identically to a standalone .eml
# ---------------------------------------------------------------------------


def _convert_mbox(source: Path) -> str:
    box = mailbox.mbox(str(source), create=False)
    try:
        rendered = []
        for key in box.iterkeys():  # file order
            msg = _email.message_from_bytes(box.get_bytes(key), policy=_policy.default)
            rendered.append(_parsed_message_to_markdown(msg))
    finally:
        box.close()
    if not rendered:
        raise ConversionError(f"no messages found in mbox: {source.name}")
    return MBOX_MESSAGE_SEPARATOR.join(rendered)


# ---------------------------------------------------------------------------
# .msg / .oft — extract-msg; body priority: plain text, HTML, RTF text
# ---------------------------------------------------------------------------

_RTF_CONTROL = re.compile(rb"\\'[0-9a-fA-F]{2}|\\[a-zA-Z]+-?\d* ?|[{}]|\\[^a-zA-Z]")


def _rtf_bytes_to_text(rtf: bytes) -> str:
    """Last-resort plain-text extraction from (already decompressed) RTF."""
    # Drop destination groups we never want rendered (fonts, colors, etc.).
    body = re.sub(rb"\{\\\*[^{}]*\}", b"", rtf)
    body = re.sub(
        rb"\{\\(?:fonttbl|colortbl|stylesheet|info|themedata)[^{}]*(?:\{[^{}]*\})*[^{}]*\}",
        b"",
        body,
    )
    body = body.replace(b"\\par", b"\n").replace(b"\\line", b"\n").replace(b"\\tab", b"\t")
    body = _RTF_CONTROL.sub(b"", body)
    return body.decode("utf-8", errors="replace")


def _decode_html_bytes(html: bytes) -> str:
    try:
        return html.decode("utf-8")
    except UnicodeDecodeError:
        return html.decode("cp1252", errors="replace")


def _msg_html_to_markdown(html) -> str:
    from markdownify import markdownify

    if isinstance(html, bytes):
        html = _decode_html_bytes(html)
    return markdownify(html, heading_style="ATX")


def _msg_rtf_to_text(msg) -> str | None:
    rtf = msg.rtfBody
    if not rtf:
        return None
    try:
        deenc = msg.deencapsulatedRtf
        if deenc is not None:
            html = getattr(deenc, "html", None)
            if html:
                return _msg_html_to_markdown(html)
            text = getattr(deenc, "text", None)
            if text:
                return _ensure_str(text)
    except Exception:
        pass
    return _rtf_bytes_to_text(rtf)


def _safe(getter):
    """extract-msg properties can raise on malformed streams; treat as absent."""
    try:
        return getter()
    except Exception:
        return None


def _convert_msg(source: Path) -> str:
    import extract_msg

    msg = extract_msg.openMsg(str(source))
    try:
        body = _ensure_str(_safe(lambda: msg.body))
        if not (body and body.strip()):
            html = _safe(lambda: msg.htmlBody)
            body = _msg_html_to_markdown(html) if html else None
        if not (body and body.strip()):
            body = _safe(lambda: _msg_rtf_to_text(msg))

        try:
            attachments = [
                getattr(a, "longFilename", None)
                or getattr(a, "shortFilename", None)
                or "(unnamed attachment)"
                for a in msg.attachments
            ]
        except Exception:
            attachments = []

        return render_email_markdown(
            msg.subject,
            msg.sender,
            msg.to,
            msg.cc,
            msg.date,
            body,
            attachments or None,
        )
    finally:
        msg.close()


# ---------------------------------------------------------------------------
# .docx — AnyDoc (local, never hosted Firecrawl or Node/npx)
# ---------------------------------------------------------------------------


def _convert_anydoc(source: Path) -> str:
    import anydoc

    # AnyDoc exposes a module-level function, not a converter object.
    return anydoc.to_markdown(str(source))


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

_CONVERTERS = {
    **{s: _convert_anydoc for s in ANYDOC_SUFFIXES},
    **{s: _convert_eml for s in NATIVE_EMAIL_SUFFIXES},
    **{s: _convert_msg for s in MSG_SUFFIXES},
    **{s: _convert_mht for s in MHT_SUFFIXES},
    **{s: _convert_mbox for s in MBOX_SUFFIXES},
}


def route_for(source: Path) -> str:
    """Human-readable route name for a source file (for reporting)."""
    suffix = source.suffix.lower()
    if suffix in ANYDOC_SUFFIXES:
        return "anydoc"
    if suffix in NATIVE_EMAIL_SUFFIXES:
        return "email"
    if suffix in MSG_SUFFIXES:
        return "extract-msg"
    if suffix in MHT_SUFFIXES:
        return "mht"
    if suffix in MBOX_SUFFIXES:
        return "mbox"
    raise ValueError(f"Unsupported source type: {source}")


def convert_to_markdown(source: Path) -> str:
    """Convert a supported source file to Markdown text.

    Raises ConversionError for empty/whitespace-only output; converter
    exceptions propagate to the caller, which records a failed result.
    """
    converter = _CONVERTERS.get(source.suffix.lower())
    if converter is None:
        raise ValueError(f"Unsupported source type: {source}")
    text = converter(source)
    if not text or not text.strip():
        raise ConversionError(f"converter produced empty output for {source.name}")
    return text
