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

Organize and store all journals by using this directory structure: `~/legal/_journal/matter_code`.

## Journal conventions

- Journal filenames use this format: `{yyyymmdd}_{taskdescription}.md`
- `taskdescription` is a short snake_case descriptor such as `diligence`, `discovery_responses`, or `corporate_cleanup`.

## Workflow: writing a journal entry

When creating a journal entry, the contents should follow this format in the exact order:
   - `TL;DR:` a concise summary of the overall task and outcome
   - `Files touched:` list of paths
   - `What I did:` concise bullet summary of the steps taken
   - `Key outputs:` where drafts or summaries are located
   - `User corrections / feedback:` explicit tracking of any user corrections and how you adapted
   - `Open questions / follow-ups:` anything that should be revisited

The user has access to the following features:
!`caption doctor`

Many of the directions below are contingent on whether the user has access to the "agentsview" feature.

### Journal cloud storage

If "agentsview" is available, also sync any journal entries you save locally to the cloud with:
`caption create_md {path_to_journal_entry.md} --project-name {matter_code}`

### Session cloud stoage

If "agentsview" is available, and the user saves a journal, ask if the user also wants to sync the entire claude code session to the cloud by asking the user this question verbatim:

"Would you also like to share this entire terminal session with your organization? If yes, make sure agentsview is running and finished syncing before responding with yes."

Then, if the user answers yes, run:

`caption sync --session-id {this_claude_code_session_uuid} --project-name {matter_code}`

## Workflow: Reading journal entries

When instructed to read the journal:

1. Load all markdown files inside the `matter_code` subfolder.
2. Identify entries that appear relevant by filename.
3. Read up to 3 journal entries before asking the user for permission to read more.
4. Briefly summarize any relevant prior work, decisions, or user preferences from those entries.
5. If nothing appears relevant, say that explicitly and proceed.

### Reading journal entries from the cloud

If "agentsview" is available, when instructed to look at the journal, also retrieve journal entries from the cloud with:
`caption list_md --project {matter_code}`

The output of `list_md` should contain summaries in the `plain_text_preview` field. If any of the files look relevant, retrieve the full journal using:
`caption get_md {uuid}`
