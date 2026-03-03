# Architecture

**Analysis Date:** 2026-02-26

## Pattern Overview

**Overall:** Modular CLI application with command dispatch architecture

**Key Characteristics:**
- Single-file Python application with layered separation of concerns
- Command specification pattern for declarative command definition
- Request/response pipeline with configuration injection
- Stateless command handlers with dependency injection via `RuntimeConfig`
- Token caching and automatic refresh mechanism for Meilisearch auth

## Layers

**CLI Interface Layer:**
- Purpose: Parse arguments, handle user input, display output
- Location: `caption.py` (lines 125-172, 824-961, 1073-1093)
- Contains: Argument parsing (`build_parser`, `parse_args`), command handlers (`_handle_*`), output formatting (`emit_output`)
- Depends on: Core business logic, command specifications
- Used by: `main()` entry point

**Command Layer:**
- Purpose: Execute business operations, orchestrate API calls and transformations
- Location: `caption.py` (lines 509-821)
- Contains: Command implementations (`command_*`, `dl_transcript`), helper builders (`_build_create_body`, `_build_edit_body`)
- Depends on: API integration layer, data transformation, configuration
- Used by: Command handlers (`_handle_*`)

**API Integration Layer:**
- Purpose: Handle HTTP communication with Caption API and Meilisearch
- Location: `caption.py` (lines 174-247, 261-284, 301-324)
- Contains: HTTP clients (`fetch_search_token`, `_authorized_request`, `_authorized_get`, `_authorized_get_list_of_objects`, `build_meili_client`)
- Depends on: httpx, meilisearch SDK, authentication config
- Used by: Command layer

**Data Transformation Layer:**
- Purpose: Transform API responses to user-facing output, validate and convert data types
- Location: `caption.py` (lines 249-259, 421-507)
- Contains: View functions (`_project_view`, `_folder_view`, `_field_view`), output formatting (`_render_table`, `_transcript_items_to_md`, `emit_output`), parsing utilities (`_parse_iso_datetime`, `_speaker_label_for_transcript_item`)
- Depends on: Data structures, format specifications
- Used by: Command layer, CLI interface

**Error Handling & Auth Layer:**
- Purpose: Manage credential caching, token refresh, error detection and recovery
- Location: `caption.py` (lines 286-350, 363-388)
- Contains: Cache management (`load_cached_search_token`, `save_search_token`), auth retry logic (`_run_with_single_auth_retry`, `_is_meili_auth_error`), requirement checkers (`_require_*`)
- Depends on: File I/O, configuration, HTTP layer
- Used by: Command layer, token management

**Configuration Layer:**
- Purpose: Hold runtime configuration and validate prerequisites
- Location: `caption.py` (lines 40-91)
- Contains: Data classes (`CliError`, `SearchToken`, `RuntimeConfig`, `CommandSpec`)
- Depends on: Environment variables, file paths
- Used by: All layers

## Data Flow

**Search Operation:**

1. User invokes: `caption search "query" --index projects_v1 --limit 10`
2. CLI Interface: `parse_args()` → load `.env`, resolve defaults, return `Namespace`
3. Command Dispatch: Lookup `CommandSpec` for "search", invoke handler
4. Handler: `_handle_search()` → calls `command_search(config, query, index, limit)`
5. Auth Layer: `_require_cached_or_fresh_token()` → load from cache or fetch fresh
6. API Layer: `_run_with_single_auth_retry()` wraps search operation
7. Search Op: `client.index(index).search(query, {"limit": limit})`
8. Retry: On 401/403, refresh token, rebuild client, retry once
9. Output: `emit_output(result, "json")` → print JSON or fall back to `_render_table()`

**Create Project Operation:**

1. User invokes: `caption create_project "My Project" --description "Desc"`
2. CLI Interface: Parse args → `Namespace` with name, description, workspace_id=None
3. Handler: `_handle_create_project()` → calls `command_create_project(config, name, description, workspace_id)`
4. Config Resolution: If workspace_id is None, call `fetch_current_workspace_id()` → GET `/users/me/workspace`
5. Body Building: `_build_create_body()` → validate name, construct JSON body: `{"name": "My Project", "description": "Desc"}`
6. API Layer: `_authorized_request()` → POST `/workspaces/{ws_id}/projects`, expect 201
7. Transform: `_project_view(payload)` → filter to `PROJECT_OUTPUT_FIELDS`
8. Output: `emit_output(result, "json")`

**Download Transcript Operation:**

