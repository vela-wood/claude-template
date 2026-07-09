#!/usr/bin/env python3
"""Phase 3-4 of /sigcheck: parse *_sigs.md files into structured signature-block
records and emit two CSVs:

  sig_long.csv    one row per signature block:
                  file, block, role, entity, by_chain, name, title, address, email
  sig_matrix.csv  signors aligned side-by-side: one column per file, rows grouped
                  by signor with sub-rows for each field (entity as written, by
                  chain, name, title, address, email). Signors are matched across
                  files by normalized entity/name with fuzzy fallback, so a blank
                  cell means "did not sign this document" and a near-miss spelling
                  still lands on the same row.

Usage:
  uv run sig_table.py FILE_sigs.md [FILE2_sigs.md ...] [--outdir DIR]
                      [--fuzzy 0.87] [--json]
"""

import argparse
import csv
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

LABEL_RE = re.compile(r"^(By|Name|Title|Its|Address|Attn|Notices(?:\s+to)?|E-?mail|Date|Phone|Fax|Facsimile)\s*:\s*(.*)$", re.I)
LABEL_SPLIT_RE = re.compile(r"\s{2,}(?=(?:By|Name|Title|Its|Address|Attn|Notices(?:\s+to)?|E-?mail|Date|Phone|Fax|Facsimile)\s*:)")
ROLE_RE = re.compile(r"^([A-Z][A-Z0-9 '&/,.-]{1,40}?)S?:$")
ROLE_WORD_RE = re.compile(
    r"^(COMPANY|CORPORATION|INVESTORS?|PURCHASERS?|KEY\s+HOLDERS?|STOCKHOLDERS?|SHAREHOLDERS?|"
    r"HOLDERS?|LENDERS?|BORROWERS?|SELLERS?|BUYERS?|GUARANTORS?|FOUNDERS?)\s*:?$")
SEPARATOR_RES = [
    re.compile(r"(?:parties|undersigned)\s+(?:has|have)\s+executed\s+this", re.I),
    re.compile(r"in\s+witness\s+whereof", re.I),
    re.compile(r"\[\s*(?:company\s+|counterpart\s+)?signature\s+pages?\s+(?:follow|to\s+follow)", re.I),
    re.compile(r"^signature\s+page\s+to\b", re.I),
]
NOISE_RES = [
    re.compile(r"^\(?signature\s+pages?\b", re.I),
    re.compile(r"^name\s+and\s+title\s+of\s+signatory$", re.I),
    re.compile(r"^_{2,}$"),
    re.compile(r"^\[?remainder\s+of\s+(this\s+)?page", re.I),
    re.compile(r"^[A-Z&]+\\\d+(\.\d+)*$"),  # law-firm doc stamps like GDSVF&H\12004103.2
]
FIELDS = ["entity", "by_chain", "name", "title", "address", "email"]


def strip_md(s: str) -> str:
    s = re.sub(r"\\([_\[\]#$%&*\\])", r"\1", s)   # unescape first
    s = re.sub(r"\*+", "", s)                      # bold/italic markers
    s = re.sub(r"(?<![\w])_+|_+(?![\w])", lambda m: "" if len(m.group(0)) < 3 else m.group(0), s)
    return re.sub(r"\s+", " ", s).strip()


