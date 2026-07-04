# Caption CLI Error Playbook

Triage by exit code first, then by symptom. Diagnostics go to stderr; data goes to stdout.

## Exit code triage

| Exit | Meaning | First move |
|---|---|---|
| 0 | success (empty results included) | none - empty is not an error |
| 1 | user-input error (bad value, bad local path, conflicting flags); also `doctor --strict` probe failure | read stderr; fix the argument |
| 2 | usage error (unknown command/flag, missing argument) | check flag ordering and syntax in `caption capabilities` |
| 3 | configuration error (missing env var) | identify which var THAT command needs (see below) |
| 4 | upstream failure (HTTP error, Meili error, malformed response) | read the embedded error report; retry once; then `doctor --strict` |
| 5 | remote resource not found (404) | verify the UUID against cached `list_projects` / `list_matters` output |

## Global-flag ordering (common exit-2 cause)

Global flags must precede the subcommand:

```bash
caption --output json --output-file caption_cache/x.json list_projects   # correct
caption list_projects --output json                                      # exit 2
```

Also: `caption --version` does not exist (exit 2). Use `caption capabilities` and read `.version`.

## Missing environment (exit 3)

Requirements are per-command, not global - do NOT assume every command needs `CAPTION_API_URL`:

- Caption workspace API commands (`list_projects`, `list_folders`, `create_*`, `edit_*`, `dl_transcript`, `assign_speakers`, `list_speakers`, `rename_speaker`): `CAPTION_API_URL` + `CLERK_API_KEY`
- `token`, `search`: additionally `CAPTION_MEILI_URL`
- Hosted history (`sync`, `list_matters`, `create_md`, `list_md`, `get_md`): `CLERK_API_KEY` + `ORGANIZATION_ID`, no `CAPTION_API_URL` needed
- `capabilities`, `robot-docs`, `--help`: nothing

Recovery:
1. A `.env` in `$PWD` is loaded automatically; pass `--env-file <path>` for another location.
2. Hosted history commands also accept `--clerk-api-key` and `--org-id` flags directly.
3. Confirm with `caption --output json doctor --strict`.

## Missing installed CLI

Symptom: `command not found: caption`.

```bash
uv tool install --force --python 3.13 "caption-cli @ git+https://github.com/sec-chair/caption-cli.git"
uv tool update-shell   # if still not on PATH
```

Do not substitute a repo-local launcher.

## Hosted-history auth failures (sync, list_matters, *_md)

Symptoms: 401/403 from `history.caption.fyi`.

1. Confirm `CLERK_API_KEY` and `ORGANIZATION_ID` are set (or passed as `--clerk-api-key` / `--org-id`).
2. Verify org access: `caption --output json doctor --strict` shows the resolved `organization` and whether the `agentsview` feature probe passes.
3. Remember these credentials are separate concerns from `CAPTION_API_URL` - a working workspace API proves nothing about hosted history.

## Meili/search auth failures

`search` refreshes the token via `/search/token` once automatically and retries. If it still fails:
1. `caption token` to force a refresh (output redacted; only add `--show-token` when raw output is explicitly required, and never paste it into logs or chat).
2. Persistent 401/403 means `CLERK_API_KEY` is invalid or lacks scope.

## Condensed output is missing fields

`list_projects`, `list_folders`, `list_matters`, and `list_md` condense output by default and announce it on stderr. Re-run with `--full` for raw server payloads:

```bash
caption --output json --output-file caption_cache/list_projects_full.json list_projects --full
```

## Bulk sync guard

`caption sync --session-id '*'` selects EVERY session. Without `--test`, the CLI requires `--yes`. Rules:
1. Never add `--yes` unless the user explicitly asked to sync all sessions.
2. Preview first: `caption sync --session-id '*' --test` prints payloads and sends nothing.
3. `--session-id` is a case-insensitive substring match - a short value can silently match multiple sessions; check the `--test` output.
4. Exit 4 on partial failure embeds a full sent/failures report in the error message - read it before retrying.

## Speaker validation failures (exit 1)

- `assign_speakers` requires exactly one of `--transcript-id` / `--project-id` and exactly one of `--speaker-id` / `--name`.
- `--channel` accepts only `0|1|2|microphone|loopback|external`.
- Prefer `--name`: the API does not verify that a `--speaker-id` belongs to the same project - a foreign ID corrupts assignments silently.
- `rename_speaker` rejects user-backed speakers; only custom speakers can be renamed.
- Scout targets first: `caption list_speakers <transcript-uuid>`, then validate with `--dry-run` before the real call.

## Edit validation failures (exit 1)

- `edit_project` / `edit_folder` require at least one field.
- Set/clear pairs conflict: `--description` vs `--clear-description`, `--folder` vs `--clear-folder`, `--parent` vs `--clear-parent`. Pick one side.
- Use `--dry-run` to print `{dry_run, method, path, body}` without sending.

## Output too large for the terminal

Every request should already use `--output-file caption_cache/<name>`. If output was lost:

```bash
ls -lt caption_cache
head -n 40 caption_cache/<file>
grep -n "term" caption_cache/<file>
```

Cite the cache file path in any summary so the full output stays reviewable.
