---
kind: rule
roles: [reviewer]
fields: [cap, criteria]
governs:
  layers.criteria: soft-cap
why: |
  The reviewer's brief on a layer plan with layers over story.max_layer_criteria (2026-11-05). The architect's
  package step failed mostly on big layers: AIR-62 layer 01 (18 tests over 6 criteria, its reply cut off),
  AIR-55 and AIR-61 layer 02 (6 over 6), while layers of 0-5 criteria packaged fine. Noah: the cap is soft,
  since sometimes a story cannot be broken down further, so a model of another family rules on each layer.
---
This is not a code review: no code is written yet. The architect has split a story into layers, and each layer is built from a package of tests the architect writes in one reply, so a layer should make at most {cap} acceptance {criteria} true. The layers below make more. For each of them, say whether it can be split into smaller layers without splitting an acceptance criterion and without making a layer that cannot be tested on its own: split, with the split you suggest in one line, or keep, with the reason in one line. Read only what is shown here.
