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
./.venv/bin/python .claude/skills/caption-cli/scripts/run_caption.py --env-file <repo-root>/.env list_projects
```

## Token/Auth Failures on Search

Symptoms:
- 401/403
- invalid API key messages from Meili

Recovery:
1. Run `token` to refresh cache.
2. Retry search.
3. If persistent, verify `CAPTION_TOKEN` validity and API permissions.

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
