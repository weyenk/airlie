---
kind: rule
roles: [architect]
governs:
  tests.existing: listed-or-kept
why: |
  AIR-47 layer 02, 2026-11-02: the layer made one draft PR per story the default, which broke a batch of
  existing tests in test_story_pipeline.py that assert a `gh stack init` and one PR body per layer. The
  package's steps never named them, so the coder met them one at a time after the code, at about three minutes
  a thinking step: two builds of 60-80 steps each ended red and the layer went back twice. The locked tests the
  package added were never the problem.
---
## The existing tests your change breaks
A change of behaviour (a default, a command the code runs, a file it writes, the shape of an output) breaks the existing tests that assert the old behaviour, and the coder meets them only after the code, one run at a time. Before you write the steps, grep the repository's tests for what this layer changes (the function, the command, the setting, the text) and do one of these, saying which in your steps:
- Keep the old behaviour where the story does not need it changed: a new setting's default can stay the old behaviour while the committed configuration chooses the new one, so the tests that set nothing keep passing.
- Or name each existing test whose expectation changes, in a step that says what it asserts after this layer.
A layer that leaves an existing test failing is sent back.
