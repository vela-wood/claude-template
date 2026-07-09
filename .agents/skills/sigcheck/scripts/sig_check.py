#!/usr/bin/env python3
"""Phase 5 of /sigcheck: deterministic spelling/consistency checks over the
sig_long.csv produced by sig_table.py. Reports:

  SPELLING-VARIANT   same signor (fuzzy-matched) written differently across blocks
  FIELD-MISMATCH     same signor with differing name/title/address/email across files
  MISSING-SIGNOR     signor present in some files but absent from others
  TEXT-ANOMALY       doubled words, double spaces, or stray placeholders in a field
  LOWERCASE-DRIFT    lowercase word inside an otherwise capitalized entity name

Exit code 1 if any issue is found (so it can gate a review), 0 if clean.

Usage:
  uv run sig_check.py sig_long.csv [--fuzzy 0.87] [--ignore-missing]
"""

import argparse
import csv
import re
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

FIELDS = ["entity", "by_chain", "name", "title", "address", "email"]
PLACEHOLDER_RE = re.compile(r"\[_*\s*_*\]|_{3,}|\bTBD\b|\[•\]|\[ \]", re.I)
DOUBLED_WORD_RE = re.compile(r"\b(\w+)\s+\1\b", re.I)


def norm_key(entity, name):
    base = (entity or name).lower()
    base = re.sub(r"\b(l\.?l\.?c|l\.?p|inc|corp|co|ltd)\.?\b", lambda m: m.group(0).replace(".", ""), base)
    return re.sub(r"[^a-z0-9]", "", base)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csvfile", help="sig_long.csv from sig_table.py")
    ap.add_argument("--fuzzy", type=float, default=0.87,
                    help="similarity threshold for same-signor matching (default 0.87)")
    ap.add_argument("--ignore-missing", action="store_true",
                    help="suppress MISSING-SIGNOR findings (expected when signor sets differ)")
    args = ap.parse_args()

    rows = list(csv.DictReader(Path(args.csvfile).open(encoding="utf-8")))
    if not rows:
        sys.exit("ERROR: empty csv")
    files = sorted({r["file"] for r in rows})

    # shorten filenames for display by stripping shared prefix/suffix
    import os.path
    pre = len(os.path.commonprefix(files)) if len(files) > 1 else 0
    suf = len(os.path.commonprefix([f[::-1] for f in files])) if len(files) > 1 else 0
    short = {f: (f[pre:len(f) - suf] or f) for f in files}

    # group rows into signors (same fuzzy logic as sig_table.py)
    groups = []
    for r in rows:
        k = norm_key(r["entity"], r["name"])
        if not k:
            continue
        best, best_ratio = None, 0.0
        for g in groups:
            r2 = 1.0 if k == g["key"] else SequenceMatcher(None, k, g["key"]).ratio()
            if r2 > best_ratio:
                best, best_ratio = g, r2
        if best is not None and best_ratio >= args.fuzzy:
            best["rows"].append(r)
        else:
            groups.append({"key": k, "rows": [r]})

    issues = []

    def where(rs):
        return ", ".join(f'{short[r["file"]]}#{r["block"]}' for r in rs)

    for g in groups:
        label = g["rows"][0]["entity"] or g["rows"][0]["name"]

        # spelling variants of the signor label itself
        variants = defaultdict(list)
        for r in g["rows"]:
            variants[(r["entity"] or r["name"]).strip()].append(r)
        if len(variants) > 1:
            detail = " | ".join(f'"{v}" ({where(rs)})' for v, rs in variants.items())
            issues.append(("SPELLING-VARIANT", label, detail))

        # per-field disagreement ACROSS FILES for the same signor: compare each
        # file's set of values so two co-signing directors in one file are not a
        # false mismatch; files where the field is absent entirely are skipped.
        for field in ["name", "title", "address", "email"]:
            by_file = defaultdict(set)
            for r in g["rows"]:
                if r[field]:
                    by_file[r["file"]].add(r[field].strip())
            distinct = {frozenset(v) for v in by_file.values()}
            if len(distinct) > 1:
                detail = " | ".join(
                    f'{short[fn]}: {sorted(vs)}' for fn, vs in sorted(by_file.items()))
                issues.append(("FIELD-MISMATCH", f"{label} / {field}", detail))

        # presence across files
        present = {r["file"] for r in g["rows"]}
        absent = [short[f] for f in files if f not in present]
        if absent and not args.ignore_missing:
            issues.append(("MISSING-SIGNOR", label,
                           f"signs {len(present)}/{len(files)} files; absent from: {absent}"))

        # intra-field text anomalies
        drift_seen = set()
        for r in g["rows"]:
            for field in FIELDS:
                v = r[field]
                if not v:
                    continue
                m = DOUBLED_WORD_RE.search(v)
                if m and len(m.group(1)) > 1:
                    issues.append(("TEXT-ANOMALY", f"{label} / {field}",
                                   f'doubled word "{m.group(1)}" in "{v}" ({where([r])})'))
                if PLACEHOLDER_RE.search(v) and field != "email":
                    issues.append(("TEXT-ANOMALY", f"{label} / {field}",
                                   f'placeholder/blank in "{v}" ({where([r])})'))
            # lowercase drift inside the entity name proper (before any
            # descriptive ", as Trustee of ..." / ", acting solely ..." tail)
            ent = r["entity"].split(",")[0]
            if ent in drift_seen:
                continue
            drift_seen.add(ent)
            words = re.findall(r"[A-Za-z][\w.&'-]*", ent)
            minor = {"of", "the", "and", "as", "its", "a", "an", "in", "for", "to",
                     "dated", "by", "or", "de", "da", "van", "von"}
            caps = [w for w in words if w[0].isupper()]
            lowers = [w for w in words if w[0].islower() and w.lower() not in minor]
            if lowers and len(caps) >= 2:
                issues.append(("LOWERCASE-DRIFT", label,
                               f'lowercase "{", ".join(lowers)}" in "{ent}" ({where([r])})'))

    order = {"SPELLING-VARIANT": 0, "FIELD-MISMATCH": 1, "LOWERCASE-DRIFT": 2,
             "TEXT-ANOMALY": 3, "MISSING-SIGNOR": 4}
    issues.sort(key=lambda i: (order.get(i[0], 9), i[1].lower()))

    if not issues:
        print(f"OK: {len(groups)} signor(s) across {len(files)} file(s), no issues found.")
        return
    print(f"{len(issues)} issue(s) across {len(groups)} signor(s) / {len(files)} file(s):\n")
    for kind, subject, detail in issues:
        print(f"[{kind}] {subject}\n    {detail}")
    sys.exit(1)


if __name__ == "__main__":
    main()
