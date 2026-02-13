# Claude Code Legal Template

This template contains (1) an AGENTS.md file, (2) a variety of python scripts, and (3) a claude skill that have been optimized to make the best use of Claude's 200k (or 1m) context window.

## Claude 4.6 updates

1. The old `main.py` flow was removed and replaced with a `startup.py`-first workflow.
2. `startup.py` was updated for better file handling and token indexing.
3. DOCX editing was standardized on `/superdocs-redlines` (vendored skill lives at `.claude/skills/superdoc-redlines`).
4. The `superdoc-redlines` integration was moved from submodule to vendored source in-repo.

## Current repo layout

1. `AGENTS.md`: primary behavior spec (startup procedure, tooling rules, journaling protocol).
2. `startup.py`: converts `.pdf/.docx/.eml/.msg` inputs to markdown, updates `.hash_index.csv` and `.token_index.csv`, and surfaces optional features.
3. `nd.py` + `netdocs/`: NetDocs CLI/TUI integration.
4. `tools/remove_artifacts.py`: cleans PDF markdown artifacts via API when configured.
5. `count_tokens.py` and `tools/count_tokens.py`: token-count helper.
6. `.claude/skills/superdoc-redlines/`: see below

## Prerequisites

1. `uv` installed; run Python tasks with `uv run ...`.
2. Node.js available for the vendored SuperDoc redlining CLI.
3. Optional environment variables for integrations:
   - NetDocs: `MATTERS_DB`, `ND_API_KEY`, `NDHELPER_URL`
   - Artifact cleaning: `ARTIFACT_API_TOKEN`, `ARTIFACT_URL`

## Typical use

1. Put matter files in this workspace.
2. Run `uv run startup.py` to ensure everything works.
3. Launch Claude Code.
4. For DOCX editing/redlines, make sure to use `/superdocs-redlines` via the vendored skill.

## Attribution

This repo contains a fork of **superdoc-redlines** from [yuch85/superdoc-redlines](https://github.com/yuch85/superdoc-redlines/). Check out his [other repos](https://github.com/yuch85/) as well; he is single-handledly pushing the boundaries of legaltech and AI.
