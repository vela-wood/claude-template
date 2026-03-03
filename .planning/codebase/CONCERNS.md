# Codebase Concerns

**Analysis Date:** 2026-02-26

## Tech Debt

**Monolithic single-file module:**
- Issue: Entire application logic (1110 lines) lives in `caption.py` with no separation of concerns. All API clients, command handlers, output formatters, and utility functions are mixed together.
- Files: `caption.py`
- Impact: Difficult to test individual components in isolation. High cognitive load when modifying any single feature. Increases likelihood of unintended side effects when adding new commands.
- Fix approach: Extract into modules: `clients/` (API, Meilisearch), `commands/` (command handlers), `output/` (formatters), `models/` (data structures). Maintain backwards compatibility by keeping module entry point.

**Inconsistent HTTP client instantiation:**
- Issue: Three separate functions create `httpx.Client` instances independently (`fetch_search_token`, `_authorized_request`, `_authorized_get_list_of_objects`). Each opens/closes connection separately with identical timeouts.
- Files: `caption.py` lines 177, 201, 234
- Impact: Redundant connection overhead, harder to configure timeouts globally, potential for timeout inconsistency if changed in one place but not others.
- Fix approach: Create singleton HTTP client factory or dependency-inject shared client. Store in `RuntimeConfig`.

**Token expiration not validated:**
- Issue: `SearchToken.expires_at` is cached and returned but never validated. Tokens may be used after expiration.
- Files: `caption.py` lines 50, 60-69, 352-360
- Impact: Search operations may fail silently with auth errors if cached token has expired. User must manually clear cache to recover.
- Fix approach: Check `expires_at` timestamp in `_require_cached_or_fresh_token()` before returning cached token. Refresh automatically if expired.

**Unvalidated JSON response parsing:**
- Issue: Three locations call `response.json()` without catching `json.JSONDecodeError`. If API returns invalid JSON, the exception bubbles up uncaught.
- Files: `caption.py` lines 184, 212, 241
- Impact: Cryptic error messages to user if API returns malformed JSON. CLI terminates with traceback instead of user-friendly error.
- Fix approach: Wrap `response.json()` calls in try/except to raise `CliError` with clear message about response format.

**Inconsistent API endpoint paths:**
- Issue: Folder operations use different endpoint paths inconsistently:
  - Create: `/workspaces/{workspaceId}/folders` (line 729)
  - Edit: `/workspaces/folders/{folderId}` (line 801)
- Files: `caption.py` lines 729, 801
- Impact: If API endpoint structure changes, one path may be forgotten. Edit folder may hit wrong endpoint.
- Fix approach: Standardize path construction. Consider parameterized path builder to prevent this pattern.

**Missing input validation for date parsing:**
- Issue: `_parse_iso_datetime()` only handles ISO format with Z or +HH:MM timezone. Rejects valid ISO formats like `±HH:MM` or no timezone. Used by transcript rendering.
- Files: `caption.py` lines 421-428
- Impact: Transcripts with certain valid ISO timestamp formats fail to render to markdown.
- Fix approach: Enhance parser or use `dateutil.parser` for more flexible parsing. Add test cases for edge formats.

**Pagination loop lacks upper bound:**
- Issue: `_command_list_workspace_items()` loops `while True` and breaks on `page >= total_pages`. If API returns incorrect `totalPages`, could loop indefinitely on network retry.
- Files: `caption.py` lines 551-580
- Impact: Potential infinite loop if API returns corrupted pagination metadata or if there's retry logic added in future.
- Fix approach: Add max iteration limit (e.g., `page > 1000`) as circuit breaker.

**Channel hardcoding in transcript:**
- Issue: `_speaker_label_for_transcript_item()` hardcodes channel 0 = "me" and channel 1 = "meeting-X". No way to customize speaker labels.
- Files: `caption.py` lines 440-445
- Impact: Cannot handle transcripts with more than 2 channels. Label "me" may not match context (e.g., "speaker", "user", custom names).
- Fix approach: Accept optional speaker name mapping from config or arguments.

## Known Bugs

**Create folder returns 200, but API creates with 201:**
- Symptoms: Command completes successfully but HTTP status code expectation mismatch if API changes.
- Files: `caption.py` line 731 expects `{200}`, but line 700 (project creation) expects `{201}`
- Trigger: Check API documentation—folder create should likely expect 201 like project create.
- Workaround: If API returns 201, currently fails with "Failed POST ... (201)" error. Can manually adjust expected status.
- Recommendation: Verify with API spec which status is correct and standardize both create operations.

**Folder edit endpoint path inconsistency with create:**
- Symptoms: If endpoints are `/workspaces/{id}/folders` for creation and `/workspaces/folders/{id}` for edit, edit will hit wrong path.
- Files: `caption.py` lines 729 (create), 801 (edit)
- Trigger: Call `edit_folder` command.
- Workaround: None—will fail at API level.
- Recommendation: Verify API documentation and ensure both use same path structure.

