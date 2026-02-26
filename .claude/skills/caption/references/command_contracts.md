# Caption CLI Command Contracts

## Shared Invocation

Use:

```bash
./.venv/bin/python .claude/skills/caption-cli/scripts/run_caption.py <global-flags> <command> [command-flags]
```

Global flags:
- `--cache-path <path>`
- `--output json|table|md`
- `--env-file <path>` (override root `.env` only when explicitly required)

## `token`

Purpose:
- Fetch `/search/token`
- Cache token payload

Usage:

```bash
... run_caption.py token
... run_caption.py token --show-token
```

Output keys:
- `token` (`[REDACTED]` by default)
- `url`
- `expiresAt`
- `cached`

## `search`

Usage:

```bash
... run_caption.py search "term" --index transcript_captions_v1 --limit 5
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
... run_caption.py --output json list_projects
```

Output:
- `workspaceId`
- `items[]` with keys: `id`, `createdAt`, `updatedAt`, `name`, `description`, `folder`, `transcript`

## `list_folders`

Usage:

```bash
... run_caption.py --output json list_folders
```

Output:
- `workspaceId`
- `items[]` with keys: `id`, `createdAt`, `updatedAt`, `name`, `description`, `parent`

## `create_project`

Usage:

```bash
... run_caption.py create_project "My Project" --description "First draft"
... run_caption.py create_project "My Project" --workspace-id <workspace-uuid>
```

## `create_folder`

Usage:

```bash
... run_caption.py create_folder "My Folder" --description "Folder notes"
... run_caption.py create_folder "My Folder" --parent <folder-uuid>
```

## `edit_project`

Usage:

```bash
... run_caption.py edit_project <project-uuid> --name "Renamed"
... run_caption.py edit_project <project-uuid> --description "Updated" --folder <folder-uuid>
... run_caption.py edit_project <project-uuid> --clear-description --clear-folder
```

Validation:
- Requires at least one changed field
- Rejects conflicting pairs:
  - `--description` + `--clear-description`
  - `--folder` + `--clear-folder`

## `edit_folder`

Usage:

```bash
... run_caption.py edit_folder <folder-uuid> --name "Renamed"
... run_caption.py edit_folder <folder-uuid> --description "Updated" --parent <folder-uuid>
... run_caption.py edit_folder <folder-uuid> --clear-description --clear-parent
```

Validation:
- Requires at least one changed field
- Rejects conflicting pairs:
  - `--description` + `--clear-description`
  - `--parent` + `--clear-parent`

## `dl_transcript`

Usage:

```bash
... run_caption.py dl_transcript <transcript-uuid>
... run_caption.py --output json dl_transcript <transcript-uuid>
```

Output:
- Default markdown: `[HH:MM.SS] speaker: content`
- Optional JSON payload with `transcriptId`, `items`, `count`