def explode(text: str):
    """Flatten markdown (incl. table rows) into logical lines; split multi-field cells."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            cells = [c for c in cells if c and not re.fullmatch(r":?-{2,}:?", c)]
        else:
            cells = [line] if line else [""]
        for cell in cells:
            for part in LABEL_SPLIT_RE.split(cell):
                out.append(strip_md(part))
    return out


class Block(dict):
    def __init__(self, role):
        super().__init__(file="", block=0, role=role or "", entity="", by_chain="",
                         name="", title="", address="", email="")

    def has_content(self):
        return any(self[f] for f in FIELDS)

    def append(self, field, value):
        self[field] = f"{self[field]}; {value}" if self[field] else value


def parse_file(path: Path):
    lines = explode(path.read_text(encoding="utf-8"))
    blocks, cur, role, mode, saw_by = [], None, "", None, False

    def flush():
        nonlocal cur, mode, saw_by
        if cur is not None and cur.has_content():
            blocks.append(cur)
        cur, mode, saw_by = None, None, False

    def ensure():
        nonlocal cur
        if cur is None:
            cur = Block(role)
        return cur

    def unlabeled(line):
        b = ensure()
        nonlocal saw_by
        if mode == "address":
            b.append("address", line)
        elif saw_by and not b["name"]:
            b["name"] = line          # signer name printed under a bare "By:" line
        elif saw_by and not b["title"]:
            b["title"] = line         # title printed under the unlabeled name
        elif not (b["name"] or b["title"] or b["by_chain"]):
            b["entity"] = f"{b['entity']} {line}".strip() if b["entity"] else line
        else:
            # a bare line after a completed block starts the next entity
            flush()
            ensure()["entity"] = line

    for line in lines:
        if not line:
            continue
        # separator sentences ("The parties have executed ... as of the date ...",
        # "IN WITNESS WHEREOF, ...") end a block wherever they match; a table cell
        # sometimes merges an entity name with the separator, so keep any prefix.
        sep = None
        for rx in SEPARATOR_RES:
            m = rx.search(line)
            if m and (sep is None or m.start() < sep):
                sep = m.start()
        if sep is not None:
            prefix = re.sub(r"\b(?:[Tt]he|IN)\s*$", "", line[:sep]).strip()
            if prefix:
                unlabeled(prefix)
            flush()
            continue
        if any(rx.match(line) for rx in NOISE_RES):
            continue
        m = ROLE_RE.match(line)
        mw = ROLE_WORD_RE.match(line)
        if (m or mw) and not LABEL_RE.match(line):
            flush()
            role = (m or mw).group(1).title()
            continue
        m = LABEL_RE.match(line)
        if m:
            label, value = m.group(1).lower().replace("-", ""), m.group(2).strip()
            b = ensure()
            mode = None
            if label == "by":
                if value:  # intermediate signer, e.g. "X GP, LLC, its general partner"
                    b.append("by_chain", value)
                else:      # bare signature line: unlabeled lines below are name/title
                    saw_by = True
            elif label == "name":
                if value and b["name"]:  # second signer under the same entity heading
                    entity, bc = b["entity"], b["by_chain"]
                    flush()
                    b = ensure()
                    b["entity"], b["by_chain"] = entity, bc
                if value:
                    b["name"] = value
            elif label == "title":
                if value:
                    b.append("title", value)
            elif label == "its":
                if value:
                    b.append("title", value)
            elif label in ("address", "attn") or label.startswith("notices"):
                if value:
                    b.append("address", value)
                mode = "address"
            elif label == "email":
                if value:
                    b.append("email", value)
            # date/phone/fax: recognized so they don't pollute entity, but not tracked
            continue
        # unlabeled content line
        unlabeled(line)
    flush()

    for i, b in enumerate(blocks, 1):
        b["file"], b["block"] = path.name, i
    return blocks


def norm_key(block):
    base = block["entity"] or block["name"]
    base = base.lower()
    base = re.sub(r"\b(l\.?l\.?c|l\.?p|inc|corp|co|ltd)\.?\b", lambda m: m.group(0).replace(".", ""), base)
    return re.sub(r"[^a-z0-9]", "", base)


def group_blocks(all_blocks, fuzzy):
    """Group blocks across files by normalized entity key with fuzzy fallback."""
    groups = []  # list of {key, label, blocks}
    for b in all_blocks:
        k = norm_key(b)
        if not k:
            continue
        best, best_ratio = None, 0.0
        for g in groups:
            if k == g["key"]:
                best, best_ratio = g, 1.0
                break
            r = SequenceMatcher(None, k, g["key"]).ratio()
            if r > best_ratio:
                best, best_ratio = g, r
        if best is not None and best_ratio >= fuzzy:
            best["blocks"].append(b)
        else:
            groups.append({"key": k, "label": (b["entity"] or b["name"]), "blocks": [b]})
    return groups


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="*_sigs.md files from sig_extract.py")
    ap.add_argument("--outdir", default=".", help="directory for CSV output (default: cwd)")
    ap.add_argument("--fuzzy", type=float, default=0.87,
                    help="similarity threshold for matching the same signor across files (default 0.87)")
    ap.add_argument("--json", action="store_true", help="also write sig_long.json")
    args = ap.parse_args()

    all_blocks, file_order = [], []
    for f in args.files:
        path = Path(f)
        if not path.exists():
            sys.exit(f"ERROR: {f}: not found")
        parsed = parse_file(path)
        print(f"{path.name}: {len(parsed)} signature block(s)")
        if not parsed:
            print(f"WARN: {path.name}: 0 blocks parsed — inspect the file; "
                  f"the extraction may have missed the signature pages.", file=sys.stderr)
        all_blocks.extend(parsed)
        file_order.append(path.name)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    long_path = outdir / "sig_long.csv"
    with long_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["file", "block", "role"] + FIELDS)
        w.writeheader()
        w.writerows({k: b[k] for k in ["file", "block", "role"] + FIELDS} for b in all_blocks)

    if args.json:
        (outdir / "sig_long.json").write_text(json.dumps(all_blocks, indent=2), encoding="utf-8")

    groups = group_blocks(all_blocks, args.fuzzy)
    matrix_path = outdir / "sig_matrix.csv"
    field_rows = [("entity", "entity (as written)"), ("by_chain", "by"), ("name", "name"),
                  ("title", "title"), ("address", "address"), ("email", "email")]
    with matrix_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["signor", "field"] + file_order)
        for g in sorted(groups, key=lambda g: g["label"].lower()):
            per_file = {fn: [b for b in g["blocks"] if b["file"] == fn] for fn in file_order}
            w.writerow([g["label"], "signs?"] +
                       ["YES" if per_file[fn] else "" for fn in file_order])
            for field, disp in field_rows:
                vals = [" || ".join(b[field] for b in per_file[fn]) for fn in file_order]
                if any(vals):
                    w.writerow(["", disp] + vals)

    print(f"\n{len(all_blocks)} block(s) across {len(file_order)} file(s), "
          f"{len(groups)} distinct signor(s).")
    print(f"Wrote {long_path} and {matrix_path}")


if __name__ == "__main__":
    main()
