---
name: sigcheck
description: Verify spelling and consistency of signature blocks across multiple legal agreements. Use when asked to check signature pages/blocks, compare signatories across documents, verify signor names/titles/addresses, or build a signatory table for a deal.
---

# sigcheck — signature-block consistency across agreements

Deterministic pipeline: three Python scripts in `scripts/` do the extraction,
tabulation, and checking. Do not hand-parse signature blocks; run the scripts
and review their output.

> **The scripts are starting points, not a fixed tool.** They encode the
> signature-block markers and layouts seen so far, but every deal drafts its
> execution pages a little differently. If a script finds **no signature
> blocks** (or clearly mis-parses them) on a given set of documents, that is
> expected behavior on an unfamiliar layout — **read the relevant script,
> inspect the document region it choked on, and revise the script** (new
> markers, a new layout branch, a tweaked regex) so it handles this deal.
> Prefer editing the script over hand-parsing or silently dropping a file.
> The `--start-line`/`--fuzzy` flags below are the first, cheapest fixes; a
> code edit is the next step when a flag won't do it. When you change a
> script, keep the change general (don't hardcode this deal's names) and note
> what you changed in the deliverable.

## Input rules

- Operate ONLY on converted markdown: `foo.docx.md` / `foo.pdf.md` (from
  `uv run startup.py`) or an `adeu extract` markdown of the docx. NEVER open a
  .docx directly without first asking the user.
- Prefer the newest edited markdown (`*_eYYYYMMDD*`) when one exists.
- Run all scripts with `uv run` from the repo root.

## Pipeline (run in order)

Let `SKILL=.claude/skills/sigcheck/scripts` and pick a working directory `OUT`.
**Default `OUT` to the folder where the input documents live** (so the CSVs
land alongside the source markdown). Use the scratchpad only for throwaway
exploratory runs, or another folder if the user asks.

### Phase 1-2: locate + extract signature blocks

```
uv run $SKILL/sig_extract.py FILE1.md FILE2.md ... --outdir OUT
```

- Finds the signature region (markers: "[Signature Page(s) Follow]",
  "IN WITNESS WHEREOF", "The parties/undersigned have executed this ...") and
  cuts it off at the first EXHIBIT/SCHEDULE/ANNEX/APPENDIX heading, writing
  `<name>_sigs.md` per input (strips the trailing `.md` first).
- If a file WARNs "no signature-block marker found": read the file's tail
  yourself, find the 1-based line where the signature pages start, and re-run
  that one file with `--start-line N`. Do not skip the file silently. If the
  same marker keeps failing across documents, the document uses a phrasing the
  extractor doesn't know — add it to the marker list in `sig_extract.py`
  rather than hand-passing `--start-line` for every file.
- Sanity-check each `_sigs.md` briefly (head/tail): it should contain only
  signature pages, no body text and no investor schedules.

### Phase 3-4: parse into aligned tables

```
uv run $SKILL/sig_table.py OUT/*_sigs.md --outdir OUT
```

Writes:
- `sig_long.csv` — one row per signature block: `file, block, role, entity,
  by_chain, name, title, address, email`.
- `sig_matrix.csv` — signors aligned side-by-side: one column per document,
  one row-group per signor (`signs?` row plus a sub-row for each populated
  field). Blank cell = did not sign that document. Signors are matched across
  files by normalized entity/name with fuzzy fallback (`--fuzzy`, default
  0.87), so near-miss spellings still land on one row.

Sanity-check `sig_long.csv` (a quick dump of file/role/entity/name/title):
block counts should match a spot-check of the `_sigs.md` files; a block whose
`entity` contains sentence fragments ("The parties have executed...") means
the parser hit an unfamiliar layout — inspect that `_sigs.md` region and, if
the layout is legitimate, add a branch to `sig_table.py` to parse it rather
than presenting garbage or flagging it away.

### Phase 5: consistency + spelling checks

```
uv run $SKILL/sig_check.py OUT/sig_long.csv            # full report
uv run $SKILL/sig_check.py OUT/sig_long.csv --ignore-missing
```

Finding types: `SPELLING-VARIANT` (same signor written differently),
`FIELD-MISMATCH` (name/title/address/email differs across documents; per-file
value-sets, so two co-signing directors in one document are fine),
`LOWERCASE-DRIFT` (e.g. "BOLD Capital partners"), `TEXT-ANOMALY` (doubled
words, placeholders), `MISSING-SIGNOR` (signs some documents but not others).
Exit code 1 when anything is found.

Use `--ignore-missing` when the documents naturally have different signor
sets (e.g. a COI or SPA vs. the stockholder agreements), but always show the
presence picture from `sig_matrix.csv` anyway.

## Deliverable to the user

1. The signatory matrix (`sig_matrix.csv`) — attach/point to it, and render
   the highlights as a compact table in the reply.
2. The issues list from `sig_check.py`, triaged by you: which findings look
   like genuine typos vs. benign case-style differences vs. expected absences.
3. Standard Issues/Assumptions note, including any file the extractor or
   parser struggled with.

## Known limitations

- Detection assumes markdown converted by startup.py/adeu; scanned PDFs need
  OCR first.
- Individual signors with no labeled fields (name-only pages) yield
  entity-only blocks; that is expected.
- `LOWERCASE-DRIFT` only inspects the entity name before the first comma, so
  descriptive tails (", as Trustee of ...") don't trigger it.
