---
kind: steering
roles: [any]
fields: [n, reasoning]
when: The role's last answer_after_cuts reads in a row each thought until thinking_budget cut it; sent after the last read's observation, and the turn after offers no tools. Not sent to a role that can write and has written, whose run ends there (stop answer_after_cuts) for its caller to ask the answer (package-json-only, AIR-55, 2026-11-06).
governs:
  answer.when: now
  reasoning: continue
why: |
  AIR-54, AIR-55 and AIR-59 (2026-11-04): the architect's package steps each thought to the budget, got the
  carried tail back, and began the design again ("I've been going in circles"), was cut before the answer and
  read once more, until the repeat guard or max_steps ended the drafting. A larger budget only made each lap
  longer (6,144 tokens: four minutes a step, the same loop). The whole of the last reasoning goes back, and the
  next turn is the answer.
---
Your thinking was cut at its budget on your last {n} turns, and each began the same argument again. All of your reasoning on the last turn:
{reasoning}

No more tools: decide what is still open and give your final answer now, complete, from this and from what you have already read.
