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
  The package's test files are not in the repository yet (the coder writes them after the check): on the
  first real run (airlie-sandbox, 2026-10-25) Muse looked for them on disk, found none, and objected that
  every agreed test was missing, which sent the architect off the proof.
---
The package's test files are in this brief and nowhere else: they are not in the repository yet, so never look for them there and never object that one is missing because the repository lacks it. Use your tools only for the repository's existing tests and code. Read these tests against the agreed proof and the repository's conventions: each agreed test is present, at its level, asserting what the plan says, with the state it named; nothing is weakened. A test the plan does not name is no objection, as long as it tests this layer's own code. Answer with your verdict on proof alone.
