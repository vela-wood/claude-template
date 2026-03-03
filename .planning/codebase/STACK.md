# Technology Stack

**Analysis Date:** 2026-02-26

## Languages

**Primary:**
- Python 3.12+ - Single-file CLI application (`caption.py`)

## Runtime

**Environment:**
- Python 3.12 minimum required (`requires-python = ">=3.12"` in `pyproject.toml`)

**Package Manager:**
- uv (modern Python package manager)
- Lockfile: `uv.lock` present

## Frameworks

**Core:**
- argparse (Python standard library) - CLI argument parsing

**HTTP Client:**
- httpx 0.28.1+ - HTTP requests with Bearer token authentication and timeout support (15s)

**Search:**
- meilisearch 0.40.0+ - Search index client for querying Meilisearch

**Configuration:**
- python-dotenv 1.1.1+ - Environment variable loading from `.env` file

## Key Dependencies

**Critical:**
- httpx 0.28.1+ - Why it matters: HTTP client for all API calls to Caption API and Meilisearch endpoints
- meilisearch 0.40.0+ - Why it matters: Meilisearch client library for search operations with token-based authentication
- python-dotenv 1.1.1+ - Why it matters: Loads environment configuration from `.env` file (API URLs, tokens, cache path)

**Optional Transitive:**
- anyio 4.12.1+ - Async backend for httpx
- certifi - SSL/TLS certificate validation for httpx
- charset-normalizer - Text encoding normalization for httpx
- idna - International domain name support
- h11 - HTTP/1.1 protocol implementation (httpx dependency)

## Build System

**Package Build:**
- hatchling - Build backend for wheel distribution
- Build target: `wheel` with `caption.py` included as main module

**CLI Entry Point:**
- Script: `caption` → `caption:main` function in `caption.py` line 1096

## Configuration

**Environment-first configuration:**

Required variables:
- `CAPTION_API_URL` - Base URL for Caption API (required for all commands)
- `CAPTION_TOKEN` - Bearer token for Caption API authentication (required for authenticated API calls)
- `CAPTION_MEILI_URL` - Meilisearch instance URL (required for `token` and `search` commands only)

Optional variables:
- `CAPTION_MEILI_CACHE` - Override default cache path (`search-token.json`)

**Dotenv loading:**
- Default file: `.env` (loaded automatically before command execution)
- Can override with `--env-file` CLI argument
- Loading occurs in `parse_args()` before main argument parsing (line 162)

**CLI configuration options:**
- `--cache-path PATH` - Search token cache location (defaults to `CAPTION_MEILI_CACHE` env var or `search-token.json`)
- `--output {json,table,md}` - Output format (defaults to `json` except `dl_transcript` uses `md`)
- `--env-file PATH` - Dotenv file to load (default: `.env`)

## Platform Requirements

**Development:**
- Python 3.12+
- uv package manager
- No OS-specific dependencies

**Production:**
- Python 3.12+
- Network access to Caption API endpoint (`CAPTION_API_URL`)
- Network access to Meilisearch endpoint (`CAPTION_MEILI_URL`)
- Read/write access to cache file directory (default: current working directory for `search-token.json`)

## Timeout Configuration

**HTTP operations:**
- All httpx requests use 15-second timeout (line 177, 201, 234)
- Applies to: API calls, token fetches, search requests

## Version & Distribution

**Current Version:** 0.1.0
**Package Name:** caption-cli

---

*Stack analysis: 2026-02-26*
