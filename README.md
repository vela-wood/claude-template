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

The repository already contains real skill files for both Claude and Codex. No symlink
setup is required.

**3. Install the Caption tool** (for meeting/transcript features):

```sh
uv tool install --force --python 3.13 "caption-cli @ git+https://github.com/sec-chair/caption-cli.git"
```

If your terminal says `caption` can't be found afterward, run this once and reopen the terminal:

```sh
uv tool update-shell
```

**4. Run the setup hub:**

```sh
uv run config.py
```

This opens a small interactive setup screen (a "hub") listing three independent tasks, each showing its current state. Run one at a time, or pick **Run everything**:

1. **Caption credentials** — the Caption sign-in flow; saves the selected organization's credentials into a local `.env` file and skips anything already saved.
2. **Sidecar naming** — choose how converted Markdown files are named: visible `contract.docx.md` (the default) or dot-prefixed `.contract.docx.md` (hidden in Finder and plain `ls`). After changing this, the next `uv run startup.py` renames existing converted files to the chosen style automatically. If both styles of the same file somehow exist, startup keeps the one matching your setting and warns you to delete the other — it never deletes either file itself.
3. **Statusline + usage cache** — installs the status bar account-wide: the setup copies this repo's `ccstatus.py` to `~/.claude/hooks/` and points the `statusLine` command in your user-level `~/.claude/settings.json` at it, so it works in every folder, not just this repo. Nothing extra to install — no Node, no internet, just Python. The bar shows context usage, model + effort + token speed, prompt-cache countdown, git branch, 5h usage, and memory; the same script also keeps a small usage cache up to date so the optional usage guard can read your Claude usage. Until you install it, there is no status bar and the usage guard silently does nothing. When a repo update changes `ccstatus.py`, the setup hub shows "an updated status bar is ready to install"; installing also removes any leftover repo-local statusLine that would shadow the account-wide one.

The hub also shows a notice at the top when the repo itself has updates available (it never updates automatically).

**5. (Optional) Check that your Caption credentials work:**

```sh
uv run setup_test.py
```

