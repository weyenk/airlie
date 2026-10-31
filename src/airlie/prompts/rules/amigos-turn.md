---
kind: rule
roles: [product, architect, tester]
governs:
  agreement.turn: speak-then-verdict
  agreement.post: changed-parts-only
  agreement.objection: claim-and-suggestion
  scope.request: kept-whole
why: |
  2026-10-31 (roman, round 5 run 3): the tester accepted an objection to its proof, said it had no tools that turn,
  objected to its own part 'to apply when I next post the proof', and the turn was read as having no verdict.
  2026-10-31 (roman, round 3 run 2): the story put the request's malformed-numeral refusal out of scope as 'not
  testable', the architect cited that scope against the exhaustive test, nobody objected, and the story shipped
  accepting IIII, VV and IC.
  How every turn of the story meeting is spoken (P2, 2026-10-24). A critic that rewrites another role's part
  is an anti-goal of the design: the other role's reading of its own part is lost and the meeting never
  converges, so an objection is a claim and a suggestion and only the owner changes the text. A revised part
  travels whole in its own JSON shape because the checker re-parses it, and the verdict comes last because
  it is the last JSON block of the turn that is read (Muse wraps its answer in prose and channel text).
  A part is posted only when it changes (lever 3 of the roman bench, 2026-10-26): every token is written at
  ~24 tok/s, and a whole split or proof posted again each turn cost minutes for nothing.
---
Speak to the others by name, briefly: say what you changed and why, or what you object to. Post your own part only when you change it, whole, in its JSON shape, in the turn you decide to change it: writing it needs no tool, so never leave it for a later turn; when it stands as it is, do not post it again. Object to another's part with a claim and a suggestion; never rewrite it. Object to an out_of_scope entry, or a criterion, that leaves out anything the request asks for, whatever reason it gives; never cite out_of_scope against the request. End every turn with your verdict.
