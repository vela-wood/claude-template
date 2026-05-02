---
name: caption
description: Operates the Caption CLI. Use when users ask about transcripts of their conversations.
---

# Caption CLI Skill

## Overview

This skill uses the global `caption` CLI installed by `uv tool install` for Caption workspace and transcript workflows. Do not use repo-local launchers.

Use this skill for:
- Search (`search`)
- Workspace listing (`list_projects`, `list_folders`)
- Workspace mutations (`create_project`, `create_folder`, `edit_project`, `edit_folder`)
- Transcript caption export (`dl_transcript`)
- claude code history session sharing (`sync`)

## Required Environment

The CLI reads credentials from its normal environment handling. Use direct `caption` commands from the workspace where `.env` and `caption_cache/` belong.

Required keys:
- `CAPTION_API_URL`
- `CLERK_API_KEY`
- `CAPTION_MEILI_URL` (required for `token` and `search`)
- `ORGANIZATION_ID` (required for `sync`)

Optional key:
- `CAPTION_MEILI_CACHE`

Install model:
- Run `uv sync` at the repo root for repo dependencies.
- Install `caption-cli` as a global uv tool:

```bash
uv tool install --force --python 3.13 "caption-cli @ git+https://github.com/sec-chair/caption-cli.git"
```

- If `caption` is not on `PATH` after installation, run `uv tool update-shell`.

For token-heavy commands (`list_projects`, `list_folders`, `dl_transcript`), send output to files explicitly:
- Use `caption --output-file caption_cache/list_projects.out list_projects`
- Use `caption --output-file caption_cache/list_folders.out list_folders`
- Use `caption --output-file caption_cache/<transcript-uuid>.txt dl_transcript <transcript-uuid>`

Retrieve saved output using bash commands:

```bash
ls -lt caption_cache
head -n 40 caption_cache/<file>
```

## Workflows

### 1) Search

```bash
caption search "term" --limit 5
```

### 2) List workspace data

```bash
caption --output-file caption_cache/list_projects.out list_projects
caption --output-file caption_cache/list_folders.out list_folders
```

### 3) Create or edit entities

```bash
caption create_project "My Project" --description "First draft"
caption edit_project <project-uuid> --name "Renamed"
```

### 4) Download transcripts

```bash
caption --output-file caption_cache/<transcript-uuid>.txt dl_transcript <transcript-uuid>
```

### 5) Sync / share claude sessions

Sync a single session by uuid:

```bash
caption sync --session-id <session-id>
```

Always default to current session's uuid unless otherwise directed. If expressly asked to sync all sessions, use `*`:

```bash
caption sync --session-id '*'
```

By default, `sync` reads the SQLite database at:

```text
~/.agentsview/sessions.db
```

Override the database path when needed:

```bash
caption sync --session-id <session-id> --db-path ~/.agentsview/sessions.db
```

Credentials can also be input by:
- `--clerk-api-key`
- `--org-id`

`sync` does not require `CAPTION_API_URL` or `CAPTION_MEILI_URL`.

## Critical Constraints

- Always use the global `caption` command directly.
- The caption api supports multiple transcripts in a project, when calling dl_transcript ALWAYS use transcript_id.
- However, transcripts and projects are currently 1 to 1.
- Users will often refer to "meetings" "recordings" "transcripts" and "projects" interchangably. If the user asks to edit or create a new transcript, they mean the project containing such transcript, since the CLI cannot edit individual lines inside transcripts.
- Reference the cache to minimize unnecessary requests.
- Validate project uuids before edit/create flows where possible.
- `token` output is redacted by default. Only use `--show-token` when required.
- Do not invent command flags not present in `caption --help`.

## Common Failures

- Missing `caption` executable: run the `uv tool install` command above. If the tool is installed but unavailable on `PATH`, run `uv tool update-shell`.
- Missing `CAPTION_API_URL`: all commands fail.
- Missing `CLERK_API_KEY`: authenticated API calls fail.
- Missing `CAPTION_MEILI_URL`: `token` and `search` fail.
- Invalid Meili token: search retries once after `/search/token` refresh.
- No-op edit calls: rejected by `edit_project` / `edit_folder` validation.
- Missing agentsview `sync` database: `sync` defaults to `~/.agentsview/sessions.db`; pass `--db-path` or set `AGENT_VIEWER_DATA_DIR`.
- Uploading with `sync` without auth: pass `--clerk-api-key` / `--org-id` or set `CLERK_API_KEY` / `ORGANIZATION_ID`.

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
| `sync --session-id <uuid>` | Upload claude code sessions |
