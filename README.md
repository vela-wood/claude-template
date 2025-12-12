## 1. Overview

This repo is a minimal harness around `AGENTS.md` for running LLM-based legal workflows. The real “logic” of the project lives in the agent instructions, not in complex code.

## 2. Repo Contents

1. `AGENTS.md` – primary operational spec for how the agent must work (startup procedure, tooling rules, journaling).
2. `main.py` – trivial entrypoint (`uv run python main.py`) used mainly to verify that the environment is wired correctly.
3. `count_tokens.py` – helper for counting tokens in markdown or other text files via `uv`.
4. `pyproject.toml` / `uv.lock` – `uv` project configuration and lockfile.

## 3. Prerequisites

You are expected to have at least:

1. **Python environment via `uv`**
   - Install `uv` (see the official docs) and run all Python commands through it.
   - Example: `uv run python main.py`
2. **`osgrep`**
   - CLI search tool the agent relies on (alongside `rg`, `find`, etc.) for file discovery.
   - https://github.com/Ryandonofrio3/osgrep
3. **`cass`**
   - Chat-history index/search tool used to query prior agent sessions.
   - https://github.com/Dicklesworthstone/coding_agent_session_search
   
If `uv run` fails because of local sandboxing or permission issues, fix your environment first; otherwise the agent will not behave as specified in `AGENTS.md`.

## 4. Typical Workflow

1. Place your matter-specific files in this directory (or subdirectories).
2. Ensure `AGENTS.md` reflects the constraints and tools you actually want the agent to obey.
3. Confirm `osgrep` and `cass` are on your `PATH` and working (e.g., `cass health`).
4. Run whatever agent harness you are using (e.g., Codex CLI) with this directory as `$PWD` so `AGENTS.md` governs behavior.
5. Use `uv run python main.py` only as a sanity check that Python + `uv` are installed; the core interaction is via the agent, not this script.

## 5. Journaling and History

The agent is expected to:

1. Write journal entries into `~/legal/_journal/` with filenames like `{matter_name}_{yyyymmdd}_{taskdescription}.md`.
2. Use `cass` with `--workspace {matter_name} --robot` to search prior chats when you explicitly authorize it.

If you don’t want this journaling behavior or history search, change `AGENTS.md`; otherwise, assume the agent will follow it exactly.
