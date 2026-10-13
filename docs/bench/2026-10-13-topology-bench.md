# Topology benchmark, 2026-10-13: single agent vs K=3 teams vs fork-when-stuck

`task bench:topology -- --topologies single,teams,fork --tasks lang_detect,size_parse,inventory_cli --repeats 3`
· base = Qwen3-Coder-30B-A3B-4bit · team loop = `native` · repo map on · engine restarted first · guards from the
same morning in place (`repetition_penalty 1.05`, coder `max_tokens 4096`, one tool call per step). `single` =
`teams` with `k: 1` and `lts.enabled: false`; `teams` = the build spec (K=3 + LTS heuristic); `fork` = design
2026-10-13 with `width 2, after_failures 2, child_steps 15, max_rounds 3, diversify hypotheses`. Raw:
`topology.json`. log_pipeline was dropped: 0 of 18 teams ever solved it, so it measures nothing about topology.

| topology | runs | mean wall s | mean wall on solved runs s | mean steps | mean forks | winner passed hidden tests |
|---|---|---|---|---|---|---|
| single | 9 | 233 | 39 | 26 | 0 | 5 of 9 |
| teams | 9 | 521 | 57 | 52 | 0 | 5 of 9 |
| fork | 9 | 163 | 43 | 32 | 1 | 2 of 9 |

Verdicts (design §9, against `single`): **teams** same pass rate, solved runs 46 % slower → NOT met.
**fork** pass rate 0.56 → 0.22 → NOT met.

| task | single | teams | fork |
|---|---|---|---|
| lang_detect | 3/3, 19–30 s | 2/3, 31–454 s | 1/3, 56–152 s |
| size_parse | 0/3, 428–689 s | 0/3, 965–1521 s | 0/3, 226–439 s |
| inventory_cli | 2/3, 61–178 s | 3/3, 48–53 s | 1/3, 29–151 s |

## Per run

| task | topology | rep | wall s | steps | branches (stop, steps, visible pass) | result |
|---|---|---|---|---|---|---|
| lang_detect | single | 0 | 22 | 9 | done | PASS |
| lang_detect | teams | 0 | 105 | 28 | team-1 done 7; two cancelled | PASS |
| lang_detect | fork | 0 | 76 | 14 | trunk fork_point 8; r1-1 done 3 (no pass); r1-2 repeated_action 3 → best_effort r1-1 | FAIL |
| lang_detect | single | 1 | 19 | 12 | done | PASS |
| lang_detect | teams | 1 | 454 | 53 | team-2 repeated_action 8; team-1 done 20; team-3 done 25 | FAIL |
| lang_detect | fork | 1 | 56 | 14 | trunk fork_point 5; r1-1 done 5 (pass) → visible_pass | PASS |
| lang_detect | single | 2 | 30 | 21 | done | PASS |
| lang_detect | teams | 2 | 31 | 14 | team-3 done 7; two cancelled | PASS |
| lang_detect | fork | 2 | 152 | 30 | trunk fork_point 10; r1-1 done 9 (no pass); r1-2 fork_point 11 → best_effort r1-1 | FAIL |
| size_parse | single | 0 | 689 | 40 | max_steps | FAIL |
| size_parse | teams | 0 | 1462 | 69 | done 13; repeated_action 29; repeated_action 27 | FAIL |
| size_parse | fork | 0 | 439 | 49 | 3 rounds, every child fork_point/max_steps | FAIL |
| size_parse | single | 1 | 428 | 31 | repeated_action | FAIL |
| size_parse | teams | 1 | 1521 | 112 | repeated_action 36 ×2; max_steps 40 | FAIL |
| size_parse | fork | 1 | 279 | 45 | trunk repeated_action 15; both children max_steps 15 | FAIL |
| size_parse | single | 2 | 609 | 40 | max_steps | FAIL |
| size_parse | teams | 2 | 965 | 120 | max_steps ×3 | FAIL |
| size_parse | fork | 2 | 226 | 49 | trunk fork_point 19; both children max_steps 15 | FAIL |
| inventory_cli | single | 0 | 61 | 21 | done | PASS |
| inventory_cli | teams | 0 | 51 | 20 | team-3 done 20; two cancelled | PASS |
| inventory_cli | fork | 0 | 29 | 22 | trunk fork_point 15; r1-2 done 6 (pass) → visible_pass | PASS |
| inventory_cli | single | 1 | 178 | 39 | done (hidden tests fail) | FAIL |
| inventory_cli | teams | 1 | 53 | 34 | team-1 done 18; two cancelled | PASS |
| inventory_cli | fork | 1 | 59 | 39 | trunk done 39, visible pass, no fork (hidden tests fail) | FAIL |
| inventory_cli | single | 2 | 64 | 23 | done | PASS |
| inventory_cli | teams | 2 | 48 | 19 | team-2 done 19; two cancelled | PASS |
| inventory_cli | fork | 2 | 151 | 27 | trunk done 27, visible pass, no fork (hidden tests fail) | FAIL |

## Reading

- **The single agent is the right default on this hardware.** It matches K=3 on pass rate at 45 % of the
  wall-clock and half the steps. K=3 buys pass@3 on inventory_cli (3/3 vs 2/3) and loses it on lang_detect
  (2/3 vs 3/3); with three repeats those are one run each. Where K=3 is slower it is slower by a lot: three
  teams share one decode budget and all three run to `max_steps` when the task is hard.
- **Fork-when-stuck, as configured, is worse than doing nothing**, and the per-run rows say why:
  1. The trigger fires too early. Every trunk forked between steps 5 and 19, at the second failing `run_tests`,
     which is normal iteration (tests written, first fix not enough). The single agent that was left alone
     finished lang_detect in 9–21 steps.
  2. Best-effort resolution picks children that quit. In both lang_detect failures the chosen child was one that
     said "done" after 3–9 steps without a passing test run, because nobody had parseable test output; the run
     then ended on a `done` with no visible pass. A child that declares done without passing should not end the
     run.
  3. `child_steps 15` is less than the single agent typically needs after a fork point; both size_parse
     children hit `max_steps 15` every time.
  The two inventory_cli fork failures never forked (trunk `done` with a visible pass, hidden tests failed): that
  is the same variance that cost the single agent one run, not a fork effect.
- **size_parse is beyond this model** under every topology (0 of 9 today, 0 of 13 overall). It stays in the set
  as the hard case, but any topology comparison on it is a comparison of how expensively the model fails: fork
  fails in 4–7 min, single in 7–11, teams in 16–25.
- **The guards worked.** No runaway generation and no cut-off tool call in 27 runs; the same three tasks took
  3 h for 14 runs in the morning pass and 2 h 15 for 27 here.

## Decision

`airlie.yaml` default is now the single agent: `topology.kind: teams`, `teams.k: 1`, `lts.enabled: false`.
The spec's configuration is three lines away (`teams.k: 3`, `lts.enabled: true`) and `fork` is `topology.kind:
fork`. Two of the fork mechanisms above were fixed right after this run (review of the branch, same day, commit
following this write-up): a branch that quits with "done" but never passed its tests is now a fork point, and
best-effort resolution ranks branches that ran tests and did more work ahead of quitters. The numbers in this
file are from before those fixes. A fork follow-up worth one 30-minute pass: `after_failures: 3`, `child_steps: 25`, and resolve rules that
never end the run on a child's `done` without a visible pass (continue the trunk instead).
