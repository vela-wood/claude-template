# claude-template

Template workspace for document-heavy legal work with Claude/Codex. It is set up to:

- convert source documents to markdown and maintain token indexes
- edit Word documents through Track Changes using the `/redline` skill
- connect to caption.fyi and NetDocs when credentials are configured

## Requirements

- `uv`
- Python `3.13`

Do not rely on bare `python` from the command line in this repo. Use `uv run ...`.

## Setup

After you get GitHub access and clone the repo:

1. Open Terminal and go into the repo:

```sh
cd claude-template
```

2. Install repo dependencies:

```sh
uv sync
```

`uv sync` installs the default dependency groups:

- `redline`: `adeu`, sourced from the slim `vela-wood/adeu` Git tag that keeps the DOCX editing CLI but drops unused MCP-server packages.
- `compare`: `python-redlines[docxodus]`, used to produce Word-native track-changes comparison documents from two existing `.docx` files.

These dependency groups are not Windows-only. They are part of the normal repo environment for every platform.

3. Install the Caption CLI tool:

```sh
uv tool install --force --python 3.13 "caption-cli @ git+https://github.com/sec-chair/caption-cli.git"
```

If `caption` is not found after install, run:

```sh
uv tool update-shell
```

4. Optionally configure Caption and NetDocs credentials:

```sh
uv run setup_claude.py
```

`setup_claude.py` opens the Caption setup flow, loads the selected organization's credentials, appends new values to `.env`, and skips existing matching values. It does not install tools.

5. Verify Caption credential setup when needed:

```sh
uv run setup_test.py
```

## Windows Setup

Use the `windows` branch on Windows:

```sh
git checkout windows
```

Then run the installer from that branch:

```powershell
.\setup_windows.ps1
```

That script lives on the `windows` branch. It handles the Windows setup flow there, including dependency setup.

## Basic Usage

Run the startup script at the beginning of each task:

```sh
uv run startup.py
```

`startup.py`:

- converts `.pdf`, `.docx`, and supported email files to adjacent `.md` files
- maintains `.hash_index.csv` for source-file change detection
- maintains `.token_index.csv` for converted-file token counts
- creates and clears `caption_cache/` for Caption command outputs
- prints whether NetDocs access appears configured

Prefer the converted markdown file over the original binary document when reading or searching document text. For example, read `contract.pdf.md` instead of `contract.pdf`.

For tabular data, use pandas through `uv run`. For Word document edits, use only `/redline`. For a Word-native comparison document from two existing `.docx` files, use `/compare`.

## Agent Instructions

The repo keeps agent behavior in:

- `AGENTS.md`: primary Codex-style instructions
- `CLAUDE.md`: Claude include file that points to `AGENTS.md` and `USERPREFS.md`
- `USERPREFS.md`: local preference include

## Available Skills

This repo provides Claude skills under `.claude/skills/`:

### `/caption`

Use this for Caption transcript and workspace tasks, including:

- searching transcripts
- listing projects and folders
- creating or editing Caption projects/folders
- downloading transcript text

### `/compare`

Use this when you have two existing `.docx` files and need a third `.docx` showing the modified file's changes as Word track changes:

```sh
.claude/skills/compare/scripts/run_compare.sh original.docx modified.docx
.claude/skills/compare/scripts/run_compare.sh original.docx modified.docx -o comparison.docx --author "A. Lin"
```

Constraints:

- both inputs must be `.docx`
- argument order is original first, modified second
- the wrapper writes a new output file and refuses to overwrite either input
- this workflow is powered by `python-redlines[docxodus]`, installed by the default `compare` dependency group

### `/redline`

Use this for Word document editing and machine-readable document diffs:

- preview structured edits
- apply redlines/comments back into a `.docx`
- inspect `.docx` differences as text or JSON

Important constraints:

- this is the only approved way to edit Word documents in this repo
- `adeu diff` outputs text or JSON only; use `/compare` when you need a human-readable track-changes `.docx`
- do not use it to create a brand-new blank Word file from scratch
- keep output as a new redlined file unless you explicitly want the original overwritten
- `adeu` is sourced from the slim `vela-wood/adeu` Git tag declared in `pyproject.toml`

### `/share`

Use this to share the current Claude Code session through Caption:

```sh
caption sync --session-id $CLAUDE_CODE_SESSION_ID
```
