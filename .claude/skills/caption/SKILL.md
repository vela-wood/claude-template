---
name: caption
description: Operates the Caption CLI. Use when users ask about transcripts of their conversations.
---

# Caption CLI Skill

## Overview

This skill operates the global `caption` CLI (installed via `uv tool install`) for Caption workspace, transcript, search, hosted Markdown docs, and session-sync workflows. Do not use repo-local launchers or wrappers.

The CLI is self-describing. These docs cover routing, delegation, and safety defaults; for exact syntax, the live CLI is the source of truth:

- `caption capabilities` - machine-readable contract (commands, flags, exit codes, env vars). Works offline, no credentials.
- `caption robot-docs guide` - paste-ready agent handbook generated from the live command table.
- `caption --output json doctor --strict` - health check; exits non-zero if a feature probe fails.

`caption --version` is NOT supported. Read the version from `capabilities.version` instead.

## Delegation policy (IMPORTANT)

The main thread should not burn context on raw CLI output. Delegate CLI operation to cheap subagents and keep only summaries in the main conversation:

- Spawn subagents via the Agent tool with `model: "haiku"` for mechanical work: listing/reviewing project or matter names, resolving a name to a UUID, running `token`/`doctor`, downloading transcripts to cache.
- Use `model: "sonnet"` when the subagent must analyze content: running searches and triaging hits, reading a downloaded transcript and summarizing it, picking speaker channel/index targets.
- Keep mutations (`create_*`, `edit_*`, `assign_speakers`, `rename_speaker`, `sync` without `--test`, `create_md`) in the main thread so the user-approved intent maps directly to the command run. Subagents gather the inputs (UUIDs, dry-run previews); the main thread executes.

Subagent prompts must instruct the agent to:
1. Run every `caption` command with `--output-file caption_cache/<descriptive-name>.<ext>` (see cache discipline below).
2. Return a SHORT summary only - e.g., matched project names + UUIDs, top search hits, a transcript synopsis - never the full payload.
3. Cite the cache file path(s) for every claim, so the main thread can `head`/`grep` the full file if the summary needs double-checking.

Example subagent prompt skeleton:

```text
Run: caption --output json --output-file caption_cache/list_projects.json list_projects
Then find projects whose name matches "<term>". Return only: matching names, their project UUIDs and transcript UUIDs, and the cache file path. Do not paste raw JSON.
```

## Cache discipline

Every `caption` request must be persisted with the global `--output-file` flag into `caption_cache/` (create the directory if missing). Global flags go BEFORE the subcommand:

```bash
caption --output json --output-file caption_cache/list_projects.json list_projects
caption --output-file caption_cache/transcript_<uuid>.md dl_transcript <transcript-uuid>
caption --output json --output-file caption_cache/search_<slug>.json search "term" --limit 10
```

- When reporting results (main thread or subagent), cite the cache file, e.g., "see `caption_cache/transcript_1234.md` lines 40-60".
- Before re-running a request, check the cache: `ls -lt caption_cache` then `head -n 40 caption_cache/<file>`.
- Prefer `--output json` for anything a program or subagent will parse; `md` for transcripts and `get_md`.

## Required environment

Not all commands need all keys - check `needs_api` in `caption capabilities`:

- `CAPTION_API_URL` - Caption workspace API commands (`list_projects`, `dl_transcript`, speaker commands, etc.)
- `CLERK_API_KEY` - all authenticated calls (Caption API and hosted history)
- `CAPTION_MEILI_URL` - `token` and `search` only
- `ORGANIZATION_ID` - hosted history commands (`sync`, `list_matters`, `create_md`, `list_md`, `get_md`)
- `AGENT_VIEWER_DATA_DIR` - optional; overrides the agentsview data dir for `sync` (default `~/.agentsview`)

`capabilities`, `robot-docs`, and `--help` need no credentials. A `.env` in `$PWD` loads automatically; override with `--env-file`. Hosted history commands also accept `--clerk-api-key` / `--org-id` flags.

Install model:

```bash
uv tool install --force --python 3.13 "caption-cli @ git+https://github.com/sec-chair/caption-cli.git"
```

If `caption` is missing from `PATH` afterward, run `uv tool update-shell`.

## Startup for a caption task

1. `caption capabilities` (or delegate to a haiku subagent that returns just the command names relevant to the task).
2. If anything fails or the environment is uncertain: `caption --output json doctor --strict`.
3. Route the work per the delegation policy above.

## Workflows

### 1) Discovery / health

```bash
caption capabilities
caption robot-docs guide
caption --output json --output-file caption_cache/doctor.json doctor --strict
```

### 2) Search (haiku/sonnet subagent)

```bash
caption --output json --output-file caption_cache/search_<slug>.json search "term" --limit 10
```

Default index is `transcript_captions_v1`; other indexes via `--index` (e.g., `projects_v1`). `token` is fetched/refreshed automatically; run `caption token` only to debug auth (output redacted; `--show-token` only when explicitly required).

### 3) Workspace projects and folders (haiku subagent for listing)

```bash
caption --output json --output-file caption_cache/list_projects.json list_projects
caption --output json --output-file caption_cache/list_folders.json list_folders
```

Default output is condensed; add `--full` for raw payloads. Mutations stay in the main thread; use `--dry-run` first when intent is unclear:

```bash
caption create_project "My Project" --description "First draft"
caption edit_project <project-uuid> --name "Renamed" --dry-run
```

### 4) Transcript export (haiku subagent downloads; sonnet summarizes)

