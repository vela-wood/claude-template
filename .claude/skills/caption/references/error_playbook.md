# Caption CLI Error Playbook

## Missing Environment Variables

Symptoms:
- `Missing Caption API URL`
- `Missing API bearer token`
- `Missing Meilisearch URL`

Recovery:
1. Confirm the needed environment variables are available to the `caption` process.
2. Confirm required keys are present and non-empty.
3. Re-run with explicit env file if needed:

```bash
caption --env-file "$PWD/.env" list_projects
```

## Missing Installed CLI

Symptoms:
- `caption executable not found on PATH`
- `command not found: caption`

Recovery:
1. Install the global tool:

```bash
uv tool install --force --python 3.13 "caption-cli @ git+https://github.com/sec-chair/caption-cli.git"
```

2. If `caption` is still unavailable, run `uv tool update-shell` or add uv's tool executable directory to `PATH`.
3. Do not substitute a repo-local launcher.

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
- `list_projects`, `list_folders`, or `dl_transcript` produces too much terminal output

Recovery:
1. Use direct output-file commands:
   - `caption --output-file caption_cache/list_projects.out list_projects`
   - `caption --output-file caption_cache/list_folders.out list_folders`
   - `caption --output-file caption_cache/<transcript-uuid>.txt dl_transcript <transcript-uuid>`
2. List recent files: `ls -lt caption_cache`
3. Preview output: `tail -n 40 caption_cache/<file>`
4. Read full output when needed: `cat caption_cache/<file>`
