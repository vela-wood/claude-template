---
name: caption
description: Operates the Caption CLI. Use when users ask about transcripts of their conversations.
---

# Caption CLI Skill

## Overview

This skill runs the local `caption-cli` commands for Caption workspace and transcript workflows.

Use this skill for:
- Search (`search`)
- Workspace listing (`list_projects`, `list_folders`)
- Workspace mutations (`create_project`, `create_folder`, `edit_project`, `edit_folder`)
- Transcript caption export (`dl_transcript`)

Note that "project" is a transcript of a conversation in caption.

## Required Environment

The CLI reads env vars from the repo root `.env` by default:
- `<repo-root>/.env`

Required keys:
- `CAPTION_API_URL`
- `CAPTION_TOKEN`
- `CAPTION_MEILI_URL` (required for `token` and `search`)

Optional key:
- `CAPTION_MEILI_CACHE`

## Command Runner

Always run commands via:

```bash
./.venv/bin/python .claude/skills/caption-cli/scripts/run_caption.py <args>
```

This launcher resolves paths dynamically from its own location and enforces:
- Python interpreter: `<repo-root>/.venv/bin/python` when present (fallback: current Python)
- Caption entrypoint: `<repo-root>/caption-cli/caption.py`
- Default env-file: `<repo-root>/.env` (unless overridden with `--env-file`)

## Workflows

### 1) Search

```bash
./.venv/bin/python .claude/skills/caption-cli/scripts/run_caption.py search "term" --limit 5
```

### 2) List workspace data

```bash
./.venv/bin/python .claude/skills/caption-cli/scripts/run_caption.py list_projects
./.venv/bin/python .claude/skills/caption-cli/scripts/run_caption.py list_folders
```

### 3) Create or edit entities

```bash
./.venv/bin/python .claude/skills/caption-cli/scripts/run_caption.py create_project "My Project" --description "First draft"
./.venv/bin/python .claude/skills/caption-cli/scripts/run_caption.py edit_project <project-uuid> --name "Renamed"
```

### 4) Download transcripts

```bash
./.venv/bin/python .claude/skills/caption-cli/scripts/run_caption.py dl_transcript <project-uuid>
```

## Critical Constraints

- Always use the wrapper script so execution stays in the repo Python environment.
- Keep using the root `.env` unless `--env-file` is explicitly overridden.
- `token` output is redacted by default. Only use `--show-token` when required.
- Validate IDs before edit/create flows by listing/searching first when possible.
- Do not invent command flags not present in `caption --help`.

## Common Failures

- Missing `CAPTION_API_URL`: all commands fail.
- Missing `CAPTION_TOKEN`: authenticated API calls fail.
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