---
kind: rule
roles: [product]
governs:
  numbers.source: computed
  criteria.form: given-when-then
  criteria.names: what-differs
  criteria.refusal: new-phrase
  story.size: thin-slice
  criteria.subject: the-feature
  questions: assume
why: |
  2026-10-31 (roman, round 1 run 3): AC2 restated the numeral table and dropped V=5; the coder built that table,
  the whole-range test checked only the alphabet and a round trip (a table missing a row round-trips), and the
  story shipped 5 -> IVI: the bench's oracle failed 2 of 18.
  2026-10-30 (bundle 6): the number rule and the compute tool, after four invented facts in five roman runs
  (four characters, sixteen or more, thirteen, MMMDCLX malformed), each one line of Python to check.
  A criterion about the tests themselves (that the suite runs, that new tests are discovered) is proven by
  the gate every layer passes and by no test: on airlie-sandbox (2026-10-26) one such criterion became a test
  that shelled out to the test runner, failed its layer's gate, and later deadlocked the meeting's proof.
  How product writes acceptance criteria. No docstring in the code. Given/When/Then, the thinnest slice and
  stated assumptions since b34f992 (2026-10-14). "Each criterion names what differs" and the refusal
  sentence came with 44b022d (2026-10-17, AIR-5 layer 01: six 'fails validation' tests passed on the base,
  because an unknown tool name was already refused with a message listing every valid name).
  Roman, 2026-10-28: two of three runs carried an invented example that was wrong ('MCMXCIV' for 1984,
  'MMMDCCCLXXXIX' for 3999); the tests locked it and no code could pass. Invented computed examples are out.
  Reliability run 2, 2026-10-29: the rule in prose was skipped (the tester listed fourteen malformed inputs,
  'IIV' not among them), because the criteria were lists: a refusal or a range is now one whole-domain
  criterion and one exhaustive test.
  A/B run 1, 2026-10-29: the request said 1 to 3999; the exhaustive check over 'up to four characters'
  took in the empty string, and a criterion made '' the numeral for 0, widening the request. The request's
  range is kept exactly, and the exhaustive check starts at one character.
---
Write acceptance criteria as Given/When/Then, each one observable from outside the code and checkable by a test. Each criterion names what differs after the change from before it: the exact message, field, value or output the new behaviour produces. A criterion the code already meets for some other reason (an error it already raises for another cause) cannot be tested: for a refusal, quote the message or a phrase only the new message contains. Prefer the thinnest slice that delivers the request; put the rest in out_of_scope. State assumptions instead of asking questions. A value in a criterion is either the request's own or one you can stand behind: never invent a computed example (a numeral for a number, a conversion, a hash, a date) the request does not give, because a wrong one is locked into the tests and no code can then pass; state the rule instead. Where the request names a standard (Roman numerals, ISO dates, a known encoding), a criterion names it and does not restate its table or rules: a restated table that drops a row becomes the spec. The request's own range, names, signatures and inputs are kept exactly: never widen or narrow them (roman, 2026-10-29: the request said 1 to 3999 and a criterion made 0 valid). Where the request names a range, write one criterion that holds for every value in it (every integer in 1..3999 round-trips). Where it names inputs to refuse, write one criterion over all short inputs, never a list of examples: every string of one to four characters that is not exactly one of the valid outputs is refused (the valid outputs being what the conversion gives over the request's range), and the empty input is refused on its own. Every criterion is about the feature, never about the tests or the test gate (that the suite runs, that tests are found): the gate proves those for every layer, and no test can. Every number you state (a bound, a count, a maximum, a length, an example's value, a test's expected result) is one you checked with the compute tool first, or one the test computes itself; never one worked out in your head.
