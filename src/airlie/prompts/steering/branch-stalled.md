---
kind: steering
roles: [coder]
fields: [name, args, out]
when: The run being forked stopped for repeating one failing tool call; this names the call, its arguments and the end of its error, after branch-start.
governs:
  approach: different
why: |
  Added when fork children began to inherit the parent's state with the stuck tail trimmed (fix(fork), live
  findings from the first smoke run, 2026-10-13, 1fc6405): the nudge names the repeated call and its error.
  The caller passes `out` already cut to the last 600 characters.
---
The branch stalled by calling `{name}` with arguments `{args}` repeatedly; it failed with:
```
{out}
```
Do not call it that way again; supply the required arguments.
