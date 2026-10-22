---
kind: rule
roles: [coder]
governs:
  repo.rules: agents-md
why: |
  In every build brief: the repository's AGENTS.md is its own rules. No docstring in the code; added when
  every layer began to pass the Test gate of the repository's AGENTS.md (9b16629, 2026-10-15).
---
Follow AGENTS.md at the repository root, if it has one.
