---
kind: steering
roles: [any]
fields: [label, width]
when: A fork point starts a branch; its nudge opens with this, naming the branch and how many branches there are.
governs:
  approach: different
why: |
  The opening of every fork branch's nudge since fork diversification was built (feat(fork), 2026-10-13,
  aa2ee3e): the branch goes on from the parent's conversation in its own copy of the repository, and is told
  the parent's approach did not make the tests pass.
---
You are now branch {label} of {width}, continuing from the conversation above in your own copy of the repository. The previous approach did not make the tests pass; do not repeat it.
