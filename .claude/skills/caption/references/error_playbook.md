# Caption CLI Error Playbook

## Missing Environment Variables

Symptoms:
- `Missing Caption API URL`
- `Missing API bearer token`
- `Missing Meilisearch URL`

Recovery:
1. Confirm `<repo-root>/.env` exists.
2. Confirm required keys are present and non-empty.
3. Re-run with explicit env file if needed:

```bash
./.venv/bin/python .claude/skills/caption/scripts/run_caption.py --env-file <repo-root>/.env list_projects
```

## Missing Installed CLI

Symptoms:
- `caption binary not found at <repo-root>/.venv/bin/caption`

Recovery:
1. Run `uv sync` at the repo root.
2. Re-run the wrapper command after the root `.venv` has been refreshed.

## Token/Auth Failures on Search

Symptoms:
- 401/403
- invalid API key messages from Meili

Recovery:
1. Run `token` to refresh cache.
2. Retry search.
3. If persistent, verify `CLERK_API_KEY` validity and API permissions.

## Workspace Lookup Failures

Symptoms:
- failures from `/users/me/workspace`

Recovery:
1. Verify API URL/token.
2. Confirm token has workspace scope.
3. Retry with stable network.

## No-op / Conflicting Edit Arguments

Symptoms:
- `requires at least one field`
- `Use either --description or --clear-description`
- `Use either --folder or --clear-folder`
- `Use either --parent or --clear-parent`

Recovery:
1. Build one consistent patch payload.
2. Use either set or clear for nullable fields, never both.

## Transcript Formatting Failures

Symptoms:
- errors about missing `channel`, `index`, `createdAt`, or `content`

Recovery:
1. Use `--output json` for raw payload inspection.
2. Validate transcript item schema before markdown rendering.

## Sensitive Output Handling

- `token` is redacted by default.
- Only use `--show-token` when raw token output is explicitly required.
- Never paste raw tokens into shared logs, tickets, or chat.

## Token-Heavy Command Output Location

Symptoms:
- `list_projects`, `list_folders`, or `dl_transcript` only prints `Saved <command> output to <path>`

Recovery:
1. List recent files: `ls -lt caption_cache`
2. `list_projects` and `list_folders` always overwrite `caption_cache/list_projects.out` and `caption_cache/list_folders.out`.
3. `dl_transcript` always overwrites `caption_cache/<transcript_uuid>.txt`.
4. Preview output: `tail -n 40 caption_cache/<file>`
5. Read full output when needed: `cat caption_cache/<file>`
