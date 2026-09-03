---
name: vs
description: Run Version Story's hosted API to compare two documents (.docx/.doc/.pdf) into a Word redline, merge several reviewers' revisions into one tracked-changes draft, or apply/accept/reject tracked edits. Use when asked for Version Story, a hosted/API redline or merge, or to benchmark against /compare.
---

# Version Story Skill

## Overview

Drives the Version Story REST API (`api.versionstory.com`) through one wrapper.
Every call uploads the documents to Version Story and consumes the org's monthly quota.

!`uv run .claude/skills/vs/scripts/vs.py --help`

The cli help should be visible above, fix all errors until the cli works.

## When to use this vs `/compare` and `/redline`

- `/compare` (Docxodus) and `/redline` (adeu) run locally. This skill is kept separate so the
  two can be benchmarked side by side. Do not fold one into the other.
- Use this skill when the user names Version Story, needs `.doc` or `.pdf` inputs, or needs a
  multi-reviewer merge (neither local tool does merges).
- Default to the local tools when the user does not specify; they cost nothing.

## Key setup (only on error)

Do not run `check` before a job. Go straight to `compare`/`merge`/`edit`; the wrapper
already fails fast when the key is missing (exit 2, `[MISSING]`) or rejected
(`INVALID_API_KEY`, `API_KEY_ORG_MISMATCH`). `check` is a diagnostic for those failures and
for confirming a freshly saved key.

1. On a key failure, tell the user in plain words, one step at a time:
   1. Sign in at versionstory.com and open **Settings → Developer**. If that menu is
      absent, they need the Developer permission from their org admin.
   2. Create a REST API key. It appears **once** in a banner with a copy button. Copy it
      before dismissing. If lost, create a new one.
   3. Save it one of two ways:
      - Paste it into this chat, and the agent runs
        `uv run .claude/skills/vs/scripts/vs.py set-key <key>`. Mention that the key
        transits the chat.
      - Or open `.env` at the repo root themselves (mac: `open -e .env`; Windows:
        `notepad .env`; create the file if absent) and add the line
        `VERSION_STORY_API_KEY=vs_live_...` on its own line, then save.
   4. Run `uv run .claude/skills/vs/scripts/vs.py check` once to confirm the saved key,
      read the result back to them, then re-run the original job.

Rules:
- Never print, echo, or repeat the key. The scripts redact it as `vs_live_9f3c…`.
- Never write the key anywhere but `.env` (gitignored).
- `INVALID_API_KEY` right after a paste usually means a truncated copy.
- `API_KEY_ORG_MISMATCH` means the key belongs to a different organization.

## Workflows

### 1. Compare (redline)

```bash
uv run .claude/skills/vs/scripts/vs.py compare original.docx revised.docx
uv run .claude/skills/vs/scripts/vs.py compare original.docx revised.docx --format docx,md,pdf_changed_pages_only
```

Author defaults to `Velawood`, matching `/compare`, so benchmark outputs differ only by engine.
For a benchmark, run the same pair through `.claude/skills/compare/scripts/run_compare.py`.

### 2. Merge several reviewers' revisions

```bash
uv run .claude/skills/vs/scripts/vs.py merge original.docx reviewer_a.docx reviewer_b.docx
```

At least two revisions are required. Merges take longer than compares.

### 3. Edit (initiate → read markdown → build edits JSON → edit → deliver)

```bash
uv run .claude/skills/vs/scripts/vs.py edit-initiate contract.docx
# → prints file_id and writes contract_vsedit_YYYYMMDD.md (first line holds the file_id)
uv run .claude/skills/vs/scripts/vs.py edit contract.docx --edits edits.json --file-id <file_id> --format docx,redline
```

The markdown carries paragraph `unid`s and tracked-change `revision_id`s. Build `edits.json`
from them:

```json
[
  {"type": "replace", "unid": "p-123", "text": "New paragraph text"},
  {"type": "delete", "unid": "p-124"},
  {"type": "insert", "unid": "p-125", "position": "after", "text": "Inserted paragraph"},
  {"type": "accept_revision", "revision_id": "r-7"},
  {"type": "reject_revision", "revision_id": "r-8"}
]
```

`--file-id` skips re-uploading; the DOCUMENT argument then only names the output.
Store `edits.json` next to the document with a useful filename, not in a tmp folder.

`.doc` and `.pdf` inputs are converted server-side and take longer.

## Output naming

| Command | Default folder | Default stem | Per-format suffix |
|---|---|---|---|
| `compare` | MODIFIED's folder | `<modified>_vs_<original>_vscompare_YYYYMMDD` | `.docx`, `.pdf`, `_changed.pdf`, `.md`, `.json` |
| `merge` | ORIGINAL's folder | `<original>_vsmerge_YYYYMMDD` | `.docx`, `.md`, `.json` |
| `edit-initiate` | DOCUMENT's folder | `<document>_vsedit_YYYYMMDD` | `.md` |
| `edit` | DOCUMENT's folder | `<document>_vsedit_YYYYMMDD` | `.docx`, `_redline.docx` |

Collisions get `_2`, `_3`, … Inputs are never overwritten. Each written file is reported as `Wrote: <path>`.

## Failure handling

| Signal | Meaning | Action |
|---|---|---|
| exit 2, `[MISSING]` | No key | Run the key setup walkthrough above |
| `USAGE_LIMIT_REACHED` (402) | Monthly quota exhausted | Tell the user; do not retry |
| `INVALID_API_KEY` (401) | Bad or truncated key | Run the key setup walkthrough above |
| `FILE_TOO_LARGE` (413) | Input over 100 MB | Ask for a smaller file |
| Job failed `DOCUMENT_PROTECTED` | Password/edit-protected file | Ask the user for an unprotected copy |
| Job failed `DOCUMENT_UNREADABLE` / `DOCUMENT_CONVERSION_FAILED` | Corrupt or unsupported content | Try re-saving as `.docx` |
| `Timeout` | Job still running server-side | Re-run with a larger `--timeout` |
| `INTERNAL_ERROR` (500) | Transient | Already retried 3x by the client; report request_id |

Always relay the `request_id` / job id printed on failure.

## Critical Constraints

- Always invoke the wrapper via `uv run .claude/skills/vs/scripts/vs.py`. Do not call the API directly.
- Never overwrite inputs; outputs are always new files.
- Report every output path to the user.
- Confirm with the user before runs that consume quota on large batches.
