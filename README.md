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

Then run the interactive PowerShell installer:

```powershell
.\setup_windows.ps1
```

The installer confirms each step before it runs it: `git pull`, `uv` installation, `uv sync`, and Caption CLI installation.

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

For tabular data, use pandas through `uv run`. For Word document edits, use only `/redline`.

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

### `/redline`

Use this for Word document editing and comparisons:

- compare two `.docx` files
- preview structured edits
- apply redlines/comments back into a `.docx`

Important constraints:

- this is the only approved way to edit Word documents in this repo
- do not use it to create a brand-new blank Word file from scratch
- keep output as a new redlined file unless you explicitly want the original overwritten

### `/share`

Use this to share the current Claude Code session through Caption:

```sh
caption sync --session-id $CLAUDE_CODE_SESSION_ID
```