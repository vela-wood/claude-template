# Testing Patterns

**Analysis Date:** 2026-02-26

## Test Framework

**Runner:**
- pytest 9.0.2+ (from `pyproject.toml` line 21)
- Config: No explicit config file (`pytest.ini`, `pyproject.toml` test section, or `setup.cfg`) - uses defaults
- Python 3.12+ (from `pyproject.toml` requires-python = ">=3.12")

**Assertion Library:**
- pytest assertions (`assert` statements)
- No external assertion libraries (e.g., `assertpy`, `hamcrest`)

**Run Commands:**
```bash
pytest                          # Run all tests in tests/ directory
pytest tests/test_caption.py    # Run single test file
pytest tests/test_caption.py::test_token_command_fetches_and_caches_credentials  # Run specific test
pytest -v                       # Verbose output
pytest -k "search"              # Run tests matching pattern
```

## Test File Organization

**Location:**
- `tests/` directory at project root (colocated, not in source directory)
- Single test file: `tests/test_caption.py` (1194 lines)

**Naming:**
- Test file: `test_caption.py` (matches `caption.py` module)
- Test functions: `test_*` prefix (pytest convention) (lines 43, 64, 73, etc.)
- Test classes: None used - functions preferred

**Structure:**
```
tests/
├── __pycache__/
├── test_caption.py          # 1194 lines of tests
└── __init__.py              # Not present - not required by pytest
```

## Test Structure

**Suite Organization:**
```python
# Module-level fixtures
@pytest.fixture
def config(tmp_path: Path) -> caption.RuntimeConfig:
    return caption.RuntimeConfig(...)

# Helper functions (not tests)
def write_cache(path: Path, token: str = "cached-token", url: str = "https://cached.meili") -> None:
    path.write_text(json.dumps({"token": token, "url": url}), encoding="utf-8")

def set_runtime_env(monkeypatch: pytest.MonkeyPatch, *, meili_url: str | None = "https://configured.meili") -> None:
    monkeypatch.setenv("CAPTION_API_URL", "http://localhost:8000")
    ...

# Test functions
def test_token_command_fetches_and_caches_credentials(monkeypatch: pytest.MonkeyPatch, config: caption.RuntimeConfig) -> None:
    expected = caption.SearchToken(...)
    monkeypatch.setattr(caption, "fetch_search_token", lambda api_url, api_token: expected)
    result = caption.command_token(config)
    assert result["token"] == "meili-token"
    ...
```

**Patterns:**
- Setup: Fixtures for common test data (lines 19-27)
- Helper functions for repeated setup: `write_cache()`, `set_runtime_env()` (lines 30-40)
- Monkeypatch for isolation: Replace module functions and environment variables
- Teardown: Implicit via fixtures and file cleanup (tmp_path fixtures auto-cleaned by pytest)

**Assertion Pattern:**
```python
def test_search_command_defaults_to_captions_index_and_limit() -> None:
    args = caption.parse_args(["search", "term"])

    assert args.command == "search"
    assert args.query == "term"
    assert args.index == caption.DEFAULT_SEARCH_INDEX
    assert args.limit == caption.DEFAULT_LIMIT == 5
```

## Mocking

**Framework:** pytest `monkeypatch` fixture (built-in, no additional packages needed)

**Patterns:**

1. **Function Replacement:**
```python
def test_invalid_meili_token_refreshes_once_and_retries(
    monkeypatch: pytest.MonkeyPatch,
    config: caption.RuntimeConfig,
) -> None:
    # ... setup fake classes and functions ...

    def fake_build_client(url: str, token: str):
        built_clients.append((url, token))
        if token == "stale-token":
            return FirstClient()
        return SecondClient()

    def fake_fetch_search_token(api_url: str, api_token: str) -> caption.SearchToken:
        fetch_calls.append((api_url, api_token))
        return caption.SearchToken(token="fresh-token", url="https://new.meili")

    monkeypatch.setattr(caption, "build_meili_client", fake_build_client)
    monkeypatch.setattr(caption, "fetch_search_token", fake_fetch_search_token)
```

