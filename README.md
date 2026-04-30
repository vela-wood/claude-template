# claude-template

Template workspace for document-heavy legal work with Claude/Codex. It is set up to:

- optimize token usage by converting office documents into markdown
- manipulate word documents in track changes via the /redline skill
- connect to other important sources of context (currently, caption.fyi and netdocs)
- don't forget to bring your own first party connectors via mcp

## Non-Technical Setup

After you get GitHub access and clone the repo:

1. Open Terminal and go into the repo:

```sh
cd claude-template
```

2. If you are using Windows, switch to the Windows version of the template:

```sh
git checkout windows
```

The default branch is set up for Mac.

3. Install repo dependencies:

```sh
uv sync
```

4. Install the Caption CLI tool:

```sh
uv tool install --force --python 3.13 "caption-cli @ git+https://github.com/sec-chair/caption-cli.git"
```

If `caption` is not found after install, run:

```sh
uv tool update-shell
```

5. Optionally set up the [Caption](https://app.caption.fyi/) and NetDocs connectors:

```sh
uv run setup_claude.py
```

`setup_claude.py` writes credentials and Claude connector settings. It does not install tools.

## Basic Usage

Laumch claude and start asking questions, you control the context by referencing documents in the folder.

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

## Requirements

- `uv`
- Python `3.13`

Do not rely on bare `python` from the command line in this repo. Use `uv run ...`.
