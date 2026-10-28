---
kind: rule
roles: [product]
fields: [seats, limit]
governs:
  specs.round.refused: over-the-consultation-cap
why: |
  Tells product which asks of a round were not run because the phase's total of story.specs.max_consultations
  ({limit}) was reached (P4, 2026-10-25). The rest of the round ran.
---
These asks were not run because the {limit} consultations the phase allows are used up: {seats}.
