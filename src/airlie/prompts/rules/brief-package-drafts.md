---
kind: rule
roles: [architect]
fields: [layers]
governs:
  package.when: drafted-up-front
why: |
  The opening of the conversation in which the architect drafts every layer's package (amendment of
  2026-10-26 to the planning-line design, Noah: write the whole plan, as superpowers' writing-plans does).
  Run 5 on airlie-sandbox asked for each package cold, just before its layer: four asks that re-read the same
  files, 27 of 37 calls reads, each thinking to its cap. In one conversation the repository is read once; a
  later layer's tests import what an earlier layer's interfaces declare, and the check on the real base just
  before each layer still catches a draft that does not fit.
---
You write the packages of all {layers} layers of this story in this conversation, one layer per turn, starting with layer 01 below. Read what you need of the repository now: the later turns ask for the next layer and rely on what you have read. Each layer is built on the branch the layer below it produced from your package, so a later layer's tests import the names an earlier layer's interfaces declare, and a test file an earlier package wrote is there as that package wrote it: when a later package changes it, write it whole, with the earlier tests kept.
