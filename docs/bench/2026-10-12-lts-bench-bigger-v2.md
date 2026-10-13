# LTS benchmark on the bigger tasks, round 2 (background admission + ranked injection), 2026-10-12

`task bench:lts -- --tasks inventory_cli,log_pipeline --repeats 3` · K=3 AutoGen teams · base =
Qwen3-Coder-30B-A3B-4bit · `never` (memory-free) vs `heuristic` · commits 3ca431b + bb56e95 (admission in the
background, keys ranked per team, own keys excluded, top 2 values inlined once). Raw: `lts-bigger-v2.json`.

| task | controller | repeat | wall s | steps | s/step | prompt tok/step | admissions | retrievals | teams pass | stop |
|---|---|---|---|---|---|---|---|---|---|---|
| inventory_cli | never | 0 | 73 | 31 | 2.3 | 5596 | 0 | 0 | 1/3 | early stop |
| inventory_cli | heuristic | 0 | 617 | 124 | 5.0 | 2810 | 38 | 0 | 0/3 | max_steps ×3 |
| inventory_cli | never | 1 | 517 | 126 | 4.1 | 1431 | 0 | 0 | 0/3 | max_steps ×3 |
| inventory_cli | heuristic | 1 | 187 | 49 | 3.8 | 1005 | 13 | 0 | 0/3 | early stop (then hidden tests failed) |
| inventory_cli | never | 2 | 196 | 65 | 3.0 | 580 | 0 | 0 | 1/3 | early stop |
| inventory_cli | heuristic | 2 | 45 | 18 | 2.5 | 4008 | 12 | 0 | 1/3 | early stop |
| log_pipeline | never | 0 | 402 | 126 | 3.2 | 5926 | 0 | 0 | 0/3 | max_steps ×3 |
| log_pipeline | heuristic | 0 | 1365 | 123 | 11.1 | 10561 | 64 | 0 | 0/3 | max_steps ×3 |
| log_pipeline | never | 1 | 1229 | 132 | 9.3 | 1853 | 0 | 0 | 0/3 | max_steps ×3 |
| log_pipeline | heuristic | 1 | 861 | 124 | 6.9 | 4553 | 67 | 0 | 0/3 | max_steps ×3 |
| log_pipeline | never | 2 | 1104 | 121 | 9.1 | 4208 | 0 | 0 | 0/3 | max_steps ×3 |
| log_pipeline | heuristic | 2 | 1199 | 131 | 9.2 | 1208 | 69 | 0 | 0/3 | max_steps ×3 |

| controller | runs | mean wall s | mean steps | s/step (pooled) | prompt tok/step (pooled) | mean admissions | retrievals | pass rate | team pass rate |
|---|---|---|---|---|---|---|---|---|---|
| never | 6 | 586.9 | 100.2 | 5.86 | 3148 | 0 | 0 | 0.333 | 0.111 |
| heuristic | 6 | 712.5 | 94.8 | 7.51 | 4379 | 43.8 | 0 | 0.167 | 0.056 |

Verdict: wall-clock 587 s → 713 s (−21 %), pass rate 0.33 → 0.17; **criterion NOT met**. No `admission_error`
events in any run.

## What changed since round 1, and what did not

- **Admission is no longer on the critical path.** Round 1's 844 s `log_pipeline` outlier came from waiting on the
  summarizer after every step. That wait is gone (zero errors from the background path), yet per-step cost is
  still 28 % higher with LTS on: 7.5 s vs 5.9 s per step, with 39 % more prompt tokens per step (4379 vs 3148).
  The remaining cost is the injected block itself (up to 12 keys plus two 1500-char values per step, resent and
  prefix-cache-missed because it is a new user message each turn) and the summarizer's small-model calls sharing
  the engine's batch with the coders.
- **Still zero `retrieve_value` calls** across 12 runs and 263 admissions. With the top values inlined, the
  obvious hits need no call, so a zero here no longer proves the keys are ignored; it does mean the learned
  controller's usage signal (`used_later`) is still empty.
- **The benchmark is dominated by whether anyone solves the task, not by memory.** Wall-clock is bimodal: a run
  where one team passes stops early at 18–65 steps (45–200 s); a run where nobody passes runs every team to
  `max_steps` (120–130 steps, 400–1400 s). `log_pipeline` was solved by 0 of 18 teams under both controllers;
  `inventory_cli` by 3 of 18. Three repeats cannot separate a memory effect from that coin flip, and the pass-rate
  "regression" (2 of 6 vs 1 of 6 runs) is one run.

## Reading

On these tasks with this coder model, LTS has not paid for itself: sharing does not raise the solve rate, and the
injection costs ~28 % per step. The honest statement of the spec 04 §4 criterion is "not met; not measurable at
this solve rate". Options, in the order I would take them:

1. Make the tasks solvable before measuring memory: a stronger coder (`llama70b` / `qwen25_32b` are in the catalog;
   a larger Qwen3-Coder is one catalog line) or easier bigger tasks, so runs end on `done` and wall-clock measures
   work, not `max_steps`.
2. Cut the injection cost to near zero when it is not used: `inline_top: 1`, `inline_chars: 600`, `max_keys: 6`,
   and inject only when the ranked set changed since the team's last step.
3. Move to the spec's own benchmark (M4b, GAIA/AssistantBench): web facts and parsed data are the kind of value a
   peer team can reuse verbatim, unlike a half-finished edit in someone else's worktree.
