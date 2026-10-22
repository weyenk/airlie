---
kind: rule
roles: [tester, coder]
governs:
  file.rewrite: never
why: |
  In every build brief and tester brief. No docstring in the code; added with ba3cb08 (2026-10-16, HDS-5
  layer 01: Qwen3-Coder rewrote all of a 17,000-character test file in one write_file, was cut off at
  max_tokens 4096, then derailed into empty calls); write_file refuses to overwrite a long file since then.
---
Change an existing file with edit_file, not by rewriting it.
