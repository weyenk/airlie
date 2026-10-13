# LTS benchmark on the bigger tasks, 2026-10-12 (one sample per cell)

`task bench:lts -- --tasks inventory_cli,log_pipeline --repeats 1` · K=3 AutoGen teams · base = Qwen3-Coder-30B-A3B-4bit
· `never` (memory-free) vs `heuristic`. Raw: `lts-bigger.json`.

```
inventory_cli controller=never repeat=0 run=20261012-180534-40a3
  wall 387s, steps 96, admissions 0, retrievals 0, teams passed 1/3
inventory_cli controller=heuristic repeat=0 run=20261012-181203-dbec
  wall 223s, steps 73, admissions 23, retrievals 0, teams passed 1/3
log_pipeline controller=never repeat=0 run=20261012-181547-9437
  wall 210s, steps 81, admissions 0, retrievals 0, teams passed 0/3
log_pipeline controller=heuristic repeat=0 run=20261012-181918-fec9
  wall 844s, steps 115, admissions 62, retrievals 0, teams passed 0/3
```

| controller | runs | mean_wall_s | mean_steps | mean_admissions | mean_retrievals | pass_rate | team_pass_rate |
|---|---|---|---|---|---|---|---|
| never | 2 | 298.4 | 88.5 | 0 | 0 | 0.5 | 0.167 |
| heuristic | 2 | 533.1 | 94 | 42.5 | 0 | 0.5 | 0.167 |

Verdict: wall-clock 298 s → 533 s (−79 %), pass rate unchanged; **criterion NOT met**.

## What the run says

- **Still zero cross-team retrievals**, now on tasks with a real codebase to explore. Keys are injected (23–62
  admissions per run) but no team ever called `retrieve_value`. Until that changes, LTS cannot help.
- **LTS as wired is pure overhead.** `after_step` awaits the summarizer (a small-model call) and the controller
  before the team's next turn, and every injected key block grows the prompt. On `log_pipeline` that turned 210 s
  into 844 s. The spec puts admission after step completion but nothing requires it on the critical path.
- The tasks are hard for this model: `log_pipeline` 0/3 under both controllers, `inventory_cli` 1/3.

## Next changes implied

1. Move summarize + admit off the critical path (background task per step, bounded concurrency) so LTS costs
   nothing in wall-clock when it is not used.
2. Make retrieval likelier: inject only the few most relevant keys (not the last 32), and consider inlining the
   values of the top 1–3 keys so no tool call is needed for the obvious hits.
3. Repeats ≥ 5 before trusting any delta; single runs vary by minutes.
