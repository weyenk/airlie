---
kind: rule
roles: [product]
governs:
  criteria.form: given-when-then
  criteria.names: what-differs
  criteria.refusal: new-phrase
  story.size: thin-slice
  criteria.subject: the-feature
  questions: assume
why: |
  A criterion about the tests themselves (that the suite runs, that new tests are discovered) is proven by
  the gate every layer passes and by no test: on airlie-sandbox (2026-10-26) one such criterion became a test
  that shelled out to the test runner, failed its layer's gate, and later deadlocked the meeting's proof.
  How product writes acceptance criteria. No docstring in the code. Given/When/Then, the thinnest slice and
  stated assumptions since b34f992 (2026-10-14). "Each criterion names what differs" and the refusal
  sentence came with 44b022d (2026-10-17, AIR-5 layer 01: six 'fails validation' tests passed on the base,
  because an unknown tool name was already refused with a message listing every valid name).
---
Write acceptance criteria as Given/When/Then, each one observable from outside the code and checkable by a test. Each criterion names what differs after the change from before it: the exact message, field, value or output the new behaviour produces. A criterion the code already meets for some other reason (an error it already raises for another cause) cannot be tested: for a refusal, quote the message or a phrase only the new message contains. Prefer the thinnest slice that delivers the request; put the rest in out_of_scope. State assumptions instead of asking questions. Every criterion is about the feature, never about the tests or the test gate (that the suite runs, that tests are found): the gate proves those for every layer, and no test can.
