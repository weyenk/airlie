---
kind: rule
roles: [tester]
governs:
  critic.reads: tests-against-the-proof
  critic.rewrites: never
why: |
  The tester critic's one turn on a layer's package (P2 of the planning line, 2026-10-24; D9). The architect
  writes the layer's tests as code from the proof the three amigos agreed, and the tester, on the
  cross-family model, reads them before the mechanical checks run: AIR-22 layer 02's tests tested the wrong
  thing at the wrong level and no one read them against anything until the pre-code check bounced them. The
  critic judges "weakened" against the proof's `asserts` and objects with a claim and a suggestion; it never
  rewrites the architect's tests (anti-goal of the design: a critic that rewrites another role's part).
  On the first real run (airlie-sandbox, 2026-10-25) the test files were in the brief alone; Muse looked for
  them on disk, found none, and objected that every agreed test was missing, which sent the architect off the
  proof. Since 2026-10-26 the critic reads at a commit that holds them (`_with_tests`), and a tool-written
  draft's files are in its snapshot (the tool-written packages design, 2026-11-06), so the brief says where
  they are. An existing test the package changes in place is shown before and after (Noah, 2026-11-06):
  editing one is allowed, weakening one is the critic's to catch.
---
The package's test files are in this brief and in your checkout, at the paths shown: read them in the brief, and use your tools for the repository's other tests and code. Read these tests against the agreed proof and the repository's conventions: each agreed test is present, at its level, asserting what the plan says, with the state it named; nothing is weakened. A test the plan does not name is no objection, as long as it tests this layer's own code. An existing test the package changes is shown before and after: object when the change weakens what it checked. Answer with your verdict on proof alone.
