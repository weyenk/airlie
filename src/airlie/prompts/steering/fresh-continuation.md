---
kind: steering
roles: [any]
when: A subtask sent back with a nudge (a fix round, a follow-up) has a stored conversation over teams.fresh_continuation_tokens, so it starts a new one; this goes between its brief and the nudge.
governs:
  history: worktree
  diff: given
why: |
  What a subtask sent back on a long conversation is told instead of seeing that conversation again.

  Added with the fresh start (86d9a06, 2026-10-17; HDS-5 layer 01: the fix round and the red-check
  follow-up resent the coder's whole conversation, 58k then 72k tokens, and both replies were cut off at
  max_tokens before the coder acted).
---
You have already worked on this subtask, and your earlier conversation is not repeated here. What you changed so far is below: it is in the worktree as shown, so do not read those files again to find it. Pick up from there and deal with what follows.
