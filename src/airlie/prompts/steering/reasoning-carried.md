---
kind: steering
roles: [any]
fields: [tail]
when: A read's turn thought until thinking_budget cut it; sent after the read's observation, where it stays when the next is added.
governs:
  reasoning: continue
why: |
  What follows the observation of a read whose turn's thinking ran to thinking_budget (AIR-30). The engine
  keeps reasoning only in the step trace, and vllm-mlx's MLLM path rewrites the Qwen template so past assistant
  turns never render theirs, so the next turn began the same argument from zero, was cut again, and read one
  more file (AIR-28's coder, 2026-10-18: 10 of 21 steps re-derived the same three contradictions in its brief).
  The tail goes back as plain text. Each stays where it was sent: until 2026-10-24 the next carry dropped the
  one before it, and removing a message mid-history missed the engine's prefix cache, so the step after each
  carry re-prefilled the whole conversation (20-36 s against ~2 s; docs/bench/2026-10-23-performance-review.md).
---
Your reasoning on the last turn, cut at the budget:
{tail}

Do not start over: continue from here and decide.
