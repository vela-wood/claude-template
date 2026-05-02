# Caption CLI Command Contracts

## Shared Invocation

Use:

```bash
caption <global-flags> <command> [command-flags]
```

Standard install path:
- Run `uv sync` at the repo root for repo dependencies.
- Install `caption-cli` globally:

```bash
uv tool install --force --python 3.13 "caption-cli @ git+https://github.com/sec-chair/caption-cli.git"
```

- If `caption` is not found after installation, run `uv tool update-shell`.

Global flags:
- `--cache-path <path>`
- `--output json|table|md`
- `--output-file <path>`
- `--env-file <path>`

Token-heavy output behavior:
- `caption --output-file caption_cache/list_projects.out list_projects`
- `caption --output-file caption_cache/list_folders.out list_folders`
- `caption --output-file caption_cache/<transcript-uuid>.txt dl_transcript <transcript-uuid>`
- Inspect saved output with:
  - `ls -lt caption_cache`
  - `tail -n 40 caption_cache/<file>`
  - `cat caption_cache/<file>`

## `token`

Purpose:
- Fetch `/search/token`
- Cache token payload

Usage:

```bash
caption token
caption token --show-token
```

Output keys:
- `token` (`[REDACTED]` by default)
- `url`
- `expiresAt`
- `cached`

## `search`

Usage:

```bash
caption search "term" --index transcript_captions_v1 --limit 5
```

Defaults:
- `index=transcript_captions_v1`
- `limit=5`

Behavior:
- Uses cached token if present
- On Meili auth error, refreshes token once and retries once

## `list_projects`

Usage:

```bash
caption --output-file caption_cache/list_projects.out list_projects
caption --output json list_projects
```

Output:
- `workspaceId`
- `items[]` with keys: `id`, `createdAt`, `updatedAt`, `name`, `description`, `folder`, `transcript`

## `list_folders`

Usage:

```bash
caption --output-file caption_cache/list_folders.out list_folders
caption --output json list_folders
```

Output:
- `workspaceId`
- `items[]` with keys: `id`, `createdAt`, `updatedAt`, `name`, `description`, `parent`

## `create_project`

Usage:

```bash
caption create_project "My Project" --description "First draft"
caption create_project "My Project" --workspace-id <workspace-uuid>
```

## `create_folder`

Usage:

```bash
caption create_folder "My Folder" --description "Folder notes"
caption create_folder "My Folder" --parent <folder-uuid>
```

## `edit_project`

Usage:

```bash
caption edit_project <project-uuid> --name "Renamed"
caption edit_project <project-uuid> --description "Updated" --folder <folder-uuid>
caption edit_project <project-uuid> --clear-description --clear-folder
```

Validation:
- Requires at least one changed field
- Rejects conflicting pairs:
  - `--description` + `--clear-description`
  - `--folder` + `--clear-folder`

## `edit_folder`

Usage:

```bash
caption edit_folder <folder-uuid> --name "Renamed"
caption edit_folder <folder-uuid> --description "Updated" --parent <folder-uuid>
caption edit_folder <folder-uuid> --clear-description --clear-parent
```

Validation:
- Requires at least one changed field
- Rejects conflicting pairs:
  - `--description` + `--clear-description`
  - `--parent` + `--clear-parent`

## `dl_transcript`

Usage:

```bash
caption --output-file caption_cache/<transcript-uuid>.txt dl_transcript <transcript-uuid>
caption --output json dl_transcript <transcript-uuid>
```

Output:
- Default markdown: `[HH:MM.SS] speaker: content`
- Optional JSON payload with `transcriptId`, `items`, `count`

## `sync`

Usage:

```bash
caption sync --session-id <session-id>
caption sync --session-id '*'
caption sync --session-id <session-id> --db-path ~/.agentsview/sessions.db
```

Required auth:
- `CLERK_API_KEY`
- `ORGANIZATION_ID`
