# claude-template

A ready-to-use workspace for document-heavy legal work with Claude and Codex.

Drop your matter files into a copy of this repo and you get, out of the box:

- **Automatic conversion** of Word docs, PDFs, and emails into plain-text Markdown that the AI can read and search.
- **Word Track Changes editing** through the `/redline` skill — the AI proposes edits, you get a redlined `.docx` you can open in Word.
- **Side-by-side comparison** of two Word documents into a single track-changes `.docx` with `/compare`.
- **Signature-block checking** across a set of agreements with `/sigcheck`.
- **Connections** to caption.fyi (meeting transcripts) and NetDocs (document management) when you provide credentials.

You do not need to be a programmer to use this. If you can copy and paste a few lines into a terminal, you can set it up. This README walks you through every step.

---

## What you need first

- **`uv`** — a small tool that manages Python and this workspace's software for you. The setup steps below install it.
- **Python 3.13** — `uv` will install the correct version automatically if you don't have it.

> **One rule to remember:** in this repo, never type plain `python`. Always run things through `uv run ...` (for example, `uv run startup.py`). `uv run` makes sure the right environment is used. The AI assistant follows this rule too.

Pick your operating system below and follow that section.

---

## Setup on macOS / Linux

After you have GitHub access and have cloned (downloaded) the repo:

**1. Open Terminal and go into the repo folder:**

```sh
cd claude-template
```

**2. Install the repo's software:**

```sh
uv sync
```

`uv sync` installs everything the workspace needs, including:

- **`redline`** — `adeu`, the tool that edits Word documents as Track Changes. (Sourced from the slim `vela-wood/adeu` build, which keeps the editing tool but drops software it doesn't need.)
- **`compare`** — `python-redlines`, which turns two Word files into one track-changes comparison document.
- **pandas / openpyxl** — for reading and analyzing spreadsheets (`.csv`, `.xlsx`).

These are installed on **every** platform — none of them are Windows-only or Mac-only.

**3. Install the Caption tool** (for meeting/transcript features):

```sh
uv tool install --force --python 3.13 "caption-cli @ git+https://github.com/sec-chair/caption-cli.git"
```

If your terminal says `caption` can't be found afterward, run this once and reopen the terminal:

```sh
uv tool update-shell
```

**4. (Optional) Connect Caption and NetDocs credentials:**

```sh
uv run setup_claude.py
```

This opens the Caption sign-in flow, saves the selected organization's credentials into a local `.env` file, and skips anything already saved. It does not install software.

**5. (Optional) Check that your Caption credentials work:**

```sh
uv run setup_test.py
```

You're done. Skip ahead to [Everyday use](#everyday-use).

---

## Setup on Windows

On Windows, everything lives on the **main** branch — there is no longer a separate `windows` branch to switch to. A guided PowerShell installer does the work for you.

**1. Open PowerShell** and go into the repo folder:

```powershell
cd claude-template
```

**2. Run the installer:**

```powershell
.\setup_windows.ps1
```

The installer is **interactive and cautious**: it shows you each command and its purpose, then waits for you to type `Y` before running it. It walks through, in order:

1. `git pull` — get the latest version of the repo.
2. Installing `uv`.
3. `uv sync` — install the workspace's software (same tools listed in the macOS/Linux section above).
4. Installing the Caption tool.

If PowerShell says `uv` can't be found right after it's installed, close and reopen PowerShell (or run `uv tool update-shell`), then run the installer again — it will skip the steps that are already done.

**3. (Optional) Connect Caption and NetDocs credentials:**

```powershell
uv run setup_claude.py
```

That's it — the same workspace, no branch switching required.

---

## Everyday use

**At the start of each task, run the startup script:**

```sh
uv run startup.py
```

`startup.py` prepares your files. It:

- converts `.pdf`, `.docx`, and supported email files into matching `.md` (Markdown) files next to them — for example, `contract.pdf` becomes `contract.pdf.md`;
- keeps `.hash_index.csv` so it knows which source files have changed;
- keeps `.token_index.csv` with the size (token count) of each converted file;
- creates and clears the `caption_cache/` folder for Caption output;
- tells you whether NetDocs access looks configured;
- flags any PDFs that look like scanned images and may need **OCR** (text recognition).

> **About OCR:** scanned PDFs have no selectable text, so they need OCR before the AI can read them. OCR can be slow, so it does **not** run automatically. If `startup.py` reports PDFs that need it, ask before running `uv run startup.py --ocr`.

**When reading documents,** prefer the converted `.md` file over the original — for example, open `contract.pdf.md` rather than `contract.pdf`. The Markdown version is what the AI reads and searches.

**For spreadsheets** (`.csv`, `.xlsx`), the AI uses pandas through `uv run`.

**For Word edits,** only the `/redline` skill is used. **For a human-readable comparison document** from two existing `.docx` files, use `/compare`.

---

## How the AI is instructed

The assistant's behavior is defined in a few files at the repo root:

- **`AGENTS.md`** — the primary instructions (role, rules, workflow).
- **`CLAUDE.md`** — a small file that points Claude at `AGENTS.md` and `USERPREFS.md`.
- **`USERPREFS.md`** — your personal preferences (kept local, not shared).

---

## Available skills

These are the built-in `/` commands the assistant can use. They live under `.claude/skills/`.

### `/redline` — edit Word documents

The **only** approved way to edit Word documents in this repo. Use it to:

- preview structured edits before applying them;
- apply redlines and comments back into a `.docx` as Word Track Changes;
- inspect how two `.docx` files differ, as text or JSON.

Notes:

- `adeu diff` produces text or JSON only. When you want a comparison document a person can open in Word, use `/compare` instead.
- It won't create a brand-new blank Word file from scratch.
- Output is saved as a **new** file; the original is left untouched unless you explicitly ask to overwrite it.
- `adeu` comes from the slim `vela-wood/adeu` build declared in `pyproject.toml`.

### `/compare` — comparison document from two Word files

When you have two existing `.docx` files and want a third `.docx` showing the second file's changes as Word Track Changes:

```sh
.claude/skills/compare/scripts/run_compare.sh original.docx modified.docx
.claude/skills/compare/scripts/run_compare.sh original.docx modified.docx -o comparison.docx --author "A. Lin"
```

- Both inputs must be `.docx`.
- Order matters: original first, modified second.
- It writes a new output file and refuses to overwrite either input.
- Powered by `python-redlines`, installed by the default `compare` dependency group.

### `/sigcheck` — check signature blocks

Verifies the spelling and consistency of signature blocks across a set of agreements. Use it to compare signatories across documents, confirm names/titles/addresses, or build a signatory table for a deal.

### `/caption` — meeting transcripts

For Caption transcript and workspace tasks: searching transcripts, listing projects and folders, creating or editing Caption projects/folders, and downloading transcript text.

### `/journal` — matter journal

Reads from and saves notes to the matter journal — useful for capturing decisions, corrections, and standing preferences over the life of a matter.

### `/share` — share a session

Shares the current Claude Code session through the AgentsView cloud service so a colleague can review it.

---

## NetDocs

NetDocs (document management) is only used when both are true:

1. you ask for it, and
2. `startup.py` reported that NetDocs access is configured.

The assistant reaches NetDocs through `uv run nd.py` with specific options — never the bare interactive interface.
