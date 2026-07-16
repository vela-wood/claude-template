#!/usr/bin/env python3
"""
UserPromptSubmit hook: when a user's prompt names a known matter, inject that
matter's _matter.md state file into context, led by a short DIGEST (staleness
verdict + open-item headlines) sized to survive the harness's ~2KB
persisted-output preview. The full state file follows the digest, so even when
the block is too large to inject inline, the critical layer still lands.

Staleness tripwire: any dated entry whose file mtime is newer than _matter.md
means the state file may not reflect that entry. The journal skill writes
_matter.md LAST so "state is the newest file in the folder" is the compliant
invariant. Matters last written under the old state-first order can show a
one-time false STALE until their next journal write - safe direction (it errs
toward re-verification, never toward trusting stale state). A stamp check
(State as of vs newest entry filename date) backs up the mtime check.

Re-injection: each matter injects in FULL at most once per session. The
per-session marker stores a compact change tuple -
(mm_mtime_ns, verdict, entry_count, max_entry_mtime_ns) - so a new dated
entry is noticed even when _matter.md itself is untouched. When any tuple
field later changes (a parallel conversation wrote to the matter), a SHORT
change notice is emitted instead of a second full copy, so one context never
holds two conflicting full state blocks. Marker filenames are fixed-length
hashes (no session ID or matter code on disk), replaced atomically, and
committed only AFTER the injection has been flushed to stdout - a failed
write must leave the retry path open for the next matching prompt.

Self-contained, stdlib only, fail-silent, UTF-8-forced stdout (Windows cp1252
trap). JOURNAL_HOOK_ROOT env var overrides the journal root (for tests).
"""
import sys, os, re, json, hashlib, tempfile
from datetime import datetime
from pathlib import Path

# Claude Code runs hooks with a cp1252 stdout on Windows; _matter.md contains
# characters outside cp1252 (e.g. U+2248). Force UTF-8 so print() never raises.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

JOURNAL_ROOT = Path(os.environ.get("JOURNAL_HOOK_ROOT",
                                   str(Path.home() / "legal" / "_journal")))
NDCLI_CFG = Path.home() / "ndcli.cfg"
HEADLINE_BUDGET = 1200   # chars of open-item headlines kept in the digest
MARKER_VERSION = 2       # bump when the marker snapshot schema changes

# Explicit per-matter opt-in for single-word matter identities, keyed by
# matter code. Derived one-token identities NEVER match automatically (a
# one-word client name like "Discovery" is indistinguishable from ordinary
# legal vocabulary, and a false hit leaks confidential state into an
# unrelated conversation). Adding an alias here is a deliberate privacy
# decision for that matter - keep this separate from the automatically
# derived ndcli.cfg / filename / heading identities so the opt-in is visible
# in code review.
SINGLE_TOKEN_ALIASES: dict[str, tuple[str, ...]] = {}

# Flat journal buckets (root-level "X__matter.md" files, no folder/matter code).
# These have no ndcli.cfg entry, so triggers are explicit regexes, not derived
# tokens. Keyed by file stem prefix (filename minus "__matter.md").
FLAT_BUCKETS = {
    "VW-Internal-BD": re.compile(
        r"(?:\bbd\b|\bbiz\s*dev\b|business\s+development|\bcrm\b|"
        r"network\s+list|growth\s+plan|\breferrals?\b|\borigination\b)",
        re.I),
    # A blog-specific cue must be present. Bare "article(s)" is NOT a trigger:
    # corporate work references "Articles of Incorporation", "restated
    # articles", "Article IV" constantly, and no exclusion list can enumerate
    # every legal phrase that surrounds the word.
    "VW-Internal-Blogs": re.compile(
        r"(?:\bblogs?\b|\bblog\s+post\b|"
        r"\breddit\s+(?:answer|post|thread)\b|"
        r"\bcontent\s+(?:engine|piece|calendar)\b)",
        re.I),
}

# Dropped before matching: corporate suffixes, generic deal/legal terms, common
# English words and affirmations ("sure", "okay", ...) that would false-trigger.
STOPWORDS = {
    "inc", "llc", "corp", "corporation", "company", "holdings", "group", "ltd",
    "the", "dba", "and", "lp", "llp", "plc", "incorporated", "limited",
    "series", "board", "consent", "consents", "stockholder", "stockholders",
    "shareholder", "shareholders", "financing", "round", "equity", "plan",
    "agreement", "review", "proof", "analysis", "update", "recap", "matter",
    "state", "draft", "notice", "repurchase", "note", "notes", "waterfall",
    "liquidation", "pro", "rata", "rofo", "rofr", "term", "sheet",
    "proforma", "summary", "reference", "snapshot", "engagement", "docs",
    "phase", "status", "intake", "package", "design", "build",
    "sure", "okay", "yeah", "yes", "fine", "thanks", "please", "with", "this",
    "that", "from", "your", "about", "could", "would", "should", "there",
    "these", "those", "which", "while", "where", "into", "over", "also",
}


