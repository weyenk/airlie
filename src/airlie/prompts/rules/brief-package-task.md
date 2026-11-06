---
kind: rule
roles: [architect]
governs:
  package.form: tests-and-steps
why: |
  The opening of the architect's package brief, the call made just before a layer is built (P1 of the
  planning line, 2026-10-24). The tests are the layer's design and its contract, written by the role that has
  read the code (AIR-22 layer 03: a tester spent 62 minutes designing them inside a 20-step loop, twice), and
  the steps leave the coder nothing to design. airlie runs the suite on the tests before the coder starts, so
  the rule says what comes back to the architect. A criterion already true on the base (AIR-59, AIR-61 layer
  02, 2026-11-05) has a test that can never fail first: it is said up front, and marked a regression guard.
---
Write this layer's package: its tests as code, and the steps a coder follows to make them pass without further design. Read the test module nearest the feature first; its fixtures and fakes are yours to use, and a new test file is named for the feature. Each test calls the code the way its users will once the layer is built, with the exact names, messages and values the coder must produce, and fails now because the feature is missing. airlie runs the whole suite on your tests before the coder starts; a test that passes there (unless it is a regression guard, below), an import it cannot resolve, or a criterion with no test comes back to you. When you find a criterion already true on the base as you read the code, say so: mark its test a guard, as the contract below says, rather than force a test that fails. Do not write the feature's code.
