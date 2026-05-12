---
name: sync
description: Use to sync the claude code session to the agentsview cloud service.
---

# Sync Skill

If the user asks to sync this session, run the following command:
!`caption sync --session-id $CLAUDE_CODE_SESSION_ID`
And then show the user the command you ran and what its output was.

If "sent_count" was not 1, remind the user that agentsview must be running, and then offer to run `caption sync --session-id $CLAUDE_CODE_SESSION_ID` again.