---
kind: steering
roles: [architect]
when: A layer's package failed the readiness check and the architect is asked for it again; it heads the package as airlie checked it (each test file after ruff's fixes, only the additions for a file the package added to, then its JSON object), after the findings and before `package-fix`.
governs:
  package.previous: given
why: |
  AIR-59 and AIR-61, 2026-11-05: a package retry on a conversation over teams.fresh_continuation_tokens was
  started fresh, and `fresh-continuation` points at a worktree diff, but a package is a reply, not a change
  to the worktree, so the diff was empty and the package gone. AIR-59's architect ("I don't have the exact
  text of the middle tests!") wrote its tests again from memory and repeated the same 111-character line;
  AIR-61's changed a test's meaning. The retry now carries the package as checked.
---
Here is your package as airlie checked it: each test file after ruff's fixes (for a file you added tests to, only your additions), then its JSON object. Start from it and change only what the findings name, keeping every other line as it is; do not write it again from memory.
