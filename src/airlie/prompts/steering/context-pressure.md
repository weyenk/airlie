---
kind: steering
roles: [any]
fields: [max_prompt, max_context]
when: A step takes the role's prompt to 75% of its max_context; said once per run.
why: |
  Added 2026-10-19 (9d6cdff, "Team loop: 75% nudge and stop at the role's max_context") as an inline
  f-string with no docstring: once a step takes the prompt to 75% of the role's max_context, the role is
  told, once, to stop reading and write from what it has; at max_context the run stops (context_full) or
  trims its oldest observations, per on_context_full. Moved to the hub 2026-10-22.
---
Your context is at {max_prompt} of {max_context} tokens. Stop reading: write from what you have.
