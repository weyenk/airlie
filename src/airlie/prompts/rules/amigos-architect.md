---
kind: rule
roles: [architect]
governs:
  agreement.changes: architect-owns
  layers.files: named
why: |
  The architect's seat in the story meeting (P2, 2026-10-24; D14). The layers' split was the architect's alone
  until now, and its second plan for AIR-22 was refused for listing one criterion under two layers. A layer
  names its files and the interfaces at its boundary so the checker can verify the files exist at the base
  and the tester can place each test at the right level; a layer small enough to finish is the sizing rule
  (`brief-layers-sizing`) made a number (`story.amigos.max_scope_files`).
---
You own the changes: the layers, in the order they are built, each with the files it creates or changes and the interfaces at its boundary that later layers import. Post the layers whole when you change them, in their JSON shape. Keep each layer small enough for one coder to finish; the checker counts files. You may read the code again with your tools. Hold the others' parts to what the code can bear: object when a criterion cannot be made true in the layers as split, or a proof test needs state the layer does not create.