```bash
caption --output-file caption_cache/transcript_<uuid>.md dl_transcript <transcript-uuid>
```

Timestamps are stripped by default for token efficiency; pass `--timestamp` to keep them. Always call `dl_transcript` with the transcript UUID (from `list_projects` output, field `transcript`), not the project UUID.

### 5) Speaker assignment (main thread mutation; subagent scouts)

```bash
caption --output-file caption_cache/speakers_<uuid>.txt list_speakers <transcript-uuid>
caption assign_speakers --transcript-id <uuid> --channel microphone --index 1 --name Alice --dry-run
caption rename_speaker <project-uuid> <speaker-uuid> --name "Alice"
```

- Prefer `--name` over `--speaker-id` (the API does not ownership-check speaker IDs across projects).
- Omitting `--index` updates all diarization indexes in the channel.
- `--project-id` fans out over every transcript in the project - confirm with the user first.
- Only custom speakers can be renamed; user-backed speakers are rejected.

### 6) Live transcription with `tail` (main thread, ALWAYS in the background)

`caption tail` streams finalized captions for one transcript in real time. It first prints the entire transcript so far, then - if transcription is still in progress - keeps appending new captions as they arrive. This lets the user interact with a live meeting transcription from inside a Claude session.

```bash
caption tail >> caption_cache/tail_live.txt
caption tail <transcript-uuid> --idle-timeout 300 >> caption_cache/tail_<slug>.txt
```

Rules:

- ALWAYS run `tail` as a background task (e.g., Bash `run_in_background`) with stdout appended to a `caption_cache/` file. Never run it in the foreground - it blocks until interrupted or a bound is hit.
- Omit `transcript_id` to tail the transcript attached to the most recently updated project (the usual case: "tail my current meeting"). Pass a UUID to pin a specific transcript.
- To answer questions about the meeting, read the cache file (`tail -n 50 caption_cache/tail_live.txt`, `grep`, etc.) rather than waiting on the stream. Re-read it whenever the user asks about the latest discussion.
- Output lines are fixed format `{channel}-{index}: {content}` (e.g., `microphone-1: We should ship on Friday.`); timestamps and IDs are stripped. Diagnostics and deleted-caption notices go to stderr.
- Bound the stream when scripting: `--duration N`, `--max-events N`, or `--idle-timeout N` (recommended: `--idle-timeout 300` so the process exits ~5 minutes after the meeting goes quiet). An unbounded tail runs until killed.
- During reconnect/backfill, output is deduped by caption ID but not guaranteed to be in `createdAt` order.
- `tail` keeps following the same transcript even if a new session starts; restart it to switch to a newer transcript.
- Needs `CAPTION_API_URL` and `CLERK_API_KEY` (or `--clerk-api-key`).



## Critical constraints

- Always use the global `caption` command directly; global flags (`--output`, `--output-file`, `--env-file`, `--cache-path`) go before the subcommand.
- `dl_transcript` ALWAYS takes a transcript UUID. Projects and transcripts are currently 1:1, but they have distinct UUIDs.
- `tail` is a long-running stream: ALWAYS run it in the background with stdout redirected to `caption_cache/`, never in the foreground.
- Users refer to "meetings", "recordings", "transcripts", and "projects" interchangeably. "Edit/create a transcript" means the project containing it - the CLI cannot edit lines inside transcripts.
- `list_projects` != `list_matters` (workspace API vs hosted history server).
- Reference `caption_cache/` before re-requesting anything.
- Validate UUIDs (via cached listings) before edit/create flows; use `--dry-run` on any mutation whose intent is unclear.
- Do not invent flags. Confirm syntax in `caption capabilities` or `caption <command> --help`.
- Never paste raw tokens (`token --show-token`) into logs, summaries, or chat.

## Common failures

- `command not found: caption` - run the `uv tool install` command above; then `uv tool update-shell`.
- Exit 3 (configuration) - a required env var is missing for THAT command; check the env-var dictionary in `capabilities` rather than assuming all keys are needed.
- Exit 2 (usage) - flag ordering or unknown flag; re-check global-flag-before-subcommand ordering.
- Condensed output missing fields - re-run with `--full`.
- Meili auth failure - `search` refreshes the token once automatically; persistent failure means `CLERK_API_KEY` is invalid.

See `references/command_contracts.md` for the command map and `references/error_playbook.md` for exit-code recovery.

## Quick reference

| Command | Purpose | Delegate to |
|---|---|---|
| `capabilities` / `robot-docs guide` | Live CLI contract / agent handbook | haiku |
| `doctor --strict` | Feature/health probes | haiku |
| `token` | Fetch/cache Meili search token (debug only) | haiku |
| `search <query>` | Search a Meilisearch index | sonnet |
| `list_projects` / `list_folders` | List workspace projects (transcripts) / folders | haiku |
| `create_project` / `create_folder` | Create project / folder | main thread |
| `edit_project` / `edit_folder` | Patch project / folder | main thread |
| `dl_transcript <transcript-uuid>` | Download transcript captions | haiku (dl), sonnet (summarize) |
| `tail [transcript-uuid]` | Stream live captions (backfills full transcript, then follows) | main thread, background task only |
| `list_speakers <transcript-uuid>` | Map channel/index/speaker groups | haiku |
| `assign_speakers` / `rename_speaker` | Speaker mutations | main thread |
| `list_matters` | List hosted-history matters | haiku |
| `create_md` / `list_md` / `get_md` | Hosted Markdown docs | main thread / haiku / haiku |
| `sync --session-id <uuid>` | Upload claude code sessions | main thread |
