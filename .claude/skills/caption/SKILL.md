---
name: caption
description: Operates the Caption CLI. Use when users ask about transcripts of their conversations.
---

# Caption CLI Skill

## Overview

This skill runs the installed `caption` CLI from the repo-local `.venv` for Caption workspace and transcript workflows.

Use this skill for:
- Search (`search`)
- Workspace listing (`list_projects`, `list_folders`)
- Workspace mutations (`create_project`, `create_folder`, `edit_project`, `edit_folder`)
- Transcript caption export (`dl_transcript`)

## Required Environment

The CLI reads env vars from the repo root `.env` by default:
- `<repo-root>/.env`

Required keys:
- `CAPTION_API_URL`
- `CLERK_API_KEY`
- `CAPTION_MEILI_URL` (required for `token` and `search`)

Optional key:
- `CAPTION_MEILI_CACHE`

Install model:
- Run `uv sync` at the repo root for standard setup.
- `caption-cli` is declared in the root `caption` dependency group, and that group is enabled by default.

## Command Runner

Always run commands via:

```bash
./.venv/bin/python .claude/skills/caption/scripts/run_caption.py <args>
```

This launcher resolves paths dynamically from its own location and enforces:
- Python interpreter: `<repo-root>/.venv/bin/python` when present (fallback: current Python)
- Caption binary: `<repo-root>/.venv/bin/caption`
- Default env-file: `<repo-root>/.env` (unless overridden with `--env-file`)

For token-heavy commands (`list_projects`, `list_folders`, `dl_transcript`), the wrapper:
- Writes full stdout to `<repo-root>/caption_cache/`
- Overwrites `caption_cache/list_projects.out` for `list_projects`
- Overwrites `caption_cache/list_folders.out` for `list_folders`
- Overwrites `caption_cache/<transcript_uuid>.txt` for `dl_transcript`
- Prints only a short stdout line: `Saved <command> output to <path>`

Retrieve saved output using bash commands:

```bash
ls -lt caption_cache
head -n 40 caption_cache/<file>
```

## Workflows

### 1) Search

```bash
./.venv/bin/python .claude/skills/caption/scripts/run_caption.py search "term" --limit 5
```

### 2) List workspace data

```bash
./.venv/bin/python .claude/skills/caption/scripts/run_caption.py list_projects
./.venv/bin/python .claude/skills/caption/scripts/run_caption.py list_folders
```

### 3) Create or edit entities

```bash
./.venv/bin/python .claude/skills/caption/scripts/run_caption.py create_project "My Project" --description "First draft"
./.venv/bin/python .claude/skills/caption/scripts/run_caption.py edit_project <project-uuid> --name "Renamed"
```

### 4) Download transcripts

```bash
./.venv/bin/python .claude/skills/caption/scripts/run_caption.py dl_transcript <transcript-uuid>
```

## Critical Constraints

- Always use the wrapper script so execution stays in the repo Python environment.
- The caption api was built to allow for multiple transcripts in a project, when calling dl_transcript ALWAYS use transcript_id.
- However, transcripts and projects are currently 1 to 1.
- Users will often refer to "meetings" "recordings" "transcripts" and "projects" interchangably. If the user asks to edit or create a new transcript, they mean the project containing such transcript, since the CLI cannot edit individual lines inside transcripts.
- Reference the cache to minimize unnecessary requests.
- Validate project uuids before edit/create flows where possible.
- Keep using the root `.env` unless `--env-file` is explicitly overridden.
- `token` output is redacted by default. Only use `--show-token` when required.
- Do not invent command flags not present in `caption --help`.

## Common Failures

- Missing `.venv/bin/caption`: run `uv sync` at the repo root. The default `caption` group should install it into the shared repo `.venv`.
- Missing `CAPTION_API_URL`: all commands fail.
- Missing `CLERK_API_KEY`: authenticated API calls fail.
- Missing `CAPTION_MEILI_URL`: `token` and `search` fail.
- Invalid Meili token: search retries once after `/search/token` refresh.
- No-op edit calls: rejected by `edit_project` / `edit_folder` validation.

See detailed contracts and recovery in:
- `references/command_contracts.md`
- `references/error_playbook.md`

## Quick Reference

| Command | Purpose |
|---|---|
| `search <query>` | Search one Meilisearch index |
| `list_projects` | List all workspace projects (transcripts) |
| `list_folders` | List all workspace folders |
| `create_project` | Create project (transcript)|
| `create_folder` | Create folder |
| `edit_project` | Patch project |
| `edit_folder` | Patch folder |
| `dl_transcript <id>` | Download transcript captions |
