# Coding Conventions

**Analysis Date:** 2026-02-26

## Naming Patterns

**Files:**
- Single module: `caption.py` - monolithic command-line application
- Test files: `test_caption.py` - follows pytest convention of `test_*.py` pattern

**Functions:**
- Lowercase with underscores: `parse_args()`, `fetch_search_token()`, `_authorized_request()`
- Private functions prefixed with underscore: `_require_api_url()`, `_field_view()`, `_build_create_body()`
- Command handlers prefixed with underscore: `_handle_token()`, `_handle_search()`
- Helper functions prefixed with underscore: `_add_search_arguments()`, `_stringify_error()`

**Variables:**
- Lowercase with underscores: `api_url`, `api_token`, `cache_path`, `search_token`
- Private/internal variables: `_args`, lowercase underscores
- Loop variables: `page`, `item`, `key`
- Constants: UPPERCASE with underscores: `DEFAULT_CACHE_PATH`, `DEFAULT_LIMIT`, `DEFAULT_SEARCH_INDEX`, `WORKSPACE_LIST_PAGE_SIZE`
- Tuple constants for field definitions: `PROJECT_OUTPUT_FIELDS`, `FOLDER_OUTPUT_FIELDS`

**Types:**
- Dataclass names are PascalCase: `CliError`, `SearchToken`, `RuntimeConfig`, `CommandSpec`
- Type hints use lowercase unions: `str | None` (Python 3.12+ syntax, see line 1: `from __future__ import annotations`)

## Code Style

**Formatting:**
- No explicit formatter configured (no `.prettierrc` or similar)
- PEP 8 style observed throughout
- 4-space indentation (standard Python)
- Line length: appears to follow standard conventions (see lines in `caption.py` range 60-110 characters typical)
- Slot-based dataclasses used: `@dataclass(slots=True)` for memory efficiency (lines 40, 46, 73, 82)

**Linting:**
- No explicit linting configuration (.flake8, .pylintrc, etc.) detected
- Code appears clean with proper imports, type hints, and exception handling
- Future annotations imported at module top: `from __future__ import annotations` (line 1)

## Import Organization

**Order:**
1. `from __future__ import annotations` - future imports first (line 1)
2. Standard library imports: `argparse`, `json`, `os`, `sys`, `from dataclasses import dataclass`, `from datetime import datetime`, `from pathlib import Path`, `from typing import Any, Callable, Mapping, Sequence` (lines 3-10)
3. Third-party imports: `httpx`, `meilisearch`, `python-dotenv` (lines 12-15)
4. Relative imports: None used (monolithic structure)

**Path Aliases:**
- Not applicable - single-file module

**Blank Lines:**
- Two blank lines between top-level definitions (dataclasses, functions)
- One blank line within class/function bodies between logical sections

## Error Handling

**Patterns:**
- Custom exception class `CliError` defined as dataclass (lines 40-43):
  - `message: str` - user-facing error message
  - `exit_code: int = 1` - exit code for SystemExit
- Exceptions raised with context: `raise CliError(...) from exc` (lines 293, 375, 387, 428)
- Broad exception catching for specific error types:
  - JSON/file errors: `except (json.JSONDecodeError, OSError) as exc` (line 292)
  - HTTP/Meilisearch errors: `except (httpx.HTTPError, MeilisearchApiError) as exc` (line 1102)
  - Checked exceptions in main: `except CliError as exc` vs. broader library exceptions (lines 1099-1104)
- Validation pattern: Check conditions, raise `CliError` immediately if invalid (lines 169, 182, 186, 210, 214, etc.)

**Error Message Format:**
- Descriptive: includes context and expected values
- Examples:
  - `f"Failed to fetch search token ({response.status_code}): {detail}"` (line 182)
  - `f"Failed {method.upper()} {path} ({response.status_code}): {detail}"` (line 210)
  - `"{path} returned non-object JSON"` (line 214)
  - `"Missing Caption API URL. Set CAPTION_API_URL"` (line 309)

## Logging