def ordered_tokens(text):
    out = []
    for t in re.split(r"[^A-Za-z0-9]+", (text or "").lower()):
        if len(t) < 4 or t.isdigit() or t in STOPWORDS:
            continue
        out.append(t)
    return out


def explicit_token_match(token, prompt):
    """Standalone match with alphanumeric boundaries: '12345' matches
    'matter 12345', '(12345)', '12345:' but not 'ABC12345XYZ' or '612345'.
    The prompt is already lowercase, so lowercase boundaries cover both cases."""
    return re.search(
        r"(?<![a-z0-9])" + re.escape(token.lower()) + r"(?![a-z0-9])",
        prompt) is not None


def identity_matches(identity, prompt_parts):
    """Conservatively match a matter identity against pre-tokenized prompt parts.

    Multi-token identities must appear in order after generic stopwords are
    removed. Derived single-token identities never match automatically,
    regardless of length - SINGLE_TOKEN_ALIASES is the only opt-in for a
    legitimate single-name matter. A matter code is always an explicit match
    and is handled separately in process().
    """
    identity_parts = ordered_tokens(identity)
    if len(identity_parts) < 2:
        return False
    width = len(identity_parts)
    return any(
        prompt_parts[index:index + width] == identity_parts
        for index in range(len(prompt_parts) - width + 1)
    )


def cfg_entities():
    """{matter_code: entity_string} from ndcli.cfg [recent_matters]."""
    out = {}
    if not NDCLI_CFG.is_file():
        return out
    in_section = False
    for line in NDCLI_CFG.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("["):
            in_section = s.lower() == "[recent_matters]"
            continue
        if not in_section or "=" not in line:
            continue
        rhs = line.split("=", 1)[1].strip()           # "55224 - Jason Paull-Valkyrie Vision LLC"
        m = re.match(r"(\d{4,6})\s*-\s*(.+)", rhs)
        if not m:
            continue
        code, contact_entity = m.group(1), m.group(2)
        entity = contact_entity.split("-", 1)[1] if "-" in contact_entity else contact_entity
        out[code] = entity
    return out


def header_entity(first_line):
    """Strip a leading 'Matter N -' / 'N -' code and a 'Contact /' prefix."""
    s = first_line.lstrip("# ").strip()
    s = re.sub(r"^(matter\s+)?\d{4,6}\s*-\s*", "", s, flags=re.I)
    if " / " in s:
        s = s.split(" / ", 1)[1]
    return s


def filename_identities(folder):
    """Leading non-date filename prefixes, e.g. 'GOAT-Fuel_2026...'."""
    identities = []
    for f in folder.glob("*.md"):
        if f.name == "_matter.md":
            continue
        m = re.match(r"^([A-Za-z][A-Za-z0-9\-]*?)_\d{8}", f.name)
        if m:
            identities.append(m.group(1))
    return identities


def dated_entries(mm):
    """Dated-entry files for a matter. Folder matters: *.md siblings of
    _matter.md. Flat buckets (root-level X__matter.md): root X_YYYYMMDD_*.md."""
    if mm.parent != JOURNAL_ROOT:
        cand = mm.parent.glob("*.md")
    else:
        stem = mm.name[: -len("__matter.md")]
        cand = JOURNAL_ROOT.glob(stem + "_*.md")
    out = []
    for f in cand:
        if "_matter" in f.name:
            continue
        if re.search(r"\d{8}", f.name):
            out.append(f)
    return out


