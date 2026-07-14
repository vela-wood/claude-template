---
name: journal
description: Use whenever the user wants to read or save to the journal.
---

# Journal Skill

## Overview

Use this skill whenever the user asks to:
- read prior journal entries
- search the journal for relevant prior work
- save a journal entry for the current task

Journal entries are always associated with a specific matter. Use context to select from the below matters:
!`uv run nd.py --journal`

If there is ANY uncertainty regarding which matter the user is working on, ask the user. After selecting a matter, only refer to it by its `matter_code`, which are the 5 numbers at the beginning of the full name, e.g. `44924`. Determine `matter_code` before proceeding with any journal operations.

Organize and store all journals by using this directory structure: `~/legal/_journal/{matter_code}/`.

The journal is a separate git repository. If the user has not yet initialized the journal, run the following commands to initialize it:
cd ~/legal/_journal
git init
git add .
git commit -m "initialize local journal history"
git remote -v

The journal repository MUST ALWAYS REMAIN local only. Do not push it to any remote repository, and do not allow any other user to access it. The journal is a private record of the user's work and must be kept confidential.

Because the journal is its own git repository, git history is the only backup mechanism. Never create backup copies of journal files (no `.bak` files, no dated duplicates of `_matter.md`); every write session ends with one atomic commit instead (see step 5 of the writing workflow). To recover or inspect a prior version of any journal file, use git (`git log --follow {file}`, `git show {commit}:{file}`).

### Terminology and directory layout

- **Matter:** the client matter whose journal is being read or updated.
- **`matter_code`:** the matter's numeric identifier, such as `44924`. It names the matter's journal directory.
- **`_matter.md`:** the literal reserved filename for the one live current-state file in each matter directory, located at `~/legal/_journal/{matter_code}/_matter.md`. Do not replace `_matter` with the matter code or matter name. The file contains the matter's Current state, Open items, and Resolved items.
- **Dated entries:** the matter's historical journal records, named `{yyyymmdd}_{taskdescription}.md` and stored beside `_matter.md`.

For example:

```text
~/legal/_journal/44924/
├── _matter.md                  # current reconciled state for matter 44924
├── 20260710_diligence.md      # historical dated entry
└── 20260714_closing_update.md # historical dated entry
```

The dated entries are the append-only authoritative history. `_matter.md` is the concise current view derived from that history and verified against the source documents; it is not the matter code, matter directory, or a dated entry.

## Journal conventions

- Journal filenames use this format: `{yyyymmdd}_{taskdescription}.md`
- `taskdescription` is a short snake_case descriptor such as `diligence`, `discovery_responses`, or `corporate_cleanup`.
- No backup files, ever: prior versions of `_matter.md` live in git history, not in sibling files. A matter directory contains only `_matter.md` and dated entries, so no glob, reader, or hook ever mistakes anything else for the live state file.

## Accuracy model

`_matter.md` is a *verified projection* of the dated entries, not a parallel document kept by hand. There is one `_matter.md` per matter directory. The dated entries' **Decisions** (append-only, each with a pinpoint cite) are the authority; `_matter.md` is the current view computed from them and tied out to the source documents. Two consequences drive the workflow below:

- **A self-consistent journal can still be wrong.** State that agrees with the entries but has drifted from the operative document is an accuracy failure, not a clean record. Every live fact must trace to both a Decision and a source cite.
- **Many conversations write into the same matter**, so `_matter.md` routinely lags entries written by a parallel session. Every write reconciles the full record before it composes, so a later session always folds in an earlier one's decisions instead of clobbering them.

`_matter.md` carries a `**State as of:** {yyyymmdd}` line at the top. Each live fact carries its cite and a MANDATORY **epistemic status tag** - the tag records how the journal knows it:

- `[decided {yyyymmdd}]` - the principal (user, client, counsel) actually decided it; traces to a quoted message or executed document. A session's own reasoning NEVER earns this tag, however sound.
- `[verified {yyyymmdd}]` - tied out against the operative source document on that date (the existing `verified` stamp).
- `[inferred]` - a session's gloss or conclusion; name the basis inline. An inference may sit beside a decision, never inside one.
- `[unverified]` - carried on the record but its cited source has never been pulled/read. Keeps the tag until someone actually opens the source.