**Framework:** `print()` to stdout/stderr only

**Patterns:**
- Error output goes to `sys.stderr`: `print(exc.message, file=sys.stderr)` (line 1100)
- Success output goes to `sys.stdout` via `emit_output()` (lines 496-506)
- JSON output: `json.dumps(value, indent=2)` (line 498)
- No structured logging or log levels (DEBUG, INFO, ERROR)
- Single responsibility: emit_output() handles formatting (lines 496-506)

## Comments

**When to Comment:**
- Minimal comments throughout codebase
- Comments explain "why" not "what" where present
- Docstrings: None used (single-file monolith)

**Type Hints:**
- Comprehensive type hints used throughout
- Examples:
  - Function parameters: `def fetch_search_token(api_url: str, api_token: str) -> SearchToken:` (line 174)
  - Return types: `-> dict[str, Any]`, `-> str`, `-> list[Mapping[str, Any]]` (lines 227, 261, 269)
  - Complex types: `Callable[[argparse.ArgumentParser], None]`, `Mapping[str, int] | None` (lines 86, 195)

## Function Design

**Size:**
- Utility functions: 5-20 lines typical (e.g., `_require_api_url()`, `_field_view()`)
- Command handlers: 2-5 lines (delegates to command functions)
- Command functions: 10-30 lines (business logic)
- Complex functions: 30-50 lines with clear structure (e.g., `_transcript_items_to_md()`, `_run_with_single_auth_retry()`)

**Parameters:**
- Positional parameters for required inputs
- Keyword-only parameters after `*` for optional/named arguments (lines 269-276, 615-622)
- Type hints on all parameters
- Config object pattern: `config: RuntimeConfig` passed as first parameter to command functions (lines 509, 523, 591, 682-687)

**Return Values:**
- Consistent return types: functions return `dict[str, Any]` for command results (lines 509, 523, 591)
- Helper functions return specific types: `str`, `SearchToken`, `list[dict]`, etc.
- Void functions: `-> None` explicitly annotated (lines 301, 304, 496, 824)

**Example Function Structure (command pattern):**
```python
def command_search(config: RuntimeConfig, query: str, index: str, limit: int) -> dict[str, Any]:
    resolved_index = index.strip()
    if not resolved_index:
        raise CliError("--index cannot be empty")

    token_payload = _require_cached_or_fresh_token(config)

    def _operation(client: meilisearch.Client) -> dict[str, Any]:
        return client.index(resolved_index).search(query, {"limit": limit})

    return _run_with_single_auth_retry(config, _operation, token_payload)
```

## Module Design

**Exports:**
- Public functions (no leading underscore): `parse_args()`, `build_parser()`, `run()`, `main()`, `command_*()`, `fetch_*()`, `emit_output()`
- Private functions (leading underscore): `_authorized_request()`, `_require_api_url()`, `_handle_*()`, `_add_*_arguments()`
- No `__all__` defined - relies on naming convention

**Constants at module level:**
- Configuration defaults: `DEFAULT_CACHE_PATH`, `DEFAULT_LIMIT`, `DEFAULT_SEARCH_INDEX`, `WORKSPACE_LIST_PAGE_SIZE` (lines 17-20)
- Output field definitions: `PROJECT_OUTPUT_FIELDS`, `FOLDER_OUTPUT_FIELDS` (lines 21-37)

**Dataclass Pattern:**
- `CliError` - domain exception (slots=True for efficiency)
- `SearchToken` - domain model with factory method `from_payload()` and serializer `to_payload()` (lines 46-70)
- `RuntimeConfig` - configuration container (slots=True)
- `CommandSpec` - command metadata (frozen=True, slots=True) for immutability

**Initialization:**
- `parse_args()` handles CLI argument parsing and environment variable loading (lines 157-171)
- `run()` orchestrates: parse → config → command dispatch → emit output (lines 1073-1093)
- `main()` is entry point that catches exceptions and exits (lines 1096-1106)

---

*Convention analysis: 2026-02-26*
