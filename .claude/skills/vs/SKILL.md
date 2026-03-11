---
name: vs
description: Use the Version Story command-line tool in this repository for local workspace flows and API endpoint wrappers.
---

# Version Story CLI Skill (`vs`)

Use this skill when you need to operate the CLI in this repo (`version-story-command-line`).

## Scope

This skill is for the current CLI, not the legacy `version-story-cli` package.

- Local workspace commands (`vs init`, `vs pull`, `vs status`, `vs read`, `vs commit`, `vs edit`, `vs open`) are human-readable.
- API wrapper commands (`vs repositories ...`, `vs documents ...`, `vs versions ...`, `vs reviews ...`, `vs notifications ...`, `vs request ...`) return JSON.

## Agent Rules

1. Confirm you are in the target working directory before running commands.
2. Use `vs status` before `vs commit` so you understand what will be uploaded.
3. Do not run `vs commit` after `vs edit`; `vs edit` already commits automatically.
4. Always show the final `View version: <url>` line from `vs edit` output to the user.
5. Prefer explicit selectors for remote objects:
   - `--repository-id` / `--repository-slug` / `--repository-name`
   - `--branch-id` / `--branch-slug` / `--branch-name`
6. If `.versionstory/state.json` is missing, run `vs init` first.

## Environment Conventions

- Local API mode: use `--local` on local workspace commands (`init`, `pull`, `commit`, `edit`) to target `http://localhost:3001`.
- Local web mode: use `vs open --local` to open `http://localhost:5173`.
- Default (without `--local`) uses the configured/deployed API and mapped web host behavior.
  - if no API base URL is configured anywhere, CLI defaults to `https://api.versionstory.com`
  - `https://api.development.versionstory.com` -> `https://web.development.versionstory.com`
  - `https://api.versionstory.com` -> `https://web.versionstory.com`

## Binary Setup

- `vs edit` uses `edit-docx` from `bin/<platform>/edit-docx` when present.
- If native binaries are missing, the CLI runs `apps/edit-docx/edit-docx.csproj` via `dotnet run`.
- To prebuild native binaries in this repo:

```bash
npm run build:native
```

## Core Workflows

### 1) Authenticate

```bash
vs auth login --email <email> --password '<password>'
vs auth me
```

### 2) Initialize Local State

Initialize from an existing repository:

```bash
vs init --repository-id <repository-id> --branch-id <branch-id>
```

Create a new repository from local working files (`.doc`, `.docx`, `.pdf`), upload to `main`, and initialize:

```bash
vs init --name "My Repository"
```

### 3) Sync + Inspect

```bash
vs pull
vs status
vs read path/to/file.docx
```

`vs read` behavior:
- always reads from cached preprocessed working state under `.versionstory/preprocessed/...`
- if working file changed locally, refreshes the preprocessed cache first
- `--redline` is not supported in this mode

Show only changed lines with context:

```bash
vs read path/to/file.docx --changes --context 1
```

Search for specific text in large documents:

```bash
vs read path/to/file.docx --search "Acme, Inc."
vs read path/to/file.docx --search "Acme, Inc." --return-ids
```

### 4) Commit Working-Tree Changes

Commit all modified tracked files:

```bash
vs commit
```

Commit specific tracked files:

```bash
vs commit path/to/file.docx
```

### 5) Apply Structured Edits

```bash
vs edit path/to/file.docx \
  --edits '[{"type":"replace","target_id":"<paragraph-unid>","operation":{"target":{"text":"old"},"inserted_text":"new"}}]' \
  --chat-context "User request that caused these edits" \
  --message "Commit message"
```

Validation only:

```bash
vs edit path/to/file.docx --edits '[...]' --chat-context "..." --message "..." --validate
```

`vs edit` behavior:
- if working file has local modifications, refreshes preprocessed cache and exits so you can re-run `vs read` and get current UUIDs
- validates target paragraph IDs against the tracked `.docx`
- applies tracked changes through local `edit-docx`
- accepts revisions into a clean working file
- commits the edited file automatically
- prints a Version Story workspace URL that deep-links to `repository`, `branch`, `document`, and `version` as `View version: <url>`

### 6) Open Workspace in Browser

```bash
vs open
vs open --local
```

### 7) Branch Workflows

List branches:

```bash
vs repositories branches --repository-id <repository-id>
```

Create a branch:

```bash
vs repositories create-branch --repository-id <repository-id> --name "NDA updates"
```

Switch local workspace state to a different branch (recommended):

```bash
vs pull --repository-id <repository-id> --branch-id <branch-id>
```

Alternative branch switch via re-init:

```bash
vs init --repository-id <repository-id> --branch-id <branch-id> --force
```

Merge/close/delete branch:

```bash
vs repositories merge-branch --repository-id <repository-id> --branch-id <branch-id>
vs repositories close-branch --repository-id <repository-id> --branch-id <branch-id>
vs repositories delete-branch --repository-id <repository-id> --branch-id <branch-id>
```

## API Wrapper Usage

Use endpoint wrappers when you need structured JSON output:

```bash
vs repositories list
vs repositories branches --repository-id <repository-id>
vs repositories create-branch --repository-id <repository-id> --name "Feature branch"
vs repositories tree --repository-id <repository-id> --branch-id <branch-id>
vs versions upload --document-id <document-id> --branch-id <branch-id> --file ./NDA.docx
vs request GET /api/v1/repositories
```

## Common Failure Modes

- `Missing .versionstory state. Run \`vs init\` first.`:
  - initialize the directory before status/commit/edit/open.
- `File is not tracked by Version Story.`:
  - run `vs pull` or initialize correctly.
- `Invalid target_id(s) not found in document`:
  - use a valid `pt14:Unid` from the current tracked document.