Status moves UP only through evidence: `[inferred]`/`[unverified]` become `[verified]` by opening the source, `[decided]` by a quoted decision - never by repetition, age, or having appeared in a prior state file. When relaying to the user, preserve the tag's level ("the record has carried X since {date}, unverified" - not "X is true"); a gloss is never relayed as decided. This is the root cause of the system's worst failures: an unlabeled inference written under Decisions reads as decided forever after.

## Workflow: writing a journal entry

Maximum accuracy over speed: reconcile in full on every write, tie state out to the source documents, and never auto-resolve a contradiction. Five steps, in order.

### 1. Reconcile `_matter.md` against the full record (before composing anything)

Load `_matter.md` and **all** dated entries for the matter, in date order (no skipping - accuracy over speed). Then:

- **Write-write guard.** Read `_matter.md`'s `State as of` date. If any dated entry is newer, a parallel conversation wrote since the last reconciliation - fold those entries in now; merge, never clobber. This is the cross-conversation case and where drift actually enters.
- **Internal reconciliation (state vs entries).** Walk the Decisions in date order and project current state: move any open item a later Decision resolved into Resolved (dated); replace any figure/fact a later Decision superseded, noting what it supersedes; apply any reversal.
- **Source tie-out (state vs documents).** For each live fact: confirm its cite resolves and the cited version is still the operative one (flag if a newer version exists); re-derive every *computed* figure from its inputs in python and diff (e.g. shares = investment / PPS). Stamp each confirmed fact `verified {yyyymmdd}`. Flag, never silently pass, any fact that is uncited, whose source is unreachable, or whose source version has moved.
- **Open-item thread re-read (decisions hide in side threads).** Every open item pins the chat/email thread that controls it. Re-read that thread FORWARD from the item's `last checked` timestamp before restating it - a reply that landed after the journal last looked may have already resolved it (the 8-replies-later trap). If it was decided, move it to Resolved and capture the deciding message verbatim (quoted + ts + author); if still open, refresh `last checked`. Never carry an open item forward from its old text without re-reading its thread.
- **Open-item dedup (items vs items).** Group the open items by instrument/document/subject before writing. Two items touching the same subject with different status - one carried live, one resolved - are a conflict, not two items: merge them under the latest decision, or surface for adjudication. (This is how "draft the 4 Garrett CNs" survived as live after the same notes were resolved as post-close cleanup.)
- **Mootness re-justification (events kill items too).** An open item survives reconciliation only if you can name the live object it still touches (a document to edit, a message owed, a decision pending). Test each item against EVENTS, not its own wording - a filed charter, an executed agreement, a completed send moots every task that existed to perfect it, even when no Decision line mentions the item. If no live object can be named, close it to Resolved citing the mooting event. (This is how "de-bracket the OIP" survived two reconciliations after the COI was FILED - each pass re-trimmed the item's text instead of asking whether filing had mooted it.)
- **Anti-re-raise gate (opens are born cited).** Before creating any NEW open item, flag, or "verify X" task, search the Resolved log AND the dated entries for its key terms (grep, not memory). If a prior Decision or Resolved line already answers it, do not create it. If circumstances genuinely reopen it, the new item must quote the resolving line and state what changed ("reopens {date} resolution because ..."). An open item with no such search behind it is presumed stale. (This is how "verify good-standing certs" was born 6/30 contradicting a 6/15 resolution and got re-raised to the user weeks later.)
- **Conflicts.** A Decision that says "supersedes X" is trusted (the author declared intent). An *unmarked* contradiction between two entries is NOT resolved by recency - stop and surface both with their cites for the user to adjudicate before writing.
- **Completeness.** Every material change this session makes must become a Decision line. Flag any live `_matter.md` fact that traces to no Decision (including direct hand-edits) so it gets a cite rather than silent trust.
- **Prune (hard cap).** Keep `_matter.md` tight - a state file, not a log. Detail lives in the dated entries; the state file holds only what is currently true plus the dated Resolved log. HARD CAP: `_matter.md` must stay readable in a single Read call (~20k tokens / ~60KB). A state file nobody can load is why resolutions go unseen and get re-litigated - opens are injected into every prompt while Resolved sits unread at the bottom of an unloadable file. If a write would exceed the cap, prune first: collapse each open item to <=3 lines + a pointer to its dated entry, and enforce the one-line Resolved format below.

### 2. Write the dated entry

Sections, in this exact order:

- **State** - one short paragraph: where the matter stands after this session.
- **Decisions / positions taken** - each a one-line claim with a pinpoint cite (doc + section/cell/version). The defensible record. Mark any reversal ("supersedes ..."). Record ONLY what was actually decided (quote + author + ts); any accompanying gloss or extension is labeled `[inferred]` inside the line, never folded into the decision's words.
- **Corrections received** - what the user corrected and how you adapted. Flag each `[matter-specific]` or `[candidate standing preference]` (the latter prompts promotion per CLAUDE.md §8).
- **Open items (as of this date)** - a snapshot frozen at write-time, NOT back-edited when an item later closes; the live list is `_matter.md`.
- **Artifacts** - files produced or edited (path + version). Brief; reference, not substance.

Do not include a step-by-step "what I did" log - it has the lowest recall value. Capture the result and the authority, not the process.

### 3. Update `_matter.md` - write it LAST

Write the reconciled projection and set the `State as of` line to today. **`_matter.md` must be the LAST file written** (after the dated entry): "state is the newest file in the matter folder" is the compliant invariant that the staleness tripwire keys off - the inject-hook and the reading workflow treat any dated entry with a newer mtime than `_matter.md` as unfolded work and flag the state file STALE. A session that dies after the entry but before the state update leaves exactly that detectable signature.

- **Current state** - live figures, structure, settled facts, each with cite + `verified {date}` (e.g., `A-1 OIP $0.0866 (supersedes TS v10 $0.0992) [POC v12 §1; verified 20260619]`). When a value changes, replace it and note what it supersedes; never leave two live values for one fact. A resolved fact that is still operationally load-bearing (e.g., "NO BUYOUT") lives HERE, not as a long Resolved narrative.
- **Open items** - the single live open list for the matter, each with date/owner **and the controlling thread it lives in** (`[thread id; last checked {yyyymmdd-hhmm}]`). Resolving something is written ONLY here (strike it / move to Resolved); the snapshots inside dated entries are never back-conformed.
- **Resolved** - settled questions, dated, append-only, so they are never re-litigated. **ONE LINE per item:** `- **{yyyy-mm-dd} ({short claim}):** one-sentence holding [see {entry}.md]`. The reasoning lives in the dated entry the pointer names - never let `_matter.md` be the only home of detail. Record reversals here. (Format-based, not age-based: even old guards stay, but as one-liners.)

### 4. Second-pass verification (when state moved)

If step 1 changed any live figure, or resolved or created an open item, verify before finalizing: independently re-derive the affected state from the dated entries and diff it against what you wrote in `_matter.md`; resolve any delta. For a matter with many entries this can fan out to subagents (one per fact or document) so it does not crowd the working context. Skip only for a no-op entry that moved nothing.

### 5. Commit atomically

Finish every write with exactly one commit in the journal repository that stages the session's dated entry and `_matter.md` together:

```bash
cd ~/legal/_journal
git add {matter_code}/
git commit -m "{matter_code}: {yyyymmdd} {taskdescription}"
```

One session's write = one commit; never split the dated entry and the state update across commits, and never leave a write uncommitted. The commit is the journal's only backup and its recovery point - a `_matter.md` reconciliation that goes wrong is undone with git (`git diff`, `git checkout {commit} -- {file}`), not by restoring a copied file.

## Workflow: Reading journal entries

1. Read `_matter.md` first - the single source of truth for live state. For most reads this is the answer. Derive "what is currently live" ONLY from here, never from a dated entry's "Open items" snapshot. Before presenting any open item as live, re-read its pinned controlling thread forward from `last checked` - a later message may have already resolved it - AND check it against the Resolved log: an "open" item a Resolved line already answers is reported as stale-and-closable, never as open. The Open list is the more visible record but the less trustworthy one; Resolved wins. Relay every fact at its tagged epistemic level - never upgrade `[inferred]` or `[unverified]` to fact-or-decision in the retelling.
2. Staleness check: if any dated entry's file mtime is newer than `_matter.md`'s, or the `State as of` stamp lags the newest entry's filename date, the state file is stale - run the step-1 reconciliation, or, if you are only reading, name the unfolded entries and do NOT present the state as current. (The inject-hook runs this same check and labels its injection CURRENT / STALE / UNSTAMPED - trust that verdict.)
3. Load dated entries only for the detail behind a state line; identify relevant ones by filename.
4. Read up to 3 dated entries before asking the user for permission to read more.
5. Briefly summarize any relevant prior work, decisions, or user preferences; if nothing appears relevant, say so explicitly and proceed.
