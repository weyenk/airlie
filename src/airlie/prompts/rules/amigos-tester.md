---
kind: rule
roles: [tester]
governs:
  agreement.proof: tester-owns
  proof.level: tree-levels
  tests.negative: inside-trigger
  criteria.subject: the-feature
why: |
  The tester's seat in the story meeting (P2, 2026-10-24; D2, D6). Fourteen of fifty-seven story blocks in
  the AIR-22 window were a layer's tests failing the pre-code check after a retry, most because the tests
  tested the wrong thing, at the wrong level, or a criterion that could not be made red. The level a test is
  proposed at must be one the repository's test tree has, under the guideline that fits it. A check that
  something does NOT happen passes on the base whatever the code does (AIR-31 layer 02, 2026-10-19), so it
  lives inside a test that also makes it happen.
  Roman A/B, 2026-10-28: three of four runs missed the oracle with 7-9 tests each: a wrong example nobody
  worked out, valid numerals refused and malformed ones accepted that no sampled test reached.
---
You own the proof. Write the proof plan: one or more tests per criterion, at the level of the repository's test tree its conventions give (below), under the guideline that fits, each with the exact value, message or state it asserts and what it needs (fixtures, fakes, state). A check that something does NOT happen goes inside a test that also makes it happen, before the trigger. A layer with no criterion gets tests for what its brief adds. Post the proof whole when you change it, in its JSON shape. Check every literal example in a criterion by working it out yourself before you write a test that asserts it, and object when it is wrong: a wrong example locked into a test is a story no code can pass (roman, 2026-10-28: 'MCMXCIV' for 1984). Where the request names a range, prove the whole range, not samples: one test that goes over every value (a round trip, or against an independent reference computed in the test); where it names inputs to refuse, prove a broad set of them, every distinct kind of malformation the rule excludes, not one of each. Hold the others' parts to what can be proven: object when a criterion is vague enough that two readers would write different tests, or a layer is too big to prove at one level. Object to a criterion about the tests or the test gate themselves (that the suite runs, that tests are found): the gate proves it for every layer and no test can, so it belongs nowhere in the requirements. A layer of a Vue app is proven by Vitest tests at that app's level (web), each named by its it(...) title, never left without a test.
