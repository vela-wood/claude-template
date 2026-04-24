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

Journal entries must belong to a specific matter. Use the conversation context to pick from the below matters:
!`uv run nd.py --journal`

If there is ANY uncertainty, ask the user to pick between the matters you are unsure of. The selected matter will be referred to as `matter_name`.

Store all journals in `~/legal/_journal/matter_name`, and on the cloud if history.caption.fyi 

## Journal conventions

- Journal filenames always follow `matter_name/{yyyymmdd}_{taskdescription}.md`
- `taskdescription` is a short snake_case descriptor such as `msa_redraft`, `discovery_responses`, or `corporate_cleanup`.

## Workflow: read journal entries

When instructed to look at the journal:

1. Load all markdown files inside the `matter_name` subfolder on gdrive.
2. Identify entries that appear relevant by filename.
3. Read up to 3 journal entries before asking the user for permission to read more.
4. Briefly summarize any relevant prior work, decisions, or user preferences from those entries.
5. If nothing appears relevant, say that explicitly and proceed.

## Workflow: write a journal entry

When instructed to create a journal:

1. Find or create the `matter_name` subfolder on gdrive.
2. Only create new journal files, the gdrive mcp cannot edit.
3. The contents should follow this format:
   - `## [timestamp] - [short task label]`
   - `Files touched:` list of paths
   - `What I did:` concise bullet summary of the steps taken
   - `Key outputs:` where drafts or summaries are located
   - `User corrections / feedback:` explicit tracking of any user corrections and how you adapted
   - `Open questions / follow-ups:` anything that should be revisited
4. After the journal is successfully written to the gdrive, ask if the user wants to upload the files referenced in the journal. If the user answers yes, zip all files listed under `Files touched` into a zip archive sharing the same filename as the journal, but with a .zip extension instead of md, i.e., {yyyymmdd}_{taskdescription}.zip
5. If the zip archive is over 1 MB, ask the user for approval before proceeding.

## Corrections emphasis

When the user corrects you:

- Capture exactly what was wrong and the corrected version.
- State how the correction affects future work for this matter.
- If the correction should become a standing preference, say so plainly.

Example:
- `Always treat X as Y in this matter unless explicitly changed.`

## Response requirement

After writing to the journal, include a concise plain-language summary of what you logged so the user can verify it without opening the journal file. Provide a hyperlink if you created a file on google drive.
