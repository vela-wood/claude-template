---
name: compare
description: Compare two .docx files and save a Word-native track-changes .docx for human use (e.g., /compare original.docx revised.docx)
---

# Compare Skill

## Overview

This skill runs Python-Redlines (Docxodus engine) from the repo-local environment. It takes an ORIGINAL and a MODIFIED `.docx` and produces a **third `.docx`** showing MODIFIED's changes relative to ORIGINAL as real Word track changes — ready to send to a human, who can open it in Word and accept/reject changes.

!`.claude/skills/compare/scripts/run_compare.sh --help`

The cli help should be visible above, fix all errors until the cli works.

## When to use /compare vs /redline

- **/compare** (this skill): you have **two existing documents** and need a **track-changes .docx a human can open in Word**. Output is a new document for human consumption.
- **/redline** (adeu): the agent is **making/applying edits** to a single document, or needs a **machine-readable diff** — `adeu diff` outputs text/JSON only, never a .docx.

## Required Environment

- Root repo dependencies must be installed into `.venv`.
- Run `uv sync` at the repo root for standard setup.
- `python-redlines[docxodus]` is declared in the root `compare` dependency group, and that group is enabled by default.
- If `python_redlines` is not importable, run `uv sync` at the repo root.

## Command Runner

Always execute through the repo wrapper:

```bash
.claude/skills/compare/scripts/run_compare.sh original.docx modified.docx
.claude/skills/compare/scripts/run_compare.sh v1.docx v2.docx -o comparison.docx --author "A. Lin"
```

Argument order matters: **original first, modified second**. The output shows the modified document's changes as tracked insertions/deletions against the original.

## Output naming

By default the output is written next to the modified file as:

```
<modified-stem>_vs_<original-stem>_compare_YYYYMMDD.docx
```

Use `-o` to override. The script refuses to overwrite either input file.

## Critical Constraints

- Always use the wrapper script. Do not use `uvx` or call `python_redlines` ad hoc.
- Both inputs must be `.docx`. If a source is a PDF or other format, flag this to the user — do not attempt lossy conversions to force a compare.
- Always write a new file; never overwrite the inputs (per repo file-versioning rules).
- After generating, report the output path to the user. Optionally verify with `adeu extract` on the output to sanity-check the tracked changes.
