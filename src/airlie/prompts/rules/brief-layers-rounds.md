---
kind: rule
roles: [architect]
fields: [round_steps]
why: |
  In the layers brief when the architect plans in rounds (story.plan_round_steps); the code puts a space and
  REFINE_FIELD (the plan's `refine` object, an output format kept beside its parser) after it. No docstring
  in the code; added with b3361a9 (2026-10-17: AIR-5's first planning round ended at max_steps, the
  architect calling another tool on the turn that offers none).
---
You will be asked for your plan after every {round_steps} steps, whatever you have read so far, with no tools on that turn: read the files that matter first, and be ready to write the JSON plan then. You may refine it in later rounds.
