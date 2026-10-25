---
kind: rule
roles: [product, architect, tester]
governs:
  agreement.turn: speak-then-verdict
  agreement.objection: claim-and-suggestion
why: |
  How every turn of the story meeting is spoken (P2, 2026-10-24). A critic that rewrites another role's part
  is an anti-goal of the design: the other role's reading of its own part is lost and the meeting never
  converges, so an objection is a claim and a suggestion and only the owner changes the text. A revised part
  travels whole in its own JSON shape because the checker re-parses it, and the verdict comes last because
  it is the last JSON block of the turn that is read (Muse wraps its answer in prose and channel text).
---
Speak to the others by name. To change your own part, post it whole in its JSON shape. Object to another's part with a claim and a suggestion; never rewrite it. End every turn with your verdict.
