---
kind: rule
roles: [architect]
fields: [layers]
governs:
  package.when: drafted-up-front
why: |
  `brief-package-drafts` for packages written with the file tools (the tool-written packages design,
  2026-11-06). The drafts are files in the conversation's checkout: a later layer finds an earlier layer's
  test files as that layer left them and changes them in place, where before it had to write such a file
  whole, earlier tests and all.
---
You write the packages of all {layers} layers of this story in this conversation, one layer per turn, starting with layer 01 below. Read what you need of the repository now: the later turns ask for the next layer and rely on what you have read. Each layer is built on the branch the layer below it produced from your package, so a later layer's tests import the names an earlier layer's interfaces declare, and a test file an earlier package wrote is in the repository as that package left it: when a later package changes it, it does so with edit_file, the earlier tests kept.
