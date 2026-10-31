---
kind: rule
roles: [architect]
governs:
  numbers.source: computed
  tests.criteria: each
  tests.fail-first: required
  tests.negative: inside-trigger
why: |
  2026-10-31 (roman, round 5 run 1): the agreed proof asserted that the accepted 1-4 character strings equal all
  3,999 outputs (3,653 are longer); the architect found it while drafting and the proof was revised after agreement.
  2026-10-31 (roman, round 1 run 3): AC2 restated the numeral table and dropped V=5; the coder built that table,
  the whole-range test checked only the alphabet and a round trip (a table missing a row round-trips), and the
  story shipped 5 -> IVI: the bench's oracle failed 2 of 18.
  2026-10-30 (bundle 6): the number rule and the compute tool, after four invented facts in five roman runs
  (four characters, sixteen or more, thirteen, MMMDCLX malformed), each one line of Python to check.
  A Vue layer's tests are Vitest tests (the web-tests design, 2026-10-27): the wiki benchmark's run B could not
  plan its Vue layers while only pytest modules counted as tests.
  How the architect's layer tests are written (P1, 2026-10-24); the rules the tester was given in
  `pre-code-check` and `checks-fix-before-code`, said before the first try instead of after the first
  rejection. A check that something does NOT happen passes on the base whatever the code does (AIR-31 layer
  02, 2026-10-19). A test module reproduced with a test missing, or reformatted, reads to the check as a new
  or changed test (AIR-9 layer 05, 2026-10-22). An import of a name no one defines is a collection error
  the coder can only meet by guessing the name.
  Roman A/B, 2026-10-28: thin packages (7-9 tests) let valid numerals be refused and malformed ones through;
  a range and a refusal rule are proven across, not by samples.
---
One or more tests per criterion this layer owns (for a layer with none, per behaviour its brief adds). A check that something does NOT happen lives inside a test that also makes it happen, before the trigger. Where the story names a range, one test goes over every value of it and compares each output with a reference the test computes independently (a round trip or the output's characters alone do not pin it: a table missing a row passes both; check the reference with the compute tool on cases that use every row of it); where it names inputs to refuse, one exhaustive test and never a list: valid = the outputs of the inverse function over the whole range; every string of one to four characters of the alphabet (never longer; the empty input is its own refusal test) is accepted exactly when it is in valid. Assert it string by string, or as the accepted set equal to the valid outputs of one to four characters, never to all of valid: most outputs are longer, so no code could make those equal (roman, 2026-10-31: an agreed proof did, and the package draft had to revise it). Beside a whole-range test, assert no hand-computed example: pin only the request's own. A literal value you assert is one you have worked out yourself: when a criterion's example is wrong, say so rather than lock it into a test. A test file you add to must keep every test that was there; a reformat is not a change. Every name a test imports from this repository exists already or is one your interfaces declare. A test of a Vue layer is a Vitest test in that app's test tree (a `*.test.ts` beside the code it tests, in the style of the nearest one, which you read first), named by its literal it(...) title inside its describe, with an expect for what users see. Every number you state (a bound, a count, a maximum, a length, an example's value, a test's expected result) is one you checked with the compute tool first, or one the test computes itself; never one worked out in your head.
