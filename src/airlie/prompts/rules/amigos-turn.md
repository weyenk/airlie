---
kind: rule
roles: [product, architect, tester]
governs:
  agreement.turn: speak-then-verdict
  agreement.post: changed-parts-only
  agreement.objection: claim-and-suggestion
why: |
  How every turn of the story meeting is spoken (P2, 2026-10-24). A critic that rewrites another role's part
  is an anti-goal of the design: the other role's reading of its own part is lost and the meeting never
  converges, so an objection is a claim and a suggestion and only the owner changes the text. A revised part
  travels whole in its own JSON shape because the checker re-parses it, and the verdict comes last because
  it is the last JSON block of the turn that is read (Muse wraps its answer in prose and channel text).
  A part is posted only when it changes (lever 3 of the roman bench, 2026-10-26): every token is written at
  ~24 tok/s, and a whole split or proof posted again each turn cost minutes for nothing.
---
Speak to the others by name, briefly: say what you changed and why, or what you object to. Post your own part only when you change it, whole, in its JSON shape; when it stands as it is, do not post it again. Object to another's part with a claim and a suggestion; never rewrite it. End every turn with your verdict.
