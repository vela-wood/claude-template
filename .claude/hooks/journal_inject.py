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

Re-injection: each matter injects in FULL at most once per session. If
_matter.md's mtime later changes (a parallel conversation wrote to the
matter), a SHORT change-notice is emitted instead of a second full copy, so
one context never holds two conflicting full state blocks.

Self-contained, stdlib only, fail-silent, UTF-8-forced stdout (Windows cp1252
trap). JOURNAL_HOOK_ROOT env var overrides the journal root (for tests).
"""
import sys, os, re, json, tempfile
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
MIN_SINGLE_TOKEN_LENGTH = 8  # shorter one-word names are too prone to false matches

# Flat journal buckets (root-level "X__matter.md" files, no folder/matter code).
# These have no ndcli.cfg entry, so triggers are explicit regexes, not derived
# tokens. Keyed by file stem prefix (filename minus "__matter.md").
FLAT_BUCKETS = {
    "VW-Internal-BD": re.compile(
        r"(?:\bbd\b|\bbiz\s*dev\b|business\s+development|\bcrm\b|"
        r"network\s+list|growth\s+plan|\breferrals?\b|\borigination\b)",
        re.I),
    # "article(s)" must not fire on legal usage: "Articles of Incorporation/
    # Organization", "Article X / Article IV / Article 5".
    "VW-Internal-Blogs": re.compile(
        r"(?:\bblogs?\b|\bblog\s+post\b|"
        r"\barticles?\b(?!\s+(?:of\b|[ivxlcdm]+\b|\d))|"
        r"\breddit\s+(?:answer|post|thread)\b|content\s+(?:engine|piece|calendar))",
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


def identity_matches(identity, prompt):
    """Conservatively match a matter identity in a prompt.

    Multi-token identities must appear in order after generic stopwords are
    removed. A one-token identity must be at least eight characters. This
    avoids injecting confidential state merely because a prompt contains a
    generic word from a client name (for example, "vision"). A matter code is
    always an explicit match and is handled separately in main().
    """
    identity_parts = ordered_tokens(identity)
    if not identity_parts:
        return False
    if len(identity_parts) == 1 and len(identity_parts[0]) < MIN_SINGLE_TOKEN_LENGTH:
        return False
    prompt_parts = ordered_tokens(prompt)
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


def staleness(mm, body_head):
    """Return (verdict, detail). Verdicts: CURRENT / STALE / UNSTAMPED.

    STALE = a dated entry's mtime is newer than _matter.md (mtime tripwire;
    the skill writes _matter.md last), OR the State-as-of stamp lags the
    newest entry's filename date (backstop for hand-edits that bump mtime)."""
    try:
        mm_mtime_ns = mm.stat().st_mtime_ns
    except Exception:
        return "UNSTAMPED", "state file unreadable - verify against the dated entries."
    newer, newest_date = [], ""
    for f in dated_entries(mm):
        m = re.search(r"(\d{8})", f.name)
        if m and m.group(1) > newest_date:
            newest_date = m.group(1)
        try:
            if f.stat().st_mtime_ns > mm_mtime_ns:
                newer.append(f.name)
        except Exception:
            pass
    stamp = ""
    m = re.search(r"\*\*(?:State as of|Last updated):\*\*\s*(\d{4})-?(\d{2})-?(\d{2})",
                  body_head)
    if m:
        stamp = "".join(m.groups())

    if newer:
        names = ", ".join(sorted(newer)[:8])
        more = f" (+{len(newer) - 8} more)" if len(newer) > 8 else ""
        return "STALE", (
            f"{len(newer)} dated entr{'y is' if len(newer) == 1 else 'ies are'} NEWER than the "
            f"state file: {names}{more}. The state below may not reflect them - read those "
            f"entries before relying on it, and reconcile per the journal skill before writing.")
    if stamp and newest_date and newest_date > stamp:
        return "STALE", (
            f"the State-as-of stamp ({stamp}) lags the newest dated entry ({newest_date}). "
            f"The state below may not reflect recent entries - verify before relying.")
    if not stamp:
        return "UNSTAMPED", (
            "no State-as-of stamp (pre-reconciliation format). Do NOT treat this file as a "
            "reconciled record - verify anything load-bearing against the dated entries.")
    return "CURRENT", (
        "no dated entries newer than the state file. Treat it as the settled record - do not "
        "re-derive or re-litigate what it resolves; the dated entries hold the detail.")


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


