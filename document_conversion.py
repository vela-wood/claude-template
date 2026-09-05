"""Suffix-routed document-to-Markdown conversion.

Routes (see AGENTS.md / README.md):
  .docx          -> AnyDoc (firecrawl-anydoc, local Python API)
  .pdf           -> AnyDoc, except pages whose text lives only in an invisible
                    OCR layer (render mode 3), which AnyDoc drops; those pages
                    are recovered with PyMuPDF and marked with an HTML comment
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

import codecs
import email as _email
import mailbox
import re
import threading
from email import policy as _policy
from enum import Enum
from itertools import groupby
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


def _mht_root_html_part(msg):
    """Pick the HTML part to convert per RFC 2557.

    If the multipart/related "start" parameter names a part's Content-ID
    (angle brackets stripped on both sides) and that part is text/html, it is
    the root document. Otherwise — no start parameter, no Content-ID match,
    or a non-HTML match — fall back to the first text/html part. Returns None
    when there is no HTML part at all.
    """
    start = msg.get_param("start")
    if start:
        target = start.strip().strip("<>")
        for part in msg.walk():
            cid = part.get("Content-ID")
            if (
                cid
                and cid.strip().strip("<>") == target
                and part.get_content_type() == "text/html"
            ):
                return part
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            return part
    return None


def _convert_mht(source: Path) -> str:
    from markdownify import markdownify

    with open(source, "rb") as f:
        msg = _email.message_from_binary_file(f, policy=_policy.default)

    body = None
    part = _mht_root_html_part(msg)
    if part is not None:
        try:
            html = part.get_content()
        except Exception:
            html = part.get_payload(decode=True).decode("utf-8", errors="replace")
        body = markdownify(_ensure_str(html), heading_style="ATX")
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

_RTF_SKIP_DESTINATIONS = frozenset(
    (b"fonttbl", b"colortbl", b"stylesheet", b"info", b"themedata")
)

# One token starting at a backslash; a control word's optional trailing-space
# delimiter is consumed with the word.
_RTF_TOKEN = re.compile(
    rb"\\'([0-9a-fA-F]{2})"  # hex escape: one codepage byte
    rb"|\\u(-?\d+) ?"  # unicode escape
    rb"|\\([a-zA-Z]+)(-?\d+)? ?"  # control word + numeric parameter
    rb"|\\(.)",  # control symbol
    re.DOTALL,
)


def _rtf_bytes_to_text(rtf: bytes) -> str:
    """Last-resort plain-text extraction from (already decompressed) RTF.

    Not a full parser, but escape decoding is correct on nesting-free bodies:
    \\'xx hex escapes decode with the \\ansicpgN codepage (default cp1252),
    \\uN unicode escapes decode with their \\ucN fallback characters skipped
    (default 1; negative N is N + 65536), {\\*...} and font/color/style/info/
    themedata destination groups are dropped, \\par and \\line map to newlines
    and \\tab to tab, and remaining control words and braces are stripped.
    """
    codepage = "cp1252"
    declared = re.search(rb"\\ansicpg(\d+)", rtf)
    if declared:
        candidate = "cp" + declared.group(1).decode("ascii")
        try:
            codecs.lookup(candidate)
            codepage = candidate
        except LookupError:
            pass

    out: list[str] = []
    buf = bytearray()  # pending codepage-encoded bytes (literal text and \'xx)

    def flush() -> None:
        if buf:
            out.append(buf.decode(codepage, errors="replace"))
            buf.clear()

    depth = 0
    skip_depth = None  # depth of the shallowest group being dropped, if any
    uc = 1  # current \ucN fallback length
    uc_stack: list[int] = []
    pending = 0  # \uN fallback characters still to skip

    i, n = 0, len(rtf)
    while i < n:
        c = rtf[i]
        if c == 0x7B:  # {
            flush()
            depth += 1
            uc_stack.append(uc)
            pending = 0  # group boundaries end a fallback run
            if skip_depth is None:
                head = re.match(rb"\\\*|\\([a-zA-Z]+)", rtf[i + 1 : i + 16])
                if head and (head.group(1) is None or head.group(1) in _RTF_SKIP_DESTINATIONS):
                    skip_depth = depth
            i += 1
        elif c == 0x7D:  # }
            flush()
            uc = uc_stack.pop() if uc_stack else 1
            pending = 0
            if skip_depth == depth:
                skip_depth = None
            depth -= 1
            i += 1
        elif c == 0x5C:  # backslash
            token = _RTF_TOKEN.match(rtf, i)
            if token is None:  # trailing lone backslash
                break
            hexbyte, uval, word, param, symbol = token.groups()
            if skip_depth is not None:
                pass
            elif hexbyte is not None:
                if pending:
                    pending -= 1
                else:
                    buf.append(int(hexbyte, 16))
            elif uval is not None:
                flush()
                out.append(chr(int(uval) % 65536))
                pending = uc
            elif word is not None:
                flush()
                if word == b"uc" and param is not None:
                    uc = int(param)
                elif word in (b"par", b"line"):
                    out.append("\n")
                elif word == b"tab":
                    out.append("\t")
            elif symbol in b"\\{}":  # escaped literal
                if pending:
                    pending -= 1
                else:
                    buf.extend(symbol)
            i = token.end()
        else:
            if c in (0x0D, 0x0A):
                pass  # raw newlines are not RTF content
            elif skip_depth is not None:
                pass
            elif pending:
                pending -= 1
            else:
                buf.append(c)
            i += 1
    flush()
    return "".join(out)


# Charset declarations are ASCII, so scanning raw bytes is safe. Matches both
# <meta charset=...> and <meta http-equiv="Content-Type" content="...charset=...">.
_HTML_META_CHARSET = re.compile(
    rb"<meta[^>]*?charset\s*=\s*[\"']?([A-Za-z0-9._:-]+)", re.IGNORECASE
)


def _decode_html_bytes(html: bytes) -> str:
    """Decode raw HTML bytes, honoring a declared <meta> charset when present.

    A declared charset is tried first (strict); unknown codec names or decode
    failures fall through to the undeclared behavior: strict UTF-8, then
    cp1252 with replacement.
    """
    declared = _HTML_META_CHARSET.search(html[:4096])
    if declared:
        try:
            return html.decode(declared.group(1).decode("ascii"))
        except (LookupError, UnicodeDecodeError):
            pass
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
# .pdf — AnyDoc, plus recovery of invisible OCR layers it drops
# ---------------------------------------------------------------------------
#
# A scanned PDF that went through an OCR pass carries its text in PDF render
# mode 3 (invisible) under the page image. AnyDoc drops mode-3 text, so such
# pages convert to nothing (or to a visible stamp alone), and pdfcheck counts
# the invisible text as digital, so OCR never runs either. PyMuPDF reads the
# layer: get_texttrace() spans carry `type` == render mode.
#
#   pages:   1   2   3   4   5   6          kind per page
#            N   N   R   R   R   N          N = NORMAL, R = RECOVER
#   runs:    [1-2: sub-PDF -> AnyDoc]
#            [3-5: marker + fitz text]
#            [6:   sub-PDF -> AnyDoc]       joined in page order
#
# MuPDF is not thread-safe (see startup_lib/ocr.py); convert_sources runs four
# converters at once, so every fitz call here is serialized under _FITZ_LOCK.
# AnyDoc runs outside the lock.

_INVISIBLE_RENDER_MODE = 3
_MIN_INVISIBLE_CHARS = 20
_RECOVERY_NOTE = (
    "text recovered from embedded OCR layer, not verified against the page image"
)
_RECOVERY_MARKER_RE = re.compile(
    rf"^<!-- (pages? [0-9-]+): {re.escape(_RECOVERY_NOTE)} -->$", re.MULTILINE
)
_RUN_SEPARATOR = "\n\n"
_FITZ_LOCK = threading.Lock()


class _PageKind(Enum):
    NORMAL = "normal"
    RECOVER = "recover"


def _page_label(start: int, end: int) -> str:
    """0-based inclusive run -> '"page 7"' or '"pages 3-19"' (1-based)."""
    if start == end:
        return f"page {start + 1}"
    return f"pages {start + 1}-{end + 1}"


def _recovery_marker(start: int, end: int) -> str:
    return f"<!-- {_page_label(start, end)}: {_RECOVERY_NOTE} -->"


def _span_chars(span) -> int:
    return sum(1 for c in span["chars"] if not chr(c[0]).isspace())


def _page_kind(page) -> _PageKind:
    invisible = visible = 0
    for span in page.get_texttrace():
        if span["type"] == _INVISIBLE_RENDER_MODE:
            invisible += _span_chars(span)
        else:
            visible += _span_chars(span)

    # A digital page with a small hidden watermark stays NORMAL.
    if invisible >= _MIN_INVISIBLE_CHARS and invisible > visible:
        return _PageKind.RECOVER
    return _PageKind.NORMAL


def _page_kinds(doc) -> list[_PageKind]:
    """Classify every page; any fitz trouble means 'nothing to recover' so
    the plain AnyDoc path (and its own exceptions) is preserved."""
    try:
        if doc.needs_pass:
            return []
        return [_page_kind(page) for page in doc]
    except Exception:
        return []


def _page_runs(kinds: list[_PageKind]) -> list[tuple[int, int, _PageKind]]:
    """Contiguous same-kind runs as (start, end, kind), 0-based inclusive."""
    runs = []
    pos = 0
    for kind, group in groupby(kinds):
        n = len(list(group))
        runs.append((pos, pos + n - 1, kind))
        pos += n
    return runs


def _fitz_run(doc, start: int, end: int) -> str:
    texts = [doc[i].get_text("text").strip() for i in range(start, end + 1)]
    return _RUN_SEPARATOR.join(t for t in texts if t)


def _sub_pdf(doc, start: int, end: int) -> bytes:
    sub = None
    try:
        import fitz

        sub = fitz.open()
        sub.insert_pdf(doc, from_page=start, to_page=end)
        return sub.tobytes()
    finally:
        if sub is not None:
            sub.close()


def _anydoc_run(data: bytes, fallback: str) -> str:
    """AnyDoc on one sub-PDF; an image-only run is rejected (NeedsOcrError
    since 0.2.4, UnsupportedError before), so fall back to whatever text
    fitz saw (usually nothing)."""
    import anydoc

    try:
        return anydoc.to_markdown_bytes(data)
    except (anydoc.UnsupportedError, anydoc.NeedsOcrError):
        return fallback


def _recovered_run(doc, start: int, end: int) -> str:
    text = _fitz_run(doc, start, end)
    if not text:
        return ""
    return _recovery_marker(start, end) + _RUN_SEPARATOR + text


def _convert_pdf(source: Path) -> str:
    """AnyDoc for the whole file unless some page needs layer recovery.

    Costs one extra PyMuPDF texttrace pass per page on every PDF; cheap next
    to AnyDoc. Fully digital PDFs return exactly anydoc.to_markdown(source).
    """
    import anydoc
    import fitz

    fitz.TOOLS.mupdf_display_errors(False)

    # Everything fitz touches happens under the lock; AnyDoc runs after.
    with _FITZ_LOCK:
        try:
            doc = fitz.open(str(source))
        except Exception:
            doc = None
        kinds = _page_kinds(doc) if doc is not None else []
        if _PageKind.RECOVER not in kinds:
            if doc is not None:
                doc.close()
            return anydoc.to_markdown(str(source))

        pending: list[tuple[_PageKind, bytes | None, str]] = []
        for start, end, kind in _page_runs(kinds):
            if kind is _PageKind.RECOVER:
                pending.append((kind, None, _recovered_run(doc, start, end)))
            else:
                pending.append((kind, _sub_pdf(doc, start, end), _fitz_run(doc, start, end)))
        doc.close()

    outputs = []
    for kind, data, text in pending:
        chunk = text if kind is _PageKind.RECOVER else _anydoc_run(data, text)
        if chunk.strip():
            outputs.append(chunk.strip())
    return _RUN_SEPARATOR.join(outputs)


def recovery_notes(text: str) -> tuple[str, ...]:
    """Page labels of every recovery marker in a sidecar, in order.

    >>> recovery_notes("<!-- pages 3-19: ... -->")  # doctest: +SKIP
    ('pages 3-19',)
    """
    return tuple(_RECOVERY_MARKER_RE.findall(text))


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

_CONVERTERS = {
    **{s: _convert_anydoc for s in ANYDOC_SUFFIXES},
    ".pdf": _convert_pdf,
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
