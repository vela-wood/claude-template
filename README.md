# Claude Code Legal Template

A workspace for legal professionals to use Claude Code for document review, drafting, redlining, and transcript management — all from the command line.

## What this does

When you open this folder in Claude Code, Claude automatically:

- Reads and understands your matter files (PDFs, Word documents, emails).
- Follows legal-specific instructions for precision, citation, and journaling.
- Gives you access to built-in skills (see below) for common legal workflows.

## Setup

### 1. Install prerequisites

You need two things installed on your computer:

- **Git** — [git-scm.com/downloads](https://git-scm.com/downloads)
- **uv** (Python package manager) — [docs.astral.sh/uv](https://docs.astral.sh/uv/)

### 2. Clone the repository

Open your terminal and run:

```
git clone https://github.com/vela-wood/claude-template.git
cd claude-template
```

### 3. Install dependencies

```
uv sync
```

This installs Python and all required packages into a local environment. Nothing is installed globally on your system.

### 4. Connect your accounts

```
uv run setup_claude.py
```

This will prompt you to authenticate and then save the necessary credentials to a local `.env` file. You only need to do this once.

### 5. Launch Claude Code

Open the `claude-template` folder in Claude Code and start working.

## Skills

Skills are special commands you can type inside Claude Code to trigger specific workflows. Type the command and Claude takes it from there.

### `/redline` — Edit and compare Word documents

Use this when you need to:

- **Compare two versions** of a `.docx` file and see what changed.
- **Apply edits** to a Word document with tracked changes.
- **Extract text** from a `.docx` for review.

Claude generates a redlined Word document with native Track Changes markup, just like you would see in Microsoft Word.

### `/caption` — Manage transcripts and recordings

Use this when you need to:

- **Search** across your transcripts for specific terms or topics.
- **Download** a transcript from a meeting or recording.
- **Organize** projects and folders in your Caption workspace.

Requires Caption account credentials (configured during setup).

### `/vs` — Version Story document management

Use this when you need to:

- **Track versions** of documents with full history.
- **Compare changes** between document versions.
- **Collaborate** on documents with branching and merging.

Works with `.docx` and `.pdf` files. Requires a Version Story account.

## Day-to-day usage

1. **Copy your matter files** (PDFs, Word docs, emails) into this folder or a subfolder.
2. **Open the folder in Claude Code.**
3. **Ask Claude what you need** — summarize a contract, compare two drafts, find a clause, draft a response.
4. **Use skills** (`/redline`, `/caption`, `/vs`) for specialized workflows.

Claude automatically converts your files to a readable format, keeps an index of what's in the folder, and journals what it did so you have a record.