def print_digest(code, mm, rel, verdict, detail, body):
    """Digest first: it is all that survives the ~2KB persisted-output preview."""
    try:
        st = mm.stat()
        meta = f"{st.st_size / 1024:.0f} KB, file updated {datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M}"
    except Exception:
        meta = "size/mtime unavailable"
    print(f"DIGEST - matter {code} | state file: {rel} ({meta})")
    print(f"Staleness: {verdict} - {detail}")
    heads = open_headlines(body)
    if heads:
        used, shown = 0, []
        for h in heads:
            if used + len(h) > HEADLINE_BUDGET:
                shown.append(f"...(+{len(heads) - len(shown)} more)")
                break
            shown.append(h)
            used += len(h)
        print(f"Open items ({len(heads)}): " + "; ".join(shown))
    print(f"If this block was persisted to a file instead of shown inline, read {rel} "
          f"in full before doing any work on this matter.")


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    prompt = (data.get("prompt") or "").lower()
    session = data.get("session_id") or "nosession"
    if not prompt or not JOURNAL_ROOT.is_dir():
        return 0

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
            first = mm.read_text(encoding="utf-8", errors="replace").splitlines()[0]
            identities.append(header_entity(first))
        except Exception:
            pass
        code_match = re.search(r"(?<!\d)" + re.escape(code) + r"(?!\d)", prompt)
        if code_match or any(identity_matches(identity, prompt) for identity in identities):
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
        state_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        state_dir = None

    safe_session = re.sub(r"[^A-Za-z0-9]", "_", session)
    for code, mm in matched:
        try:
            cur_mtime = str(mm.stat().st_mtime_ns)
            body = mm.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        rel = f"~/legal/_journal/{mm.relative_to(JOURNAL_ROOT).as_posix()}"
        verdict, detail = staleness(mm, body[:2000])

        marker = state_dir / f"{safe_session}__{code}" if state_dir else None
        stored = None
        if marker and marker.exists():
            try:
                stored = marker.read_text().strip()
            except Exception:
                stored = ""
        if marker:
            try:
                marker.write_text(cur_mtime, encoding="utf-8")
            except Exception:
                pass

        if stored is None:
            # First injection this session: digest first, then the full state file.
            print(f'<matter-journal matter="{code}" state="{verdict}" '
                  f'source="auto-loaded by journal hook">')
            print_digest(code, mm, rel, verdict, detail, body)
            print("--- FULL STATE FILE BELOW ---")
            print()
            print(body)
            print("</matter-journal>")
        elif stored != cur_mtime:
            # Already injected, file changed since (parallel conversation wrote):
            # emit a SHORT notice, never a second full copy.
            print(f'<matter-journal-update matter="{code}" state="{verdict}" '
                  f'source="auto-loaded by journal hook">')
            print(f"The state file for matter {code} ({rel}) has CHANGED since it was "
                  f"injected earlier in this conversation - a parallel session likely wrote "
                  f"to this matter. Any earlier copy of it in this context is OUTDATED: "
                  f"re-read the file before relying on it.")
            print(f"Staleness now: {verdict} - {detail}")
            print("</matter-journal-update>")
        # stored == cur_mtime: already injected and unchanged -> stay silent.

    return 0


if __name__ == "__main__":
    # A hook must never break the user's prompt: fail silent on any error.
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
