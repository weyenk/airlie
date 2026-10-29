---
kind: rule
roles: [architect]
governs:
  tests.criteria: each
  tests.fail-first: required
  tests.negative: inside-trigger
why: |
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
One or more tests per criterion this layer owns (for a layer with none, per behaviour its brief adds). A check that something does NOT happen lives inside a test that also makes it happen, before the trigger. Where the story names a range, one test goes over every value of it (a round trip, or against a reference the test computes independently); where it names inputs to refuse, one exhaustive test and never a list: valid = the outputs of the inverse function over the whole range; every string of up to four characters of the alphabet (never longer) is accepted exactly when it is in valid. Beside a whole-range test, assert no hand-computed example: pin only the request's own. A literal value you assert is one you have worked out yourself: when a criterion's example is wrong, say so rather than lock it into a test. A test file you add to must keep every test that was there; a reformat is not a change. Every name a test imports from this repository exists already or is one your interfaces declare. A test of a Vue layer is a Vitest test in that app's test tree (a `*.test.ts` beside the code it tests, in the style of the nearest one, which you read first), named by its literal it(...) title inside its describe, with an expect for what users see.
