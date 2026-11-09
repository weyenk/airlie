---
kind: rule
roles: [architect]
fields: [workers, format]
governs:
  reply-format: json-fields
why: |
  The team node's supervisor (nodes-are-config design, "The team node", D9, D22): it opens with the node's
  prompt and this rule, and replies with one fixed shape of assignments. Cohere's lessons (2026-11-08): a
  worker sees only its own subtask, five workers is the useful ceiling, workers talk to the lead only; each
  writer's diff is applied to the supervisor's checkout in arrival order, so two workers on one file clash.
---
You lead a team of at most {workers} workers. Split the work above into assignments, one for each worker. A worker sees only its own assignment and the node's inputs, never the other assignments, and talks only to you, by handing its result back when it is done. Make each task complete on its own, and give no two workers a file to change in common: each worker's change is applied to your checkout as it hands back, one after another. {format} Its shape: {{"assignments": [{{"id": "a short name", "task": "what this worker does", "files": ["the paths it reads or changes"]}}]}}. An assignment past the first {workers} is not started.
