# External Integrations

**Analysis Date:** 2026-02-26

## APIs & External Services

**Caption API:**
- REST API for project/folder management and transcript operations
- SDK/Client: httpx (raw HTTP with Bearer token)
- Auth: `CAPTION_TOKEN` environment variable (Bearer token in Authorization header)
- Endpoints used:
  - `GET /search/token` - Fetch Meilisearch credentials (line 175)
  - `GET /users/me/workspace` - Get current workspace UUID (line 262)
  - `GET /workspaces/{workspaceId}/projects` - List projects with pagination (line 278)
  - `GET /workspaces/{workspaceId}/folders` - List folders with pagination (line 278)
  - `POST /workspaces/{workspaceId}/projects` - Create new project (line 694)
  - `POST /workspaces/{workspaceId}/folders` - Create new folder (line 725)
  - `PATCH /projects/{projectId}` - Edit project properties (line 761)
  - `PATCH /workspaces/folders/{folderId}` - Edit folder properties (line 797)
  - `GET /transcripts/{transcriptId}/captions` - Download transcript captions (line 812)

**Meilisearch:**
- Search engine for full-text search across indices
- SDK/Client: meilisearch 0.40.0+
- Auth: Dynamic token fetched from `GET {API_URL}/search/token`, cached locally
- Endpoints/Indices searched:
  - Default index: `transcript_captions_v1` (line 19)
  - Supported indices: `transcript_captions_v1`, `projects_v1`, `workspace_folders_v1`, `transcript_sessions_v1` (documented in README line 73)
- Search retry logic: On auth failure (401, 403, invalid_api_key), automatically refreshes token and retries once (lines 352-387)
- Token cache: Stored in file specified by `--cache-path` or `CAPTION_MEILI_CACHE` env var

## Data Storage

**File Storage:**
- Local filesystem only
- Search token cache: JSON file (default: `search-token.json`)
  - Format: `{ "token": string, "url": string?, "expiresAt": string? }`
  - Location: `--cache-path` argument or `CAPTION_MEILI_CACHE` env var
  - Read: `load_cached_search_token()` (line 286)
  - Write: `save_search_token()` (line 301)

**Databases:**
- No direct database integration
- Data persistence handled entirely by Caption API and Meilisearch backends

**Caching:**
- Meilisearch search token cached locally in JSON file
- Token includes optional `expiresAt` field (not enforced in cache)
- Cache is updated on: initial `token` command, or on authentication failure during search

## Authentication & Identity

**Auth Provider:** Custom Bearer token
- Token type: Opaque bearer token provided by Caption API
- Header format: `Authorization: Bearer {token}`
- Scope: Single token grants access to Caption API and receives Meilisearch credentials
- Flow:
  1. Client provides `CAPTION_TOKEN` (human-managed)
  2. CLI exchanges it for Meilisearch credentials via `GET /search/token`
  3. Meilisearch token is cached locally and used for search operations
  4. If Meilisearch auth fails, token is refreshed and search is retried

## Monitoring & Observability

**Error Tracking:** None detected
- Errors printed to stderr and exit with code 1
- HTTP errors from httpx caught and logged (line 1102)
- Meilisearch errors caught and logged (line 1102)

**Logs:**
- Console output only (stdout for success, stderr for errors)
- CLI errors: `CliError` exception with message and optional exit code (line 41)
- HTTP/Meili errors: Caught as `httpx.HTTPError` or `MeilisearchApiError` and printed to stderr (line 1102-1103)

## CI/CD & Deployment

**Hosting:** Not applicable (CLI tool)
- Single-file distribution: `caption.py`
- Installable via: `uv install` / `pip install` from wheel

**CI Pipeline:** None detected
- `.planning/` directory not in `.gitignore` (standard Python ignores: `*.pyc`, `.env`, `search-token.json`)

## Environment Configuration

**Required env vars:**
- `CAPTION_API_URL` - Caption API base URL (all commands)
- `CAPTION_TOKEN` - Bearer token (authenticated API calls)
- `CAPTION_MEILI_URL` - Meilisearch URL (token and search commands)

**Optional env vars:**
- `CAPTION_MEILI_CACHE` - Override default cache path

**Secrets location:**
- `.env` file (listed in `.gitignore`, not committed)
- Never commits secrets to repo

## Webhooks & Callbacks

**Incoming:** None
- CLI tool does not expose webhook endpoints

**Outgoing:** None
- CLI makes only HTTP requests to configured APIs
- No callbacks or server-push functionality

## API Error Handling

**Caption API errors:**
- HTTP status >= 400: Raised as `CliError` with status code and response detail (line 182, 210, 239)
- Timeout: 15 seconds, raised as `httpx.HTTPError` (caught at line 1102)

**Meilisearch errors:**
- Auth failures (401, 403, invalid_api_key): Triggers refresh-and-retry logic (line 374)
- Other errors: Wrapped in `CliError` with stringified message (line 375)
- Timeout: 15 seconds (httpx default for Meilisearch client)

**Search token refresh:**
- Cached token used first (line 353)
- On Meili auth failure, fresh token fetched via `GET {API_URL}/search/token` (line 378)
- New token saved to cache for future use (line 379)
- Search operation retried once (line 385)

## Rate Limiting

**Not documented:** No explicit rate-limiting configuration detected
- Meilisearch client uses default httpx timeout (15s) for all operations
- No backoff or retry delay specified beyond single-attempt retry on auth failure

---

*Integration audit: 2026-02-26*
