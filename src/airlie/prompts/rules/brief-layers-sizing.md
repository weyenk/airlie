---
kind: rule
roles: [architect]
fields: [max_steps, context_budget]
governs:
  layers.size: one-concept
yields_to:
  - brief-layers-rules
  - role:architect
why: |
  The sizing rule of the architect's layers brief: a layer must fit the coder's step and context budget, or
  it blocks for size. Added with the size-block lessons (AIR-5's layer 01 failed four times, the last at
  ~111k tokens, and the architect had no signal that its layers were too big).
---
Size each layer to fit the coder: one concept per layer, about 3 source files and 1 test file. The coder has {max_steps} steps and a context budget of {context_budget} tokens per layer: a layer that needs more than that will run out before it finishes and block the story.
