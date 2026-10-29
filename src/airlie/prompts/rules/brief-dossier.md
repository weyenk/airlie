---
kind: rule
roles: [architect, product, tester, coder]
governs:
  story.reading: files-given
why: |
  Lever P2 of the performance plan (2026-10-28): every step of a story started cold and read the same few
  files again by tool (roman noise run 8: 28 reads, sandbox/__init__.py and tests/test_version.py six times
  each), each read a full model call that thinks first and grows the conversation. The story dossier carries
  the files its earlier steps read, as they are at the step's commit; a tool read is for what it does not hold.
---
## Files the story's earlier steps already read, as they are at this step's commit
Here they are whole, or clipped where marked: do not read them again, and read others only when you need them.
