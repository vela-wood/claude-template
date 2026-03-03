# Codebase Structure

**Analysis Date:** 2026-02-26

## Directory Layout

```
caption-cli/
├── caption.py           # Main application (monolithic, 1110 lines)
├── pyproject.toml       # Project metadata and dependencies
├── uv.lock              # Dependency lock file (uv package manager)
├── tests/
│   └── test_caption.py  # Comprehensive test suite
├── .env                 # Environment configuration (not tracked)
├── search-token.json    # Cached Meilisearch token (generated)
├── .gitignore           # Git ignore patterns
├── .pytest_cache/       # Pytest cache (generated)
├── .venv/               # Virtual environment (generated)
├── openapi.json         # API specification (398KB)
├── PLAN.md              # Implementation plan notes
└── README.md            # Project documentation
```

## Directory Purposes

**Root Level:**
- Purpose: Project configuration and main application
- Contains: Single-file Python app, build config, environment files
- Key files: `caption.py` is the entire application

**tests/**
- Purpose: Test suite with comprehensive coverage
- Contains: Unit tests, integration tests, CLI behavior tests
- Key files: `test_caption.py` (1194 lines) with 50+ test cases

**Generated Directories (not committed):**
- `.venv/`: Python virtual environment
- `.pytest_cache/`: Pytest cache
- `__pycache__/`: Python bytecode
- `.osgrep/`: OSGrepCache output directory

## Key File Locations

**Entry Points:**
- `caption.py`: Single entry point via `if __name__ == "__main__": main()`
- `pyproject.toml`: Defines console script `caption = "caption:main"`

**Configuration:**
- `pyproject.toml`: Project name, version, dependencies, build backend
- `.env`: Runtime environment variables (CAPTION_API_URL, CAPTION_TOKEN, CAPTION_MEILI_URL)

**Core Logic:**
- `caption.py`: All business logic in single file organized by layer:
  - Lines 1-38: Constants and defaults
  - Lines 40-91: Data classes (CliError, SearchToken, RuntimeConfig, CommandSpec)
  - Lines 95-172: Argument parsing infrastructure
  - Lines 174-247: API integration layer (HTTP clients)
  - Lines 261-284: API helpers (workspace, pagination)
  - Lines 286-350: Authentication and caching
  - Lines 363-388: Retry logic
  - Lines 390-507: Output formatting and transformation
  - Lines 509-821: Command implementations
  - Lines 824-961: Command handlers and specifications
  - Lines 1073-1106: Application entry point and dispatcher

**Testing:**
- `tests/test_caption.py`: All tests (parametrized fixtures, mocking, integration patterns)

## Naming Conventions

**Files:**
- Lowercase with underscores: `caption.py`, `test_caption.py`
- Constants in UPPERCASE: `DEFAULT_CACHE_PATH`, `WORKSPACE_LIST_PAGE_SIZE`
- No file extensions except `.py` and `.json`

**Directories:**
- Lowercase plural: `tests/`
- Special prefixes: `.` for hidden config (`.env`, `.git`, `.venv`)

**Functions:**
- Public command functions: `command_*` (e.g., `command_search`, `command_create_project`)
- Private helpers: `_function_name` prefix (e.g., `_authorized_request`, `_field_view`)
- Command handlers: `_handle_*` (e.g., `_handle_search`)
- Argument builders: `_add_*_arguments` (e.g., `_add_search_arguments`)
- Format converters: `_*_to_*` (e.g., `_transcript_items_to_md`)

**Variables:**
- Configuration: `api_url`, `api_token`, `meili_url`, `cache_path`
- Data payloads: `payload`, `body`, `json_body`
- Lists/collections: `items`, `captions`, `lines`, `specs`
- Booleans for state: `is_error`, `needs_meili`, `has_*`

**Types/Classes:**
- Dataclasses: `CliError`, `SearchToken`, `RuntimeConfig`, `CommandSpec`
- PascalCase for all classes (Python convention)

## Where to Add New Code

**New Command:**

1. **Command Implementation** (`caption.py`):
   - Add function `command_new_feature(config: RuntimeConfig, ...) -> dict[str, Any]`
   - Location: Insert near other `command_*` functions (around line 509-821)
   - Example pattern:
     ```python
     def command_my_feature(config: RuntimeConfig, *, param: str) -> dict[str, Any]:
         api_url = _require_api_url(config)
         api_token = _require_api_token(config)
         # Call API and return dict
         return result
     ```

2. **Argument Builder** (`caption.py`):
   - Add function `_add_my_feature_arguments(parser: argparse.ArgumentParser) -> None`
   - Location: Near other `_add_*_arguments` functions (around line 824-898)
   - Register positional and optional arguments on the parser

3. **Command Handler** (`caption.py`):
   - Add function `_handle_my_feature(config: RuntimeConfig, args: argparse.Namespace) -> dict[str, Any]`
   - Location: Near other `_handle_*` functions (around line 900-961)
   - Extract args and call corresponding command function

4. **Command Specification** (`caption.py`):
   - Add `CommandSpec` to tuple returned by `_command_specs()` (around line 963-1070)
   - Example:
     ```python
     CommandSpec(
         name="my_feature",
         help="Description of the command",
         add_arguments=_add_my_feature_arguments,
         handler=_handle_my_feature,
         needs_meili=False,  # or True if requires Meilisearch
         default_output="json",
         usage="caption my_feature <arg> [--option VALUE]",
         notes=("Important note 1", "Note 2"),
         example="caption my_feature example-value",
     )
     ```

5. **Tests** (`tests/test_caption.py`):
   - Add test functions: `test_my_feature_*`
   - Use existing `config` fixture for RuntimeConfig
   - Mock API calls with monkeypatch fixture
   - Location: Add at end of file before final test

**New Utility Function:**

- Private utilities: Add `_helper_name()` near related functions
- Input validation: Add `_validate_*()` or extend `_build_*()` helpers
- Output formatting: Add `_format_*()` or extend `_render_*()` functions

**New API Client Method:**

- Add function like `_authorized_post()` or `_authorized_patch()`
- Location: Near other `_authorized_*` functions (around line 190-247)
- Pattern: Wrap `_authorized_request()` with specific HTTP method

## Special Directories

**tests/**
- Purpose: Unit and integration tests
- Generated: No
- Committed: Yes
- Contains: Single test module with 1194 lines, 50+ test cases covering:
  - Argument parsing
  - Command behavior
  - API integration
  - Error handling
  - Output formatting
  - Token caching and refresh

**.planning/codebase/**
- Purpose: Architecture and structure documentation
- Generated: No (manually maintained)
- Committed: Yes
- Contains: ARCHITECTURE.md, STRUCTURE.md, CONVENTIONS.md, TESTING.md

**.env**
- Purpose: Local environment variables
- Generated: No (created manually)
- Committed: No (in .gitignore)
- Contains: CAPTION_API_URL, CAPTION_TOKEN, CAPTION_MEILI_URL

**search-token.json**
- Purpose: Cached Meilisearch token and URL
- Generated: Yes (by `command_token()` or `_require_cached_or_fresh_token()`)
- Committed: No (in .gitignore)
- Format: JSON with fields: `token`, `url`, `expiresAt`

**.venv/**
- Purpose: Python virtual environment
- Generated: Yes (by `uv venv` or `python -m venv`)
- Committed: No
- Contains: Python interpreter and installed packages

**.pytest_cache/**
- Purpose: Pytest metadata and cache
- Generated: Yes (during test runs)
- Committed: No

## Code Organization Pattern

The application follows a **single-file modular architecture** with clear logical sections:

1. **Imports & Constants** (lines 1-38)
   - External imports at top
   - Internal constants (defaults, field lists)

2. **Data Classes** (lines 40-91)
   - Domain objects and configuration
   - Ordered by usage dependency

3. **Utility Functions** (lines 95-507)
   - Generic helpers used across multiple commands
   - Organized by concern (parsing, auth, formatting, API)
   - Private functions prefixed with `_`

4. **Command Logic** (lines 509-821)
   - Business logic for each command
   - Named `command_*`
   - Accept `RuntimeConfig` + command-specific parameters

5. **CLI Infrastructure** (lines 824-1093)
   - Argument builders: `_add_*_arguments`
   - Command handlers: `_handle_*` (adapters between CLI args and commands)
   - Command specs: Registry of all available commands
   - Entry points: `run()`, `main()`

**Why single file?**
- Clear dependencies (no circular imports)
- Easy to understand full application scope
- Simple deployment (single file to distribute)
- Testable via import of single module
- ~1100 lines keeps it readable with good organization

**Modularity achieved through:**
- Function decomposition (command function = logic, handler function = CLI binding)
- Data classes for configuration injection
- Separation of concerns (API, auth, formatting, validation layers)
- Clear naming conventions (prefixes indicate scope/purpose)
