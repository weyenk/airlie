---
kind: rule
roles: [coder]
why: |
  What the coder is told when a tester wrote the layer's tests before it. No docstring in the code; added
  with the tester seat (3bda4e1, 2026-10-19, lever 2: on AIR-31 layer 02 the coder had the code right by
  step 26, then spent 80 steps failing to write a test that crosses the threshold) and reworded when the
  tests became red before the code and read-only to the coder, whose run_tests runs only them (1741541,
  2026-10-19).
---
The tests for this layer were written before you (the result of 'tests' below lists them and what each asserts), and airlie has checked that each fails without the code. They are the contract, and you cannot change their files: make them pass with the code. If one is wrong about a name or a signature the brief fixes, say which and why when you stop. Add tests in a new file for anything else you change. Your run_tests runs your tests; once they pass, you are done: airlie runs the whole suite after review.
