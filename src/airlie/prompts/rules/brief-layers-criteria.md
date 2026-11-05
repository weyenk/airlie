---
kind: rule
roles: [architect]
fields: [cap, criteria]
governs:
  layers.criteria: soft-cap
why: |
  The criteria cap of the architect's layers brief (story.max_layer_criteria, 2026-11-05). A layer's whole
  package of tests is written in one reply: AIR-62 layer 01 (18 tests over 6 criteria) was cut off, AIR-55 and
  AIR-61 layer 02 (6 tests over 6) ran out of thinking, while layers of 0-5 criteria, mostly ~4 tests,
  packaged fine. Soft: a plan with a bigger layer goes to the reviewer, which may let it stand.
---
Give each layer at most {cap} acceptance {criteria} and about 4 proof tests: the layer's whole package of tests is written in one reply, and bigger layers have run out of room. Only a layer that cannot be cut without splitting a criterion, or without a part that cannot be tested on its own, may have more; another model is asked whether it can be split, and a plan it can be is sent back.
