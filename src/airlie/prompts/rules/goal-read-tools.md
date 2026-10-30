---
kind: rule
roles: [architect, design, docs, product, research, security, tester, ux]
fields: [worktree_hint]
governs:
  repo.edit: read-only
why: |
  2026-10-29, bundle 2 run 3 (roman): the architect, with only read_file, list_dir and grep, was told by
  goal-tools to "change it, run the tests"; on its third package draft it spent the whole thinking budget on
  "there's a contradiction in the system prompt", went on reasoning in its answer and was cut at max_tokens
  (46,747 characters). A role whose tools only read is told so, and asked for its deliverable in its reply.
---
You are working inside a git checkout of the repository ({worktree_hint}). Your tools only read: use them to look at what you need, then give the complete deliverable in your reply.
