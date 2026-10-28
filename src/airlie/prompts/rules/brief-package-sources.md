---
kind: rule
roles: [architect]
governs:
  package.reading: files-given
why: |
  Lever 2 of the wiki benchmark (2026-10-27): the architect's package turns spent most of their time in tool
  calls re-reading the files its layer changes, each step thinking to its cap and growing the conversation
  (one revision of layer 03: eight reads, ~20 minutes, 38k to 45k tokens). The brief now carries those files
  as they are on its base; a tool read is for what it does not hold.
---
## The files this layer changes, as they are on its base
Here they are whole, or clipped where marked: write the package from them, and read others only when you need them.
