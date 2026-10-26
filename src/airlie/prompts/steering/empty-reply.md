---
kind: steering
roles: [any]
when: A role's reply through the AutoGen client has no answer in it (empty, or reasoning only); said once.
governs:
  answer.when: now
why: |
  What a role is told once when its reply is empty or holds only its reasoning (run 6 on airlie-sandbox,
  2026-10-26: Muse, the tester in the story meeting, ended two turns with nothing after its `to=user` header,
  one of them after 2,236 tokens of reasoning, and the meeting lost its proof plan both times).
---
Your reply had no answer in it, only your reasoning or nothing. Write your answer now, in your reply.