def staleness(mm, mm_mtime_ns, body_head):
    """One pass over the dated entries.

    Returns (verdict, detail, entry_count, max_entry_mtime_ns). Verdicts:
    CURRENT / STALE / UNSTAMPED.

    STALE = a dated entry's mtime is newer than _matter.md (mtime tripwire;
    the skill writes _matter.md last), OR the State-as-of stamp lags the
    newest entry's filename date (backstop for hand-edits that bump mtime).

    entry_count and max_entry_mtime_ns feed the per-session change tuple, so
    a new or rewritten dated entry is noticed even when _matter.md itself is
    untouched. An individual entry whose stat fails is ignored for the mtime
    values (but still counted), matching the previous tolerance."""
    newer, newest_date = [], ""
    entry_count = 0
    max_entry_mtime_ns = 0
    for f in dated_entries(mm):
        entry_count += 1
        m = re.search(r"(\d{8})", f.name)
        if m and m.group(1) > newest_date:
            newest_date = m.group(1)
        try:
            entry_mtime_ns = f.stat().st_mtime_ns
        except Exception:
            continue
        if entry_mtime_ns > max_entry_mtime_ns:
            max_entry_mtime_ns = entry_mtime_ns
        if entry_mtime_ns > mm_mtime_ns:
            newer.append(f.name)
    stamp = ""
    m = re.search(r"\*\*(?:State as of|Last updated):\*\*\s*(\d{4})-?(\d{2})-?(\d{2})",
                  body_head)
    if m:
        stamp = "".join(m.groups())

    if newer:
        names = ", ".join(sorted(newer)[:8])
        more = f" (+{len(newer) - 8} more)" if len(newer) > 8 else ""
        verdict, detail = "STALE", (
            f"{len(newer)} dated entr{'y is' if len(newer) == 1 else 'ies are'} NEWER than the "
            f"state file: {names}{more}. The state below may not reflect them - read those "
            f"entries before relying on it, and reconcile per the journal skill before writing.")
    elif stamp and newest_date and newest_date > stamp:
        verdict, detail = "STALE", (
            f"the State-as-of stamp ({stamp}) lags the newest dated entry ({newest_date}). "
            f"The state below may not reflect recent entries - verify before relying.")
    elif not stamp:
        verdict, detail = "UNSTAMPED", (
            "no State-as-of stamp (pre-reconciliation format). Do NOT treat this file as a "
            "reconciled record - verify anything load-bearing against the dated entries.")
    else:
        verdict, detail = "CURRENT", (
            "no dated entries newer than the state file. Treat it as the settled record - do not "
            "re-derive or re-litigate what it resolves; the dated entries hold the detail.")
    return verdict, detail, entry_count, max_entry_mtime_ns


def marker_path(state_dir, session, code):
    """Fixed-length hashed marker name: collision-resistant, and neither the
    session ID nor the matter code appears in the filename."""
    ident = "\x00".join([str(JOURNAL_ROOT), session, code])
    return state_dir / hashlib.sha256(ident.encode("utf-8")).hexdigest()


def read_marker(marker):
    """Stored snapshot dict, or None when the marker is absent, legacy, or
    invalid - all of which mean 'emit the full state'. One extra full
    injection for an unreadable marker is safer than treating it as proof of
    delivery."""
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        if data.get("version") != MARKER_VERSION:
            return None
        snap = data["snapshot"]
        return {
            "mm_mtime_ns": int(snap["mm_mtime_ns"]),
            "verdict": str(snap["verdict"]),
            "entry_count": int(snap["entry_count"]),
            "max_entry_mtime_ns": int(snap["max_entry_mtime_ns"]),
        }
    except Exception:
        return None


def write_marker(marker, snapshot):
    """Atomic, owner-only marker write. Failure is silent: injection was
    already delivered, it may just repeat on a later prompt."""
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(marker.parent))  # 0o600 where supported
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": MARKER_VERSION, "snapshot": snapshot}, fh)
        os.replace(tmp_path, marker)
    except Exception:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def digest_lines(code, mm_stat, rel, verdict, detail, body):
    """Digest first: it is all that survives the ~2KB persisted-output preview."""
    meta = (f"{mm_stat.st_size / 1024:.0f} KB, file updated "
            f"{datetime.fromtimestamp(mm_stat.st_mtime):%Y-%m-%d %H:%M}")
    lines = [f"DIGEST - matter {code} | state file: {rel} ({meta})",
             f"Staleness: {verdict} - {detail}"]
    heads = open_headlines(body)
    if heads:
        used, shown = 0, []
        for h in heads:
            if used + len(h) > HEADLINE_BUDGET:
                shown.append(f"...(+{len(heads) - len(shown)} more)")
                break
            shown.append(h)
            used += len(h)
        lines.append(f"Open items ({len(heads)}): " + "; ".join(shown))
    lines.append(f"If this block was persisted to a file instead of shown inline, read {rel} "
                 f"in full before doing any work on this matter.")
    return lines


def open_headlines(body):
    """One-line headlines for the digest, from the '## Open items' section."""
    m = re.search(r"^## Open items\s*$(.*?)(?=^## |\Z)", body, re.M | re.S)
    if not m:
        return []
    out = []
    pattern = r"^(?:(\d+)\.|[-*+])\s+\*\*(.+?)\*\*"
    for num, title in re.findall(pattern, m.group(1), re.M):
        t = re.sub(r"\s+", " ", title).strip()
        marker = f"#{num}" if num else "-"
        out.append(f"{marker} {t[:80]}")
    return out


