---
kind: rule
roles: [architect]
fields: [id, status, waiting, not_started, format, result]
governs:
  reply-format: json-fields
why: |
  The team node's supervisor folds each worker's result in as it arrives, in finish order, without waiting
  for the rest (nodes-are-config design, "The team node", D9, D22; Cohere 2026-11-08: a lead that does not
  block cut wall-clock about a fifth). Its reply to the last hand-back is the node's synthesis, parsed by the
  node's output; a diff that does not apply comes back as a failed hand-back.
---
Worker {id} has handed back its assignment. Status: {status} (applied: its change is now committed in your checkout; answered: its answer is below; failed: nothing of it reached your checkout, and the reason is below). Still working: {waiting}. Not started, over the team's cap: {not_started}.

Fold this result into your running synthesis. While any worker is still working, reply with a short note of what you folded in. When none is, this is your final reply: give the whole synthesis. {format}

The worker's result:

{result}
