#!/usr/bin/env python3
"""Turn "the edits I made in the markdown sidecar" into an adeu JSON batch.

    sidecar_to_edits.py ORIGINAL.docx.md EDITED.docx.md [-o edits.json]
                        [--full-paragraph] [--min-similarity 0.3]

Paragraph-level diff of the two sidecars, then one `modify` per changed
paragraph. Hard-won details baked in:

  - order-preserving similarity alignment (DP), so a deleted paragraph does
    not steal the anchor of the paragraph after it
  - sidecar markup normalized to adeu text: `\\_` -> `_`, `## `/`- ` dropped
  - whitespace-only changes (sentence spacing) are kept
  - a paragraph deleted while an identical twin survives is deleted with
    match_mode "all" and the twin re-added after its predecessor
  - targets are trimmed to the changed span (plus context until unique) so
    untouched blanks in the same line do not show as delete + reinsert

Output defaults to adeu/<edited stem>/edits_YYYYMMDD.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import re
from pathlib import Path

DEFAULT_MIN_SIMILARITY = 0.3
SIDECAR_MARKERS = re.compile(r"^(#{1,6} |- )")
WORD_BOUNDARY = re.compile(r"\s")


def paragraphs(path: Path) -> list[str]:
    return [l.rstrip("\n") for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def clean(s: str) -> str:
    """Sidecar line -> adeu-matchable text."""
    return SIDECAR_MARKERS.sub("", s.replace("\\_", "_"))


def key(s: str) -> str:
    """Comparison key: markup-insensitive, whitespace-sensitive."""
    return clean(s).replace("*", "").strip()


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, key(a), key(b)).ratio()


def align(orig: list[str], new: list[str], oi: range, ni: range, min_sim: float) -> list[tuple[int, int]]:
    """Order-preserving pairing maximizing total similarity."""
    O, N = list(oi), list(ni)
    memo: dict[tuple[int, int], tuple[float, list[tuple[int, int]]]] = {}

    def solve(a: int, b: int) -> tuple[float, list[tuple[int, int]]]:
        if a == len(O) or b == len(N):
            return 0.0, []
        if (a, b) in memo:
            return memo[(a, b)]
        best = solve(a + 1, b)
        r = similarity(orig[O[a]], new[N[b]])
        if r >= min_sim:
            sc, pairs = solve(a + 1, b + 1)
            if sc + r > best[0]:
                best = (sc + r, [(O[a], N[b])] + pairs)
        memo[(a, b)] = best
        return best

    pairs = solve(0, 0)[1]
    # New paragraphs without a similar original replace a leftover deleted one.
    left_o = [i for i in O if i not in {i for i, _ in pairs}]
    left_n = [j for j in N if j not in {j for _, j in pairs}]
    if len(left_n) > len(left_o):
        raise SystemExit(f"cannot anchor inserted paragraph(s): {[new[j][:60] for j in left_n]}")
    return sorted(pairs + list(zip(left_o, left_n)))


def _back_to_boundary(s: str, idx: int, direction: int) -> int:
    """Move idx toward direction (-1 left / +1 right) until at whitespace or edge."""
    while 0 < idx < len(s) and not WORD_BOUNDARY.match(s[idx - 1 if direction < 0 else idx]):
        idx += direction
    return idx


def _skip_ws(s: str, idx: int, direction: int) -> int:
    while 0 < idx < len(s) and WORD_BOUNDARY.match(s[idx - 1 if direction < 0 else idx]):
        idx += direction
    return idx


def minimal_span(target: str, new: str, haystack: list[str]) -> tuple[str, str]:
    """Trim common prefix/suffix at word boundaries; widen until unique."""
    n = min(len(target), len(new))
    p = 0
    while p < n and target[p] == new[p]:
        p += 1
    s = 0
    while s < n - p and target[-1 - s] == new[-1 - s]:
        s += 1
    p = _back_to_boundary(target, p, -1)
    s = len(target) - _back_to_boundary(target, len(target) - s, +1)

    def spans(p: int, s: int) -> tuple[str, str]:
        return target[p : len(target) - s], new[p : len(new) - s]

    def unique(t: str) -> bool:
        return bool(t.strip()) and sum(h.count(t) for h in haystack) == 1

    def edge_ws(p: int, s: int) -> tuple[bool, bool]:
        # adeu matches whitespace loosely, so a span that begins or ends in a
        # spacing change must carry the neighbouring word on that side.
        t, r = spans(p, s)
        left = any(x[:1].isspace() for x in (t, r) if x)
        right = any(x[-1:].isspace() for x in (t, r) if x)
        return left, right

    def grow_left(p: int) -> int:
        return _back_to_boundary(target, _skip_ws(target, p, -1), -1)

    def grow_right(s: int) -> int:
        end = len(target) - s
        return len(target) - _back_to_boundary(target, _skip_ws(target, end, +1), +1)

    while True:
        left, right = edge_ws(p, s)
        if not ((left and p > 0) or (right and s > 0)):
            break
        if left and p > 0:
            p = grow_left(p)
        if right and s > 0:
            s = grow_right(s)

    while not unique(spans(p, s)[0]) and (p > 0 or s > 0):
        p, s = (grow_left(p), s) if p > 0 else (p, grow_right(s))
    return spans(p, s)


def build_edits(orig: list[str], new: list[str], min_sim: float, full_paragraph: bool) -> list[dict]:
    edits: list[dict] = []
    orig_clean = [clean(o) for o in orig]
    new_clean = [clean(n) for n in new]

    sm = difflib.SequenceMatcher(None, [key(x) for x in orig], [key(x) for x in new], autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        pairs = align(orig, new, range(i1, i2), range(j1, j2), min_sim) if tag != "delete" else []
        used = set()
        for i, j in pairs:
            target, replacement = orig_clean[i], new_clean[j]
            if not full_paragraph:
                target, replacement = minimal_span(target, replacement, orig_clean)
            edits.append({"type": "modify", "target_text": target, "new_text": replacement})
            used.add(i)
        for i in range(i1, i2):
            if i not in used:
                edits.append({"type": "modify", "target_text": orig_clean[i], "new_text": ""})

    # Deleting one of two verbatim-identical paragraphs is ambiguous for adeu
    # (no occurrence selector). Delete every copy, then re-add the surviving
    # copy after its predecessor via a paragraph break in new_text.
    readds = []
    for e in edits:
        if e["new_text"] or orig_clean.count(e["target_text"]) < 2:
            continue
        e["match_mode"] = "all"
        for k, t in enumerate(new_clean):
            if t == e["target_text"] and k > 0:
                readds.append({"type": "modify", "target_text": new_clean[k - 1],
                               "new_text": new_clean[k - 1] + "\n\n" + t})
    return edits + readds


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("original")
    ap.add_argument("edited")
    ap.add_argument("-o", "--output")
    ap.add_argument("--full-paragraph", action="store_true", help="target whole paragraphs, not minimal spans")
    ap.add_argument("--min-similarity", type=float, default=DEFAULT_MIN_SIMILARITY)
    args = ap.parse_args()

    orig, new = paragraphs(Path(args.original)), paragraphs(Path(args.edited))
    edits = build_edits(orig, new, args.min_similarity, args.full_paragraph)

    out = Path(args.output) if args.output else None
    if out is None:
        stem = Path(args.edited).name.lstrip(".").split(".")[0]
        out = Path("adeu") / stem / f"edits_{dt.date.today():%Y%m%d}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(edits, indent=1, ensure_ascii=False), encoding="utf-8")

    n_del = sum(1 for e in edits if not e["new_text"])
    print(f"{len(edits)} edits ({n_del} deletions) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