1. User invokes: `caption dl_transcript transcript-uuid`
2. CLI Interface: Default output is "md" (not "json")
3. Handler: `_handle_dl_transcript()` → calls `dl_transcript(config, transcript_id)`
4. API Layer: `_authorized_get_list_of_objects()` → GET `/transcripts/{id}/captions` → returns array of caption objects
5. Payload: Return dict with `transcriptId`, `items` (array), `count`
6. Transform: `_is_transcript_payload()` detects transcript structure
7. Format: If output="md", call `_transcript_items_to_md(items)`
   - Group consecutive captions by speaker (channel + index)
   - Merge consecutive lines from same speaker
   - Format as `[HH:MM.SS] speaker: content`
8. Output: Print markdown or JSON

**State Management:**

- Configuration immutable in `RuntimeConfig` dataclass (slots=True)
- Token state persisted to JSON file at `cache_path`
- No global state except environment variables and file I/O
- Each command execution is independent

## Key Abstractions

**CommandSpec:**
- Purpose: Declarative specification of a CLI command
- Location: `caption.py` (lines 82-91)
- Pattern: Frozen dataclass holding command metadata (name, help, argument builder, handler, output defaults)
- Used by: `_command_specs()` registry, `build_parser()` for subparser creation, `run()` for dispatch

**SearchToken:**
- Purpose: Represent Meilisearch authentication credentials with optional expiry
- Location: `caption.py` (lines 46-70)
- Pattern: Dataclass with `from_payload()` factory and `to_payload()` serializer
- Used by: Token caching, credential passing to Meilisearch client

**RuntimeConfig:**
- Purpose: Dependency injection container holding all runtime configuration
- Location: `caption.py` (lines 74-79)
- Pattern: Dataclass (slots=True) with API URL, token, Meili URL, cache path, output format
- Used by: All command handlers, passed through entire call stack

**CliError:**
- Purpose: Custom exception for user-facing error messages
- Location: `caption.py` (lines 41-43)
- Pattern: Dataclass with message and exit_code (default 1)
- Used by: All validation/error paths, caught in `main()` to emit to stderr

## Entry Points

**`main()` function:**
- Location: `caption.py` (lines 1096-1106)
- Triggers: Script execution via `if __name__ == "__main__"` or from entry point `caption` (defined in pyproject.toml)
- Responsibilities:
  - Catch `CliError`, `httpx.HTTPError`, `MeilisearchApiError`
  - Print error to stderr
  - Exit with appropriate code

**`run(argv)` function:**
- Location: `caption.py` (lines 1073-1093)
- Triggers: Called by `main()`, testable with custom argv
- Responsibilities:
  - Parse arguments with environment variable loading
  - Load command spec
  - Build `RuntimeConfig` from env vars
  - Validate required config (API URL always; Meili URL only if `needs_meili=True`)
  - Dispatch to command handler
  - Format and emit output
  - Return exit code (0 on success)

## Error Handling

**Strategy:** Fail-fast with descriptive error messages

**Patterns:**

**Input Validation:**
```python
# Example from create_project
cleaned_name = name.strip()
if not cleaned_name:
    raise CliError(f"{command_name} requires a non-empty name")
```

**API Error Handling:**
```python
# _authorized_request checks status codes
if response.status_code not in expected_statuses:
    detail = response.text.strip() or response.reason_phrase
    raise CliError(f"Failed {method.upper()} {path} ({response.status_code}): {detail}")
```

**Auth Retry:**
```python
# _run_with_single_auth_retry detects auth errors and retries once
try:
    return operation(client)
except Exception as exc:
    if not _is_meili_auth_error(exc):
        raise CliError(_stringify_error(exc)) from exc
    # Refresh token and retry
    refreshed_token = fetch_search_token(...)
    retry_client = build_meili_client(...)
    return operation(retry_client)
```

**Data Validation:**
```python
# Type checking on API responses
payload = response.json()
if not isinstance(payload, dict):
    raise CliError(f"/search/token returned non-object JSON")
```

## Cross-Cutting Concerns

**Logging:** No logging framework; only stderr for errors via `print(exc.message, file=sys.stderr)`

**Validation:** Every API response is validated for type and required fields before use

**Authentication:** Bearer token auth on all API calls via `Authorization: Bearer {token}` header

**Timeouts:** All httpx clients use 15-second timeout: `httpx.Client(timeout=15.0)`

**Output Formatting:**
- Default format per command (json for most, md for dl_transcript)
- Override via `--output` flag
- Supports: json (pretty-printed), md (for transcripts), table (TSV-like for lists)
