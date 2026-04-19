---
name: journal
description: Use whenever the user wants to read or save to the journal.
---

# Journal Skill

## Overview

Use this skill whenever the user wants to:
- read prior journal entries
- search the journal for relevant prior work
- save or update a journal entry for the current task

The journal lives at `~/legal/_journal/` and aggregates entries across all matters.

## Journal conventions

- Treat `basename $PWD` as the active `matter_name`.
- Journal filenames must follow:
  - `~/legal/_journal/{matter_name}_{yyyymmdd}_{taskdescription}.md`
- `yyyymmdd` uses `YYYYMMDD`.
- `taskdescription` must be a short snake_case descriptor such as `msa_redraft`, `discovery_responses`, or `corporate_cleanup`.

Example:
- `~/legal/_journal/mikejones_20250520_corporate_cleanup.md`

## Workflow: read journal entries

When instructed to look at the journal:

1. List `~/legal/_journal/`.
2. Identify entries that appear relevant by filename.
3. Relevance includes:
   - the current matter
   - the current task
   - similar tasks from different matters
4. Read up to 3 journal entries before asking the user for permission to read more.
5. Briefly summarize any relevant prior work, decisions, or user preferences from those entries.
6. If nothing appears relevant, say that explicitly and proceed.

## Workflow: write or update a journal entry

After performing a task when the user wants it logged:

1. Determine the journal filename using the convention above.
2. Append to the existing file or create it if it does not exist.
3. Add a section in this format:
   - `## [timestamp] - [short task label]`
   - `Files touched:` list of paths
   - `What I did:` concise bullet summary of the steps taken
   - `Key outputs:` where drafts or summaries are located
   - `User corrections / feedback:` explicit tracking of any user corrections and how you adapted
   - `Open questions / follow-ups:` anything that should be revisited

## Corrections emphasis

When the user corrects you:

- Capture exactly what was wrong and the corrected version.
- State how the correction affects future work for this matter.
- If the correction should become a standing preference, say so plainly.

Example:
- `Always treat X as Y in this matter unless explicitly changed.`

## Response requirement

After writing to the journal, include a concise plain-language summary of what you logged so the user can verify it without opening the journal file.