## Security Considerations

**API token in memory without cleanup:**
- Risk: `api_token` stored in `RuntimeConfig` dataclass and passed through function calls. No mechanism to clear from memory after use.
- Files: `caption.py` lines 74-79 (RuntimeConfig), 1080-1086 (config instantiation)
- Current mitigation: Tokens stored as environment variables initially; Python garbage collection handles cleanup.
- Recommendations: Consider using `secrets` module or secure memory library if handling sensitive keys. Document token security in README.

**Search token cached in plain JSON file:**
- Risk: `search-token.json` contains bearer token in plaintext. File permissions depend on default umask.
- Files: `caption.py` lines 301-303 (save), DEFAULT_CACHE_PATH = "search-token.json"
- Current mitigation: Assumed to run in secure environment. Token expires (though expiration not validated).
- Recommendations: Add file permission enforcement (chmod 600), document security implications, consider encrypted storage.

**Environment variables passed to subprocess:**
- Risk: If CLI is wrapped by scripts, environment variables containing tokens may be visible in process list.
- Files: Not directly in codebase, but inherent to design using environment variables.
- Current mitigation: Environment variable design is intentional (CAPTION_TOKEN, CAPTION_API_URL).
- Recommendations: Document in README that tokens in environment are visible to child processes.

**No rate limiting or retry backoff:**
- Risk: If API quota/rate limit is hit, CLI immediately fails. No exponential backoff. Could be abused if token compromised.
- Files: `caption.py` lines 363-387 (retry logic only for auth errors, not rate limits)
- Current mitigation: Single auth retry only. No other retries.
- Recommendations: Add configurable retry with exponential backoff for 429/503 responses.

## Performance Bottlenecks

**Synchronous pagination over HTTP:**
- Problem: List operations fetch pages sequentially. Workspace with 10,000 items requires 100 HTTP requests (100 page limit). Each request waits for previous to complete.
- Files: `caption.py` lines 536-588
- Cause: Python is single-threaded; awaits each response before fetching next.
- Improvement path: Use `httpx.AsyncClient` with concurrent requests or thread pool. Batch pagination fetches.

**No connection pooling:**
- Problem: Each command creates fresh `httpx.Client` that opens/closes connection. Multiple commands in sequence incur TCP handshake overhead.
- Files: `caption.py` lines 177, 201, 234
- Cause: Clients created per-function rather than reused.
- Improvement path: Move to singleton client or connection pool. Reuse across multiple requests.

**Full transcript download without streaming:**
- Problem: `dl_transcript()` loads entire caption list into memory before rendering. Large transcripts (10k+ items) consume memory.
- Files: `caption.py` lines 808-821, 448-485
- Cause: Fetches all items, then processes all in memory for markdown rendering.
- Improvement path: Stream output line-by-line, process/output captions as they arrive.

## Fragile Areas

**Transcript markdown rendering with edge cases:**
- Files: `caption.py` lines 448-485
- Why fragile: Complex logic for grouping speaker paragraphs. Multiple validation points for channel, index, createdAt. If API changes field structure or adds new channel types, fails.
- Safe modification: Add comprehensive test cases for edge formats (Z vs ±HH:MM timezones, missing diarization_index, channel > 1). Use property-based testing for timestamp parsing.
- Test coverage: Tests exist for merging speakers (line 833) and missing fields (line 854), but not for malformed timestamps or edge channel values.

**Output formatting with type gymnastics:**
- Files: `caption.py` lines 390-418
- Why fragile: `_render_table()` checks `isinstance(value, dict)` and then nested `isinstance(items, list)`. Different output paths for JSON/table/markdown. Easy to miss edge case where output type doesn't match handler.
- Safe modification: Refactor output handlers into strategy pattern. Add type guards or use Pydantic models for output validation.
- Test coverage: Tests for transcript rendering exist, but no tests for table output or edge cases in JSON.

**Meilisearch auth error detection:**
- Files: `caption.py` lines 333-343
- Why fragile: `_is_meili_auth_error()` checks status codes, error codes, and string patterns in error messages. If Meilisearch changes error message format or status codes, detection breaks.
- Safe modification: Maintain explicit mapping of known auth error patterns. Add logging to detect when pattern detection fails. Add tests for different error response formats.
- Test coverage: One test exists for auth retry (line 245), but only tests `FakeAuthError`. Real Meilisearch error formats not tested.

**Workspace ID lookup on every command:**
- Files: `caption.py` lines 261-266, 599-605
- Why fragile: List/create commands always call `/users/me/workspace` to get current workspace. If user workspace changes between invocations or permission changes, command behavior changes unexpectedly.
- Safe modification: Cache workspace ID in config file (like search token). Add `--workspace-id` override. Document behavior.
- Test coverage: Workspace lookup tested (line 343) but not workspace ID caching scenarios.

