---
name: share
description: Use to share the claude code session to the agentsview cloud service.
---

# Share Skill

If the user asks to share this session, run the following command:
!`caption sync --session-id $CLAUDE_CODE_SESSION_ID`
And then show the user the command you ran and what its output was.

If "sent_count" was not 1, remind the user that agentsview must be running, and then offer to run `caption sync --session-id $CLAUDE_CODE_SESSION_ID` again.
