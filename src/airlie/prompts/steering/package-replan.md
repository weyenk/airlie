---
kind: steering
roles: [architect]
when: A layer was built from the architect's package and the whole gate is still red after the coder's follow-up; it closes the brief of the architect's next package, after the coder's account and the gate's output.
governs:
  package.replan: decide
why: |
  What the architect is told to do when its layer comes back from a build (D2a of the planning-line design,
  2026-10-24). The coder cannot change the architect's tests and the gate is red, so either a test was wrong
  about something the steps fix (AIR-9 layer 02, 2026-10-21: a test unpacked one comment from a card with
  two, and the code was right), or the layer was too much for the coder's steps and context (AIR-5 layer 01,
  four blocks at ~111k tokens). Only the architect can change either.
---
Decide which it was. If a test was wrong about a name, a value or a signature your steps fix, correct the test and the steps. If the layer was more than the coder could finish in its steps and context, write the package again with the steps ordered so each one leaves the tests closer to passing, naming the file and the function in each. Reply with the whole package again, every test file in full and the JSON object; every criterion this layer owns still needs a test. Do not write the feature's code.
