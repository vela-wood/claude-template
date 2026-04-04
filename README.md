# Claude Code Legal Template

This template contains (1) an AGENTS.md file, (2) Python scripts for file conversion and workflow support, and (3) Claude skills wired to the repo-local environment.

## Current repo layout

1. `AGENTS.md`: primary behavior spec (startup procedure, tooling rules, journaling protocol).
2. `startup.py`: converts `.pdf/.docx/.eml/.msg` inputs to markdown, updates `.hash_index.csv` and `.token_index.csv`, and surfaces optional features.
3. `nd.py` + `netdocs/`: NetDocs CLI/TUI integration.
4. `tools/remove_artifacts.py`: cleans PDF markdown artifacts via API when configured.
5. `.claude/skills/redline/`: Adeu-backed DOCX redlining skill using the root repo environment.

## Prerequisites

1. `uv` installed.
2. Python 3.14 available for the root repo environment.
3. Run `uv sync` at the repo root so the local `.venv` contains all CLI dependencies, including Adeu.
4. Optional environment variables for integrations:
   - NetDocs: `MATTERS_DB`, `ND_API_KEY`, `NDHELPER_URL`
   - Artifact cleaning: `ARTIFACT_API_TOKEN`, `ARTIFACT_URL`

## Typical use

1. Put matter files in this workspace.
2. Run `uv run startup.py` to ensure indexing/conversion works.
3. Run `uv sync` after pulling dependency changes.
4. Launch Claude Code.
5. For DOCX editing/redlines, use `/redline`.

## Notes

- Adeu is installed into the root repo environment from `https://github.com/dealfluence/adeu.git`; this repo no longer vendors the old SuperDoc redlining package.
