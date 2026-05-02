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

Journal entries must be associated with a specific matter. Use context to select from the below matters:
!`uv run nd.py --journal`

If there is ANY uncertainty regarding which matter the user is working on, ask the user. Refer to the selected matter as `matter_name`.

Store all journals in `~/legal/_journal/matter_name`.

## Journal conventions

- Journal filenames must always be in the form of `{yyyymmdd}_{taskdescription}.md`
- `taskdescription` is a short snake_case descriptor such as `msa_redraft`, `discovery_responses`, or `corporate_cleanup`.

## Workflow: writing a journal entries

When instructed to create a journal, the contents should follow this format:
   - The firstclaude code session uuid
   - `## [timestamp] - [short task label]`
   - `Files touched:` list of paths
   - `What I did:` concise bullet summary of the steps taken
   - `Key outputs:` where drafts or summaries are located
   - `User corrections / feedback:` explicit tracking of any user corrections and how you adapted
   - `Open questions / follow-ups:` anything that should be revisited

The user may have access to the following additional features:
!`caption doctor`

The below directions depend on whether the user has access to "core" or "agentsview." If the corresponding feature is not mentioned above, ignore the corresponding directions.

### Journal cloud storage

If "agentsview" is available, also sync any journal entries you save locally with:
`caption create_md {path_to_journal_entry.md} --project-name {matter_name}`

### Session cloud stoage

If "agentsview" is available, and the user saves a journal, ask if the user also wants to sync the entire claude code session to the cloud by posing this exact question to the user:

"Would you also like to share this entire claude code session with your organization? If so, first make sure agentsview is running and has finished syncing before answering yes."

If the user answers yes, run:

`caption sync --session-id {this_claude_code_session_uuid} --project-name {matter_name}`

## Workflow: reading journal entries

When instructed to look at the journal:

1. Load all markdown files inside the `matter_name` subfolder.
2. Identify entries that appear relevant by filename.
3. Read up to 3 journal entries before asking the user for permission to read more.
4. Briefly summarize any relevant prior work, decisions, or user preferences from those entries.
5. If nothing appears relevant, say that explicitly and proceed.

### Reading journal entries from the cloud

If "agentsview" is available, when instructed to look at the journal, also retrieve journal entries from the cloud with:
`caption list_md --project {matter_name}`

The output of `list_md` should contain summaries. Ask the user if any of the files look relevant, and if so, retrieve the full journal with:
`caption get_md {uuid}`