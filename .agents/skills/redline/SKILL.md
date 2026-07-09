---
name: redline
description: Use this skill for editing .docx files with tracked edits, or for machine-readable diffs of two .docx files. To generate track-changes comparison .docx, use /compare instead.
---

# Redline Skill

## Overview

This skill runs Adeu from the repo-local environment. Use it when you need to:
- apply edits to a `.docx` file (written out as Word track changes)
- inspect the differences between two `.docx` versions as text/JSON

Note: `adeu diff` outputs text/JSON only — it never produces a .docx. If the user wants a comparison document a human can open in Word with track changes, use the /compare skill instead.

Never use this skill to create a new blank Word document from scratch.

!`uv run .claude/skills/redline/scripts/run_redline.py --help`

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
repo root dynamically, and executes the installed CLI from:
- `<repo-root>/.venv/bin/adeu`

## Workflows

### 1. Extract clean markdown

```bash
uv run .claude/skills/redline/scripts/run_redline.py extract contract.docx -o contract.md
```

Use this when you need a text representation for review, prompting, or search.

### 2. Diff two DOCX files

```bash
uv run .claude/skills/redline/scripts/run_redline.py diff v1.docx v2.docx
uv run .claude/skills/redline/scripts/run_redline.py diff v1.docx revised.txt --json
```

Use this to inspect how two document versions differ before applying further edits. The modified input can be another DOCX or a plain-text file.

### 3. Apply redlines to a DOCX

```bash
uv run .claude/skills/redline/scripts/run_redline.py apply contract.docx edits.json -o contract_redlined.docx --author "Review Bot"
```

edits.json follows the following schema:

```json
[
  {
    "type": "modify",
    "target_text": "exact text to find",
    "new_text": "replacement text",
    "comment": "optional comment"
  },
  {
    "type": "accept",
    "target_id": "Chg:12",
    "comment": "optional rationale"
  },
  {
    "type": "reject",
    "target_id": "Chg:13",
    "comment": "optional rationale"
  },
  {
    "type": "reply",
    "target_id": "Com:5",
    "text": "reply text"
  },
  {
    "type": "insert_row",
    "target_text": "text in anchor row",
    "position": "below",
    "cells": ["Cell 1", "Cell 2"]
  },
  {
    "type": "delete_row",
    "target_text": "text in row to delete"
  }
]
```

Key rules:

1.  Each item must include `type`:
2. `modify`: requires `target_text` and `new_text`; `comment` is optional. Empty `new_text` deletes. `new_text` supports Markdown headings, bold, italic, and paragraph breaks, but not manual CriticMarkup tags. 
3. `accept` / `reject`: require `target_id`, usually like `Chg:12`.
4. `reply`: requires `target_id` like `Com:5` and `text`. 
5. `insert_row`: requires `target_text` and `cells`; `position` is optional and defaults to `below`.
6. `delete_row`: requires `target_text`. 

Store `edits.json` in the adeu/ folder with a useful filename for debugging, avoid using tmp files.

## Critical Constraints

- Always invoke the wrapper via `uv run .claude/skills/redline/scripts/run_redline.py`. Do not use `uvx`.
- Always extract the document before editing so the target text matches the source.
- Use a preview or diff flow before applying large batches of edits.
- Keep redlined output as a new file unless the user explicitly asks to overwrite the original.