2. **Environment Variables:**
```python
def set_runtime_env(monkeypatch: pytest.MonkeyPatch, *, meili_url: str | None = "https://configured.meili") -> None:
    monkeypatch.setenv("CAPTION_API_URL", "http://localhost:8000")
    monkeypatch.setenv("CAPTION_TOKEN", "api-token")
    if meili_url is None:
        monkeypatch.delenv("CAPTION_MEILI_URL", raising=False)
    else:
        monkeypatch.setenv("CAPTION_MEILI_URL", meili_url)
```

3. **Exception Simulation:**
```python
class FakeAuthError(Exception):
    def __init__(self) -> None:
        self.status_code = 401
        self.code = "invalid_api_key"
        self.message = "invalid_api_key"
        super().__init__(self.message)

# In test:
class FirstIndex:
    def search(self, query, opt_params=None):
        raise FakeAuthError()
```

4. **Call Tracking:**
```python
index_calls: list[str] = []
search_calls: list[tuple[str, dict[str, int]]] = []

class FakeClient:
    def index(self, index_uid):
        index_calls.append(index_uid)
        return FakeIndex()

# After test:
assert index_calls == ["projects_v1"]
assert search_calls == [("roadmap", {"limit": 7})]
```

**What to Mock:**
- External HTTP clients: `httpx.Client` replaced with fakes (test can't make real network calls)
- Meilisearch client: `build_meili_client()` returns mock (line 229, 282, 316)
- API functions: `fetch_search_token()`, `fetch_current_workspace_id()`, `fetch_workspace_items_page()` (lines 50, 282, 403, 404, 547-548)
- Output functions: `emit_output()` captured instead of printed (line 324)

**What NOT to Mock:**
- Pure parsing functions: `parse_args()` called directly to test argument handling (line 65, 73, 92)
- Data models: `SearchToken.from_payload()` tested directly (line 53)
- Validation helpers: `_require_api_url()` called directly to test validation (implicitly in commands)
- Output rendering: `_transcript_items_to_md()` tested directly without mocking (line 842)

## Fixtures and Factories

**Test Data:**

1. **Config Fixture:**
```python
@pytest.fixture
def config(tmp_path: Path) -> caption.RuntimeConfig:
    return caption.RuntimeConfig(
        api_url="http://localhost:8000",
        api_token="api-token",
        meili_url="https://configured.meili",
        cache_path=tmp_path / "search-token.json",
        output="json",
    )
```

2. **Cache Helper:**
```python
def write_cache(path: Path, token: str = "cached-token", url: str = "https://cached.meili") -> None:
    path.write_text(json.dumps({"token": token, "url": url}), encoding="utf-8")
```

3. **Environment Setup Helper:**
```python
def set_runtime_env(monkeypatch: pytest.MonkeyPatch, *, meili_url: str | None = "https://configured.meili") -> None:
    monkeypatch.setenv("CAPTION_API_URL", "http://localhost:8000")
    monkeypatch.setenv("CAPTION_TOKEN", "api-token")
    if meili_url is None:
        monkeypatch.delenv("CAPTION_MEILI_URL", raising=False)
    else:
        monkeypatch.setenv("CAPTION_MEILI_URL", meili_url)
```

4. **Inline Fake Data (tests construct context-specific payloads):**
```python
def fake_fetch_workspace_items_page(...) -> dict[str, object]:
    if page == 0:
        return {
            "items": [
                {
                    "id": "p1",
                    "createdAt": "2023-12-31T00:00:00Z",
                    "updatedAt": "2024-01-01T00:00:00Z",
                    "name": "Alpha",
                    "description": "First",
                    "folder": None,
                    "transcript": "t1",
                    "workspace": "w1",
                    "createdBy": "u1",
                }
            ],
            ...
        }
```

**Location:**
- Module-level fixtures and helpers: top of `tests/test_caption.py` (lines 11-40)
- Test-specific fakes: defined inline within test function (lines 219-230, 349-401)
- No separate fixtures file

## Coverage

**Requirements:** Not enforced (no `--cov-fail-under` or similar configuration)

**View Coverage:**
```bash
pytest --cov=caption --cov-report=html tests/
pytest --cov=caption --cov-report=term-missing tests/
```

**Current Coverage:** Implicit - 1194 lines of tests for 1110 lines of code
- Broad coverage of command handlers (token, search, list_projects, list_folders, create_project, create_folder, edit_project, edit_folder, dl_transcript)
- Edge cases tested: auth token refresh, pagination, argument validation, conflicting flags
- Error cases: missing environment variables, invalid input, API errors

## Test Types

**Unit Tests:**
- **Scope:** Individual functions and command handlers
- **Approach:** Fixtures provide RuntimeConfig, monkeypatch replaces external dependencies
- **Examples:**
  - `test_search_command_defaults_to_captions_index_and_limit()` (line 64) - tests argument parsing defaults
  - `test_command_edit_project_requires_update_fields()` (line 861) - tests validation
  - `test_transcript_items_to_md_merges_consecutive_speaker_lines()` (line 833) - tests rendering logic

**Integration Tests:**
- **Scope:** End-to-end command execution via `run()` function
- **Approach:** Mock only external libraries (httpx, meilisearch), test full pipeline: parse_args → config → command → emit_output
- **Examples:**
  - `test_run_search_uses_default_index_when_flag_omitted()` (line 299) - full search command flow
  - `test_run_create_project_does_not_require_meili_url()` (line 1060) - full create_project flow
  - `test_run_dl_transcript_with_json_output_emits_raw_payload()` (line 1017) - full transcript download flow

**E2E Tests:**
- Not present - no headless CLI invocation or real server integration tests
- Tests do not make real network calls (all external calls are mocked)

## Common Patterns

**Async Testing:**
Not applicable - codebase is synchronous (no `async def` or `await`)

**Error Testing:**
```python
def test_search_command_rejects_limit_lt_1() -> None:
    with pytest.raises(caption.CliError, match="--limit must be >= 1"):
        caption.parse_args(["search", "term", "--limit", "0"])

def test_command_search_rejects_empty_index(
    config: caption.RuntimeConfig,
) -> None:
    with pytest.raises(caption.CliError, match="--index cannot be empty"):
        caption.command_search(config, query="term", index="   ", limit=5)

def test_command_edit_project_rejects_conflicting_nullable_flags(config: caption.RuntimeConfig) -> None:
    with pytest.raises(caption.CliError, match="Use either --description or --clear-description"):
        caption.command_edit_project(
            config,
            project_id="project-uuid",
            name=None,
            description="new",
            clear_description=True,
            folder=None,
            clear_folder=False,
        )
```

**Capture & Inspection:**
```python
def test_search_help_lists_supported_indices(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        caption.parse_args(["search", "--help"])

    captured = capsys.readouterr()
    assert "workspace_folders_v1" in captured.out
    assert "projects_v1" in captured.out
    assert "transcript_sessions_v1" in captured.out
```

**Output Verification:**
```python
def test_run_dl_transcript_does_not_require_meili_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    set_runtime_env(monkeypatch, meili_url=None)

    def fake_dl_transcript(...) -> dict[str, object]:
        return {...}

    monkeypatch.setattr(caption, "dl_transcript", fake_dl_transcript)

    exit_code = caption.run([...])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == "[15:01.23] me: hello there\n[15:01.24] meeting-0: hi\n"
```

**Pagination Testing:**
```python
def test_command_list_projects_fetches_workspace_and_all_project_pages(
    monkeypatch: pytest.MonkeyPatch,
    config: caption.RuntimeConfig,
) -> None:
    page_calls: list[tuple[str, str, int, int]] = []

    def fake_fetch_workspace_items_page(..., page: int, limit: int) -> dict[str, object]:
        page_calls.append((workspace_id, endpoint, page, limit))
        if page == 0:
            return {"items": [...], "totalPages": 2}
        return {"items": [...], "totalPages": 2}

    monkeypatch.setattr(caption, "fetch_workspace_items_page", fake_fetch_workspace_items_page)

    result = caption.command_list_projects(config)

    assert page_calls == [
        ("workspace-uuid", "projects", 0, caption.WORKSPACE_LIST_PAGE_SIZE),
        ("workspace-uuid", "projects", 1, caption.WORKSPACE_LIST_PAGE_SIZE),
    ]
```

---

*Testing analysis: 2026-02-26*
