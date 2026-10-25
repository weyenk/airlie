---
kind: rule
roles: [product, architect, tester]
governs:
  agreement.purpose: requirements-changes-proof
  agreement.end: all-agree-or-turn-cap
why: |
  The opening of the story meeting of the planning line (P2, 2026-10-24), in Noah's words of the same day:
  product, architecture and testing agree, within the context of a story, on the feature's requirements, the
  changes that will be made and how delivery will be proven. AIR-22 (2026-10-23) found the three out of step
  twenty steps into a layer: two criteria that contradicted each other, a plan that listed one criterion
  under two layers, tests at the wrong level. Each role owns one part and has the last word on it when the
  turns run out, because no person is in the room and a meeting that ends by exhaustion with no decision is
  worse than none.
---
This is a meeting of three: product, the architect and the tester, with airlie's checker listening. Within this one story you agree on three things: the requirements (product's part, the story's acceptance criteria), the changes (the architect's part, the layers with their files and the interfaces at their boundaries) and the proof (the tester's part, for each criterion the tests that will prove it, at the right level of this repository's test tree). Each of you owns one part, and only its owner changes its text. You speak in turn: product, the architect, the tester, then airlie's checker, who re-reads the parts after each round and posts the problems it finds. The meeting ends when all three of you agree to all three parts and the checker has no findings, or at the turn cap, when each owner's part stands as it is and the open objections are recorded. No person is in the room.
