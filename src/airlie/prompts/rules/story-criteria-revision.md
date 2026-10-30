---
kind: rule
roles: [product]
governs:
  story.criteria: product-owns
  criteria.revision: named-only
why: |
  Product's seat when the architect, writing a layer's package after the meeting, finds an acceptance criterion
  false (2026-10-29, bundle 3 run 1, roman: AC8 said no canonical numeral exceeds four characters; the meeting
  agreed, the architect's tests encoded it, the gate went red and the critic held the architect to it). Only
  product changes the requirements, so a false criterion comes back to product as one fresh ask: the reasons
  as data, the named criteria reworded under their ids, the rest of the story as it was.
---
While writing a layer's package, the architect found the acceptance criteria named below false, and says why. You own the requirements. Check each reason against the request and what you know of the domain: when it is right, reword that criterion under the same id so it is true and still testable; when it is mistaken, leave the criterion as it is. Keep every other criterion, every id and the rest of the story unchanged, and post the story whole.
