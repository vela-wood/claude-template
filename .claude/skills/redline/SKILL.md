---
name: redline
description: Use this skill for editing .docx files or comparing two .docx files
---

# Redline Skill

## Overview

This skill runs Adeu from the repo-local environment. Use it when you need to:
- compare two `.docx` versions
- apply redlines/comments back into a `.docx`

Do not use this skill to create a new blank Word document from scratch.

## Required Environment

- Root repo dependencies must be installed into `.venv`.
- Run `uv sync` at the repo root for standard setup.
- `adeu` is declared in the root `redline` dependency group, and that group is enabled by default.
- If `.venv/bin/adeu` is missing, run `uv sync` at the repo root.

## Command Runner

Always execute Adeu through the repo wrapper:

```bash
.claude/skills/redline/scripts/run_redline.sh --version
```

This wrapper resolves the repo root dynamically and executes the installed CLI from:
- `<repo-root>/.venv/bin/adeu`

## Workflows

### 1. Extract clean markdown

```bash
.claude/skills/redline/scripts/run_redline.sh extract contract.docx -o contract.md
```

Use this when you need a text representation for review, prompting, or search.

### 2. Diff two DOCX files

```bash
.claude/skills/redline/scripts/run_redline.sh diff v1.docx v2.docx
.claude/skills/redline/scripts/run_redline.sh diff v1.docx revised.txt --json
```

Use this to inspect how two document versions differ before applying further edits. The modified input can be another DOCX or a plain-text file.

### 3. Preview a JSON edit batch

```bash
.claude/skills/redline/scripts/run_redline.sh markup contract.docx edits.json --output preview.md
```

Use this to inspect the proposed edit set before mutating the document.

### 4. Apply redlines to a DOCX

```bash
.claude/skills/redline/scripts/run_redline.sh apply contract.docx edits.json -o contract_redlined.docx --author "Review Bot"
.claude/skills/redline/scripts/run_redline.sh apply contract.docx revised.txt -o contract_redlined.docx
```

Use this when you have either a valid Adeu edits JSON file or a revised text file and need native Track Changes output.

## Critical Constraints

- Always use the wrapper script. Do not use `uvx`.
- Adeu edits are JSON-based. Do not assume the old SuperDoc markdown edits format still applies.
- Read/extract the document before generating edits so the target text matches the source.
- Use a preview or diff flow before applying large batches of edits.
- Keep redlined output as a new file unless the user explicitly asks to overwrite the original.
