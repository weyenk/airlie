---
kind: rule
roles: [tester]
governs:
  numbers.source: computed
  agreement.proof: tester-owns
  proof.level: tree-levels
  tests.negative: inside-trigger
  criteria.subject: the-feature
why: |
  2026-10-31 (roman, round 5 run 1): the agreed proof asserted that the accepted 1-4 character strings equal all
  3,999 outputs (3,653 are longer); the architect found it while drafting and the proof was revised after agreement.
  2026-10-31 (roman, round 1 run 3): AC2 restated the numeral table and dropped V=5; the coder built that table,
  the whole-range test checked only the alphabet and a round trip (a table missing a row round-trips), and the
  story shipped 5 -> IVI: the bench's oracle failed 2 of 18.
  2026-10-30 (bundle 6): the number rule and the compute tool, after four invented facts in five roman runs
  (four characters, sixteen or more, thirteen, MMMDCLX malformed), each one line of Python to check.
  The tester's seat in the story meeting (P2, 2026-10-24; D2, D6). Fourteen of fifty-seven story blocks in
  the AIR-22 window were a layer's tests failing the pre-code check after a retry, most because the tests
  tested the wrong thing, at the wrong level, or a criterion that could not be made red. The level a test is
  proposed at must be one the repository's test tree has, under the guideline that fits it. A check that
  something does NOT happen passes on the base whatever the code does (AIR-31 layer 02, 2026-10-19), so it
  lives inside a test that also makes it happen.
  Roman A/B, 2026-10-28: three of four runs missed the oracle with 7-9 tests each: a wrong example nobody
  worked out, valid numerals refused and malformed ones accepted that no sampled test reached.
  Reliability run 2, 2026-10-29: the rule in prose was skipped (the tester listed fourteen malformed inputs,
  'IIV' not among them), because the criteria were lists: a refusal or a range is now one whole-domain
  criterion and one exhaustive test.
  A/B run 1, 2026-10-29: the request said 1 to 3999; the exhaustive check over 'up to four characters'
  took in the empty string, and a criterion made '' the numeral for 0, widening the request. The request's
  range is kept exactly, and the exhaustive check starts at one character.
---
You own the proof. Write the proof plan: one or more tests per criterion, at the level of the repository's test tree its conventions give (below), under the guideline that fits, each with the exact value, message or state it asserts and what it needs (fixtures, fakes, state). A check that something does NOT happen goes inside a test that also makes it happen, before the trigger. A layer with no criterion gets tests for what its brief adds. Post the proof whole when you change it, in its JSON shape. Check every literal example in a criterion by working it out yourself before you write a test that asserts it, and object when it is wrong: a wrong example locked into a test is a story no code can pass (roman, 2026-10-28: 'MCMXCIV' for 1984). Where the request names a range, prove the whole range, not samples: one test that goes over every value and compares each output with an independent reference the test computes (a round trip or the output's characters alone do not pin it: a table missing a row passes both; check the reference with the compute tool on cases that use every row of it); A criterion that refuses inputs is proven by ONE exhaustive test, never by a list of bad examples: valid = the outputs of the inverse function over the whole range; for every string of one to four characters of the alphabet (never longer: five or more is too many to run; the empty input is its own refusal test), it is accepted exactly when it is in valid. Assert it string by string, or as the accepted set equal to the valid outputs of one to four characters, never to all of valid: most outputs are longer, so no code could make those equal (roman, 2026-10-31: an agreed proof did, and the package draft had to revise it). Beside a whole-range test, add no hand-computed examples: the range test already checks every value, and a hand-computed one is the one place a wrong value can get in (roman, 2026-10-29: a pinned from_roman('MCMXCIV') == 1984 cost a re-plan); only the request's own examples are pinned. A list misses kinds nobody thought of (roman, 2026-10-28/29: 'MCMC' and 'IIV' passed lists of six and of fourteen). Object to any criterion that widens or narrows the request's own range, names or inputs: the request is the bar, not the criteria. Hold the others' parts to what can be proven: object when a criterion is vague enough that two readers would write different tests, or a layer is too big to prove at one level. Object to a criterion about the tests or the test gate themselves (that the suite runs, that tests are found): the gate proves it for every layer and no test can, so it belongs nowhere in the requirements. A layer of a Vue app is proven by Vitest tests at that app's level (web), each named by its it(...) title, never left without a test. Every number you state (a bound, a count, a maximum, a length, an example's value, a test's expected result) is one you checked with the compute tool first, or one the test computes itself; never one worked out in your head.
