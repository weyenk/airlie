---
kind: steering
roles: [any]
when: A subtask sent back with a nudge (a fix round, a follow-up) has a stored conversation over teams.fresh_continuation_tokens, so it starts a new one; the diff of what it changed (or, when that is too long, the list of files it changed) goes between this text and the nudge, and this goes between its brief and that.
governs:
  history: worktree
  diff: given
why: |
  What a subtask sent back on a long conversation is told instead of seeing that conversation again.

  Added with the fresh start (86d9a06, 2026-10-17; HDS-5 layer 01: the fix round and the red-check
  follow-up resent the coder's whole conversation, 58k then 72k tokens, and both replies were cut off at
  max_tokens before the coder acted).

  Reworded 2026-10-23 (AIR-9 layer 05: the retry re-read its own files for 20 steps, told not to read them
  while the diff was too long to show or empty; it now says to read only the part it is about to change).
---
You have already worked on this subtask, and your earlier conversation is not repeated here. What you changed so far is in the worktree, and below as a diff (or, when that is too long, as the list of files you changed). Do not read a file to find what you did; read only the part you are about to change. Pick up from there and deal with what follows.