def process(data, stream):
    raw_prompt = data.get("prompt") or ""
    # All current matching is case-insensitive on the normalized prompt; the
    # raw prompt is retained so a future case-aware single-token policy can be
    # evaluated without re-plumbing (capitalization is NOT a boundary today).
    prompt = raw_prompt.lower()
    session = data.get("session_id") or "nosession"
    if not prompt or not JOURNAL_ROOT.is_dir():
        return 0
    prompt_parts = ordered_tokens(prompt)

    cfg = cfg_entities()
    matched = []
    for folder in sorted(JOURNAL_ROOT.iterdir()):
        if not folder.is_dir():
            continue
        mm = folder / "_matter.md"
        if not mm.is_file():
            continue                                   # only inject matters with a state file
        code = folder.name
        identities = [cfg.get(code, "")]
        identities.extend(filename_identities(folder))
        try:
            # Discovery needs only the heading line; the full body is read
            # later, and only for a matter that actually injects.
            with open(mm, encoding="utf-8", errors="replace") as fh:
                identities.append(header_entity(fh.readline()))
        except Exception:
            pass
        if (explicit_token_match(code, prompt)
                or any(identity_matches(identity, prompt_parts) for identity in identities)
                or any(explicit_token_match(alias, prompt)
                       for alias in SINGLE_TOKEN_ALIASES.get(code, ()))):
            matched.append((code, mm))
        if len(matched) >= 3:                          # defensive cap
            break

    # Flat buckets: root-level "X__matter.md" files matched by explicit triggers.
    for stem, pat in FLAT_BUCKETS.items():
        mm = JOURNAL_ROOT / f"{stem}__matter.md"
        if mm.is_file() and pat.search(prompt):
            matched.append((stem, mm))

    if not matched:
        return 0

    state_dir = Path(tempfile.gettempdir()) / "claude_journal_hook"
    try:
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except Exception:
        state_dir = None

    for code, mm in matched:
        try:
            mm_stat = mm.stat()
            with open(mm, encoding="utf-8", errors="replace") as fh:
                head = fh.read(2048)                   # enough for the State-as-of stamp
        except Exception:
            continue
        rel = f"~/legal/_journal/{mm.relative_to(JOURNAL_ROOT).as_posix()}"
        verdict, detail, entry_count, max_entry_mtime_ns = staleness(
            mm, mm_stat.st_mtime_ns, head)
        snapshot = {
            "mm_mtime_ns": mm_stat.st_mtime_ns,
            "verdict": verdict,
            "entry_count": entry_count,
            "max_entry_mtime_ns": max_entry_mtime_ns,
        }

        marker = marker_path(state_dir, session, code) if state_dir else None
        stored = read_marker(marker) if marker else None

        if stored == snapshot:
            continue    # already injected and nothing changed: stay silent

        if stored is None:
            # First injection this session: digest first, then the full state file.
            try:
                body = mm.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            lines = [f'<matter-journal matter="{code}" state="{verdict}" '
                     f'source="auto-loaded by journal hook">']
            lines.extend(digest_lines(code, mm_stat, rel, verdict, detail, body))
            lines.extend(["--- FULL STATE FILE BELOW ---", "", body, "</matter-journal>"])
        else:
            # Already injected, something changed since: emit a SHORT notice,
            # never a second full copy.
            if stored["mm_mtime_ns"] != snapshot["mm_mtime_ns"]:
                reason = (
                    f"The state file for matter {code} ({rel}) has CHANGED since it was "
                    f"injected earlier in this conversation - a parallel session likely wrote "
                    f"to this matter. Any earlier copy of it in this context is OUTDATED: "
                    f"re-read the file before relying on it.")
            else:
                reason = (
                    f"The dated journal entries for matter {code} have changed since its state "
                    f"file ({rel}) was injected earlier in this conversation, but the state "
                    f"file itself has not been rewritten. The copy injected earlier may no "
                    f"longer be the settled record - check the staleness verdict below.")
            lines = [f'<matter-journal-update matter="{code}" state="{verdict}" '
                     f'source="auto-loaded by journal hook">',
                     reason,
                     f"Staleness now: {verdict} - {detail}",
                     "</matter-journal-update>"]

        out = "\n".join(lines) + "\n"
        # The marker commits only after the injection has actually reached the
        # stream: a failed write or flush must leave the retry path open.
        try:
            stream.write(out)
            stream.flush()
        except Exception:
            return 0
        if marker:
            write_marker(marker, snapshot)

    return 0


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    return process(data, sys.stdout)


if __name__ == "__main__":
    # A hook must never break the user's prompt: fail silent on any error.
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
