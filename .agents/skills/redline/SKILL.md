# Redline Skill

## Overview

This skill runs Adeu from the repo-local environment. Use it when you need to:
- apply edits to a `.docx` file (written out as Word track changes)
- inspect the differences between two `.docx` versions as text/JSON

Note: `adeu diff` outputs text/JSON only — it never produces a .docx. If the user wants a comparison document a human can open in Word with track changes, use the /compare skill instead.

Never use this skill to create a new blank Word document from scratch.

usage: adeu [-h] [-v] [--debug]
            {extract,diff,apply,accept-all,markup,sanitize,help} ...

Adeu: Agentic DOCX Redlining Engine (version 2.1.0+e95dbf2)

positional arguments:
  {extract,diff,apply,accept-all,markup,sanitize,help}
                        Subcommands
    extract             Extract raw text from a DOCX file
    diff                Compare two files (DOCX vs DOCX/Text)
    apply               Apply edits to a DOCX
    accept-all          Accept all tracked changes and remove all comments
                        (finalize a document)
    markup              Apply edits to a document and output as CriticMarkup
                        Markdown
    sanitize            Strip metadata and sensitive information from a DOCX
                        file
    help                Show help for adeu or a subcommand

The cli help should be visible above, fix all errors until the cli works.

## Required Environment

- Root repo dependencies must be installed into `.venv`.
- Run `uv sync` at the repo root for standard setup.
- `adeu` is declared in the root `redline` dependency group, and that group is enabled by default.
- If `.venv/bin/adeu` is missing, run `uv sync` at the repo root.

## Command Runner

Always execute Adeu through the repo wrapper, launched with `uv run`:

```bash
uv run .claude/skills/redline/scripts/run_redline.py --version
```

`uv run` activates the repo `.venv`; the wrapper then loads `.env`, resolves the
repo root dynamically, and executes the installed CLI from
`<repo-root>/.venv/bin/adeu` (or `.venv\Scripts\adeu.exe`).

For `apply` the wrapper adds three things (disable all of them with `--raw`):

```
edits.json ──encode blanks──▶ edits.encoded.json ──adeu──▶ out.raw.docx ──post-pass──▶ out.docx
```

1. **Author default.** Without `--author`, the track-changes author is `ADEU_AUTHOR` from the environment or `.env`, else `Andrew Lin` (per USERPREFS). Never let adeu fall through to the OS username.
2. **Underscore blanks survive.** Runs of `__` in JSON `new_text` are encoded as `XBLANK<n>X` before adeu sees them and turned back into underscores in the output XML. Without this, adeu's Markdown parser pairs up underscores as italic markers and blanks vanish.
3. **Deleted paragraphs disappear on accept.** When every run of a paragraph is deleted, the paragraph mark is marked deleted too, so no empty bullet or numbered item is left behind.

The post-pass needs `-o`; without it the wrapper warns and runs adeu unmodified.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/run_redline.py` | adeu wrapper (see above) |
| `scripts/sidecar_to_edits.py ORIG.docx.md EDITED.docx.md [-o edits.json]` | Build a JSON batch from edits made in a markdown sidecar |
| `scripts/verify_apply.py OUT.docx EXPECTED.md [--strict-spacing]` | Prove the accepted text of an output equals the intended text |
| `scripts/postprocess_docx.py RAW.docx OUT.docx` | Standalone post-pass (the wrapper runs it for you) |

## Workflows

### 1. Extract clean markdown

```bash
uv run .claude/skills/redline/scripts/run_redline.py extract contract.docx --page all -o adeu/contract/extract_YYYYMMDD.md
```

Always pass `--page all`: the default is synthetic page 1 only, and a partial extract cannot round-trip. Add `--clean-view` for the accepted-changes text. The output carries `> **File Path:**` and `> **Protection:**` header lines (drop both with `--no-chrome`) and footer lines (`## `, `Page  of `, `---`, `## Footnotes`, `## Endnotes`) that are not document text; strip them before comparing.

### 2. Diff two DOCX files

```bash
uv run .claude/skills/redline/scripts/run_redline.py diff v1.docx v2.docx
uv run .claude/skills/redline/scripts/run_redline.py diff v1.docx revised.txt --json
```

### 3. Apply redlines to a DOCX

```bash
uv run .claude/skills/redline/scripts/run_redline.py apply contract.docx adeu/contract/edits_YYYYMMDD.json -o contract_eYYYYMMDD.docx
uv run .claude/skills/redline/scripts/verify_apply.py contract_eYYYYMMDD.docx adeu/contract/intended_YYYYMMDD.md
```

Verification is mandatory. adeu's per-edit ✅ and CriticMarkup previews are not proof: previews show drifted context once earlier edits shift offsets, and an edit whose blanks were eaten still reports success. `verify_apply.py` extracts the accepted-changes text and diffs it paragraph by paragraph against what you intended (a sidecar, or the clean extract you edited). Exit code 0 means every paragraph matches.