**CLI arguments with special characters:**
- Files: `caption.py` lines 841-898 (argument parsing)
- Why fragile: Project/folder names and descriptions passed as-is to API. CLI doesn't validate or escape. If API has size limits or character restrictions, errors surface late.
- Safe modification: Add validation for name length (API limits), character restrictions. Test with unicode, quotes, newlines.
- Test coverage: No tests for edge case names.

## Scaling Limits

**Single-threaded pagination:**
- Current capacity: 100 items per page × max pages. ~10,000 items feasible (~1 min for 100 pages).
- Limit: Where it breaks: Workspaces with 100k+ items will timeout or consume significant time.
- Scaling path: Implement concurrent page fetching with thread pool or asyncio.

**Meilisearch search with large result sets:**
- Current capacity: Default limit=5, configurable to any value.
- Limit: Memory constraint—returning 10k results locks up CLI.
- Scaling path: Add pagination to search results or streaming output.

**Cache file grows unbounded:**
- Current capacity: Single token stored in cache, negligible size.
- Limit: If feature adds caching of user data, cache could grow. No cleanup.
- Scaling path: Implement cache eviction policy (TTL, max size).

## Dependencies at Risk

**python-dotenv without pinned patch:**
- Risk: `python-dotenv>=1.1.1` in pyproject.toml allows major version changes. Could have breaking changes.
- Files: `pyproject.toml` line 13
- Impact: Future installs might get incompatible version.
- Migration plan: Pin to `>=1.1.1,<2.0.0` to allow patch updates but prevent major.

**httpx without explicit timeout enforcement:**
- Risk: httpx defaults to 5 second timeout; code explicitly sets 15s. If httpx changes defaults, behavior changes silently.
- Files: `caption.py` lines 177, 201, 234 (all hardcode 15.0)
- Impact: Timeouts vary unexpectedly if dependency updated.
- Migration plan: Document timeout rationale. Consider config-driven timeout with sensible default.

**meilisearch SDK version pinned loosely:**
- Risk: `meilisearch>=0.40.0` allows breaking API changes in 0.41+.
- Files: `pyproject.toml` line 12
- Impact: New version could change client API.
- Migration plan: Pin to `>=0.40.0,<1.0.0` or monitor breaking changes.

## Missing Critical Features

**No support for resuming interrupted pagination:**
- Problem: If pagination is interrupted (network error after page 50 of 200), user must restart from page 0.
- Blocks: Reliable listing of large workspaces.

**No caching of workspace data:**
- Problem: List operations always hit API. No client-side cache of projects/folders.
- Blocks: Efficient repeated queries (e.g., same search command twice).

**No user-defined output formatting:**
- Problem: Output is hardcoded to JSON/table/markdown. No template or custom format option.
- Blocks: Integration with downstream tools requiring specific format.

**No interactive mode or REPL:**
- Problem: Each command requires full CLI invocation with environment setup.
- Blocks: Workflows with multiple sequential commands (create folder, list projects, create project).

## Test Coverage Gaps

**API response error handling:**
- What's not tested: Malformed JSON responses, truncated responses, binary responses.
- Files: `caption.py` lines 184, 212, 241
- Risk: Code will crash with traceback instead of user-friendly error.
- Priority: High

**Timestamp parsing edge cases:**
- What's not tested: ISO timestamps with various timezone formats (Z, +00:00, ±HH:MM, no timezone), millisecond vs microsecond precision, year 2100+.
- Files: `caption.py` lines 421-428
- Risk: Transcripts with valid but uncommon timestamp formats fail silently or crash.
- Priority: Medium

**Output formatting for non-standard data:**
- What's not tested: Table output with null values, very long values, nested structures. JSON output with circular references (edge case).
- Files: `caption.py` lines 390-418
- Risk: Output formatting may fail or produce garbled output with edge case data.
- Priority: Medium

**Pagination with edge cases:**
- What's not tested: totalPages=0, totalPages=-1, items count != page size, empty result set on page 0.
- Files: `caption.py` lines 551-580
- Risk: Loop termination logic may fail or produce incorrect result counts.
- Priority: High

**Speaker label generation:**
- What's not tested: Channel values > 1, negative channels, missing index field, diarization_index=-1.
- Files: `caption.py` lines 431-445
- Risk: Transcript rendering crashes on unexpected diarization data.
- Priority: Medium

**Meilisearch client error scenarios:**
- What's not tested: Connection refused, timeout, SSL errors, non-auth API errors.
- Files: `caption.py` lines 363-387
- Risk: Only single retry on auth errors. Other errors bubble up with generic message.
- Priority: Medium

---

*Concerns audit: 2026-02-26*
