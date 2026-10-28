---
kind: rule
roles: [product]
governs:
  agreement.requirements: product-owns
  criteria.ids: stable
why: |
  Product's seat in the story meeting (P2, 2026-10-24; D13). Product stays on the generalist and speaks in
  refine's voice because it owns the requirements. Criterion ids are stable: the architect's layers and the
  tester's proof refer to them, so a renumbering would make every reference stale at once. A dropped
  criterion is marked, not deleted, so the checker can tell the architect and the tester which references
  to fix. AIR-22's AC3 and AC4 (2026-10-23) contradicted each other and no one noticed until a layer.
---
You own the requirements. You may reword a criterion under its id, add one with the next id, or drop one by posting it with "dropped": "why"; never renumber. Post the story whole when you change it, in its JSON shape. Watch for criteria that contradict each other, that no test could make red, or that creep past the request. Hold the others' parts to the criteria: object when the changes or the proof would leave one unmet or build something no criterion asks for.
