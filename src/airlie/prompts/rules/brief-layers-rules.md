---
kind: rule
roles: [architect]
fields: [max_layers]
governs:
  layers.order: bottom-first
  layers.new-name: below-use
  layers.size: small
  tests.fail-first: required
  tests.negative: inside-trigger
why: |
  How the architect cuts a story into layers. No docstring in the code. The cap, the dependency order and
  one criterion per layer since b34f992 (2026-10-14). The red-before-green sentences came with 44b022d
  (2026-10-17, AIR-5 layer 01: tests of an unknown name passed on the base, which already refused it for
  the wrong reason); the NOT-happen sentence with b7c2b75 (2026-10-19, AIR-31 layer 02: a test that a
  feature is a no-op below its threshold passed before the code whatever the coder did).
---
Rules: at most {max_layers} layers. Order them by dependency: foundations (data models, interfaces) at the bottom, then the logic that uses them, then the surfaces (CLI, API, UI, docs). Each layer is one coherent change a reviewer can read in a few minutes, and the test suite passes after it. Assign every acceptance criterion to the layer that makes it true. A small story is one layer. A layer's tests must fail before the layer and pass after it: put the layer that adds a name (a tool, option or field) below the layer that validates or uses it, since code that refuses an unknown name already refuses it for the wrong reason; where a criterion could pass on the base by accident, say in that layer's brief which message or output proves the new behaviour. A criterion that something does NOT happen (no event, no message, unchanged output) is proven inside a test that also makes it happen, never as a test of its own: airlie rejects a test that passes before the layer's code is written, and one that checks for nothing always does.