**Updating:** from time to time, `git pull` and re-run `uv run config.py` — the hub shows what is already configured and only changes what you pick.

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
uv run config.py
```

That's it — the same workspace, no branch switching required.

The checked-in `.agents/skills/` directory contains the Codex copies. They are ordinary
files, so Windows does not need Developer Mode or Git symlink support.

---

## Everyday use

**At the start of each task, run the startup script:**

```sh
uv run startup.py
```

`startup.py` prepares your files. It:

- converts `.pdf`, `.docx`, and supported email files into matching `.md` (Markdown) files next to them — for example, `contract.pdf` becomes `contract.pdf.md` (or `.contract.pdf.md` if you chose the dot-prefixed style in setup; startup migrates existing files between styles automatically and warns — without deleting anything — if both styles of the same file exist);
- keeps `.hash_index.csv` so it knows which source files have changed;
- keeps `.token_index.csv` with the size (token count) of each converted file;
- creates and clears the `caption_cache/` folder for Caption output;
- tells you whether NetDocs access looks configured;
- flags any PDFs that look like scanned images and may need **OCR** (text recognition).

Under the hood, Word documents and PDFs are converted by **AnyDoc** (a fast local converter), and Outlook emails (`.msg`, `.oft`) by **extract-msg**. Supported email formats are `.eml`, `.emlx`, `.msg`, `.oft`, `.mht`, `.mhtml`, and `.mbox`; every email is rendered with the same header block (Subject, From, To, CC, Date), and Outlook messages also list attachment *names* (attachment contents are not converted). For Outlook messages the body is taken from plain text first, then HTML, then RTF; for `.mht`/`.mhtml` saved web pages the HTML content is used first. `.mbx` files are **not** converted — that extension is ambiguous between incompatible mailbox formats — but `startup.py` lists any it finds so you can export them to `.mbox` or `.eml` yourself.

### AnyDoc PDF release

PDFs use the official AnyDoc 0.1.8 wheel release with the `pdf-inspector` fix:

- **The bug.** AnyDoc's `pdf-inspector` dependency (≤0.1.7) pre-strips `%` comments from PDF content streams but ignores backslash escapes inside string literals. An escaped `\)` makes it think the string closed, so a later `%` glyph inside the string is treated as a comment and the rest of the line is **silently deleted** — no error, no OCR flag.
- **Why it hits this practice hard.** macOS Quartz PDFs (Word for Mac → PDF, i.e. most redlines) assign subset-font codes in first-use order, so common letters land on the `%`, `(`, `)` bytes. Worst observed case: ~60% of a redline silently dropped, including arbitration and indemnity provisions.
- **Release.** Official [`firecrawl/anydoc` v0.1.8](https://github.com/firecrawl/anydoc/releases/tag/v0.1.8) depends on `pdf-inspector` 0.1.8, which includes the escaped-comment fix from [`firecrawl/pdf-inspector` PR #259](https://github.com/firecrawl/pdf-inspector/pull/259). It publishes `firecrawl-anydoc==0.1.8` ABI3 wheels for macOS, Linux, and Windows.
- **Pinning.** `pyproject.toml` pins the official PyPI release, and `uv.lock` records the SHA-256 hash of every platform wheel. Users install a wheel and do not need Rust.

**Benchmark** (`benchmark/` — 25 PDFs from the Juno matter; metric: unique ground-truth words missing from converter output, vs. a PyMuPDF text-layer baseline; comparison run 2026-08-06 on an M-series Mac, with official AnyDoc 0.1.8 re-run 2026-08-11):

| Converter | Mean missing | Worst file | Files >2% missing | Total time |
|---|---|---|---|---|
| MarkItDown (former PDF route) | 0.31% | 3.7% | 2 | 23.0 s |
| AnyDoc 0.1.3 (former pin, buggy) | 1.13% | **27.0%** | 1 | 0.5 s |
| AnyDoc 0.1.8 (current PDF route) | **0.10%** | 1.4% | 0 | **0.6 s** |

Notes: AnyDoc 0.1.3's 27% loss is `state_court_archive/Alice DTPA letter.pdf`, a Quartz PDF — exactly the bug above; AnyDoc 0.1.8 recovers it to 0.9%. MarkItDown's two >2% files are tokenization artifacts (hyphenation/whitespace), not real loss. AnyDoc 0.1.8 is ~40× faster than MarkItDown.

To re-run (per-file TSV: path, pages, unique words, missing rate, seconds):

```sh
uv run --with 'markitdown[pdf]' python benchmark/bench.py markitdown benchmark
uv run python benchmark/bench.py anydoc benchmark
```

**DOCX benchmark** (for contrast — the `.docx` route already uses AnyDoc and is unaffected by the PDF bug). Same metric over 189 substantive `.docx` files across all matters, ground truth taken from each file's raw `word/document.xml` (run 2026-08-06):

| Converter | Mean missing | Worst file | Files >2% missing | Total time |
|---|---|---|---|---|
| AnyDoc (current DOCX route) | 0.024% | 0.9% | 0 | **0.1 s** |
| MarkItDown (`[docx]` extra, not installed here) | 0.025% | 0.9% | 0 | 16.2 s |

The two converters produce identical worst-file lists at identical rates — the sub-1% residuals are ground-truth artifacts (text boxes/fields counted in the XML), not conversion loss. Fidelity is equal; AnyDoc is ~160× faster. Re-run with `uv run python benchmark/bench_docx.py anydoc <dir>` (the `markitdown` mode needs `markitdown[docx]`, which this repo intentionally does not install).

> **About OCR:** scanned PDFs have no selectable text, so they need OCR before the AI can read them. OCR can be slow, so it does **not** run automatically. If `startup.py` reports PDFs that need it, ask before running `uv run startup.py --ocr`.

> **One caution:** don't edit or replace source files while `startup.py` is running. If a file changes mid-run, its conversion is failed on purpose and retried on the next run.

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

These are the built-in commands the assistant can use. Claude reads `.claude/skills/`,
and Codex reads `.agents/skills/`. Both directories are checked in as ordinary files so
they work immediately after cloning on macOS, Linux, and Windows.

`.claude/skills/` is the canonical copy. After editing or adding a skill, update the
Codex copies with:

```sh
uv run sync_skills.py
```

To check for drift without changing files, run:

```sh
uv run sync_skills.py --check
```

GitHub Actions runs the same check on macOS, Linux, and Windows. Do not edit
`.agents/skills/` directly; the next sync replaces it.

The Claude-only `/share` skill is intentionally not copied. It requires Claude Code's
`CLAUDE_CODE_SESSION_ID`, for which Codex has no documented repository interface.

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
uv run .claude/skills/compare/scripts/run_compare.py original.docx modified.docx
uv run .claude/skills/compare/scripts/run_compare.py original.docx modified.docx -o comparison.docx --author "A. Lin"
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
