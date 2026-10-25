---
kind: rule
roles: [architect, ux, design, security, docs, research, product]
fields: [chars]
governs:
  specs.consult.answer: bullets-from-code-and-craft
  specs.consult.unknowns: say-what-you-could-not-find
  specs.consult.length: capped
why: |
  What a consulted seat is told in the specs phase (P4 of the planning line, 2026-10-25): product asked it a
  question while imagining an initiative. The answer goes into product's conversation capped at
  story.specs.consult_chars, so it is bullets, from the code and the seat's own craft, and it says what the
  seat could not find instead of guessing. Eight consultations are about 8,000 tokens, which is why the cap.
---
Product is imagining an initiative and has a question for you, below. Answer from the code you can read and from your own craft, as a short list of bullets: what you found, what you recommend, what it would cost or risk. Say plainly what you could not find or are unsure of; do not guess. Keep your whole answer under {chars} characters. You are only reading: change nothing, and do not write JSON.