### 4. Port edits made in a markdown sidecar back to the DOCX

The recurring "I edited `.foo.docx.md`, apply it to `foo.docx`" task:

```bash
uv run .claude/skills/redline/scripts/sidecar_to_edits.py .foo.docx.md .foo_eYYYYMMDD.docx.md
uv run .claude/skills/redline/scripts/run_redline.py apply foo.docx adeu/foo_eYYYYMMDD/edits_YYYYMMDD.json -o foo_eYYYYMMDD.docx
uv run .claude/skills/redline/scripts/verify_apply.py foo_eYYYYMMDD.docx .foo_eYYYYMMDD.docx.md
```

The generator diffs paragraphs, aligns changed blocks by similarity so a deleted paragraph does not steal its neighbour's anchor, trims each target to the changed span plus enough context to be unique, keeps sentence-spacing changes, and handles the identical-twin deletion case below. Use `--full-paragraph` if span targets misbehave on a document.

## edits.json schema

```json
[
  {"type": "modify", "target_text": "exact text to find", "new_text": "replacement", "comment": "optional",
   "match_mode": "strict | first | all", "regex": false},
  {"type": "accept", "target_id": "Chg:12", "comment": "optional rationale"},
  {"type": "reject", "target_id": "Chg:13", "comment": "optional rationale"},
  {"type": "reply", "target_id": "Com:5", "text": "reply text"},
  {"type": "insert_row", "target_text": "text in anchor row", "position": "below", "cells": ["Cell 1", "Cell 2"]},
  {"type": "delete_row", "target_text": "text in row to delete"}
]
```

- `modify`: empty `new_text` deletes. `new_text` supports `**bold**`, `_italic_`, `_**both**_`, `# headings` and `\n\n` paragraph breaks; not CriticMarkup. `match_mode` defaults to `strict` (exactly one occurrence); there is no `last`.
- `accept` / `reject`: `Chg:` IDs renumber on every save; re-extract immediately before a reject batch.
- `insert_row` cells bypass the Markdown parser, so blanks survive there without the wrapper's help.

Store batches under `adeu/<docstem>/<purpose>_YYYYMMDD.<ext>` (e.g. `adeu/msa_template/edits_20260903.json`, `adeu/msa_template/extract_20260903.md`). One subfolder per source document; generic helpers live in `scripts/`, not in `adeu/`.

## Known limits (stable adeu behaviour)

- **Batches are sequential and atomic.** Each edit validates against the document as it reads after the preceding edits, including text those edits inserted. One failure rolls back the whole batch and nothing is written. Fix the failing edit; do not re-run the survivors separately unless you also re-read the file.
- **Targets are matched against the extraction markup.** A target inside a bold or italic run must carry the `**`/`_` markers; whitespace and curly quotes match loosely; underscore runs must be given in full (fuzzy `_+` does not swallow them in docx mode).
- **No occurrence selector.** Two verbatim-identical paragraphs cannot be told apart, and a footer string that also appears in the body is untargetable. To delete one twin: `match_mode: "all"`, then re-add the kept copy by modifying its predecessor to `predecessor\n\nkept text` (inherits list style; shows as delete + reinsert). `sidecar_to_edits.py` does this automatically.
- **Multi-paragraph targets are refused** when body text sits on both sides of the paragraph break. Split into one edit per paragraph.
- **Text-file apply** (`apply doc.docx modified.md`) wipes existing comments and tracked changes and fails post-verification on documents with `## **bold heading**` paragraphs or when the extractor footer lines are not reproduced exactly. Prefer JSON batches. On failure adeu leaves `<out>.unverified.docx` behind; delete it.
- **Structural elements cannot be deleted by text replacement**: cross-reference fields (`[~2~](#_Ref…)`), table cells, headings-as-elements. End the target just before the field and write `new_text` so the surviving field reads naturally.
- **Comments** cannot attach inside footer parts, on edits inside another pending insertion, or on paragraph-splitting edits (dropped or duplicated). Anchor the rationale on neighbouring text with a no-op `modify` instead.
- **Formatting-only changes** (font, size, underline) have no edit type; do them in Word. `\t` in `new_text` is written as a literal tab, not a `<w:tab/>`.
- **Numbering that "restarts at 1" in a markdown conversion is usually a rendering artifact.** Check `numbering.xml` before fixing.

## Critical Constraints

- Always invoke the wrapper via `uv run .claude/skills/redline/scripts/run_redline.py`. Do not use `uvx`.
- Always extract (`--page all`) before editing so target text matches the source.
- Always run `verify_apply.py` after `apply`; report its result, not adeu's.
- Keep redlined output as a new file (`<name>_eYYYYMMDD.docx`) unless the user explicitly asks to overwrite the original.
