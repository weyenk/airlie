# Fork-when-stuck topology — design

> **Status:** draft for Noah's review (2026-10-13). Nothing built yet.
> **Relation to the build spec:** a deliberate deviation from `docs/wiki/build-spec` 01 §2 / 04 §3 (K=3 always-parallel
> teams + LTS). Motivation and evidence: `docs/bench/2026-10-12-lts-bench-bigger-v2.md`. The spec's topology stays
> selectable (`topology.kind: teams`) and remains the default until this one beats it on the bench.

## 1. Problem

On one Mac the K=3 teams share a single decode budget, so parallel duplication buys no wall-clock, only pass@3,
and every team pays the same exploration cost (read the repo, find the tests, run them) that the others pay. In
12 benchmark runs sharing never raised the solve rate, and wall-clock was decided by whether anyone solved the
task at all. Two things we have never measured: a single agent with the whole budget, and parallelism spent only
where it has a reason to exist — at the moment a trajectory is stuck.

## 2. Goals and non-goals

Goals
- Spend parallel capacity only when a branch is stuck, on alternatives that share the stuck branch's full context
  (prompt history and working tree), so the engine's prefix cache serves the fork for almost free.
- Remove the per-run rediscovery of the repository with a deterministic repo map, for every topology.
- Measure topologies against the right baseline: one agent, no memory.
- Keep every seam: engine, tools, worktrees, observers, aggregator, events, bench, Taskfile.

Non-goals
- No learned component. No cross-team memory bank in this topology (LTS stays available in `teams`).
- No merging of code across branches: one branch wins, as today.
- No change to the AutoGen-vs-native team seam; both teams gain the same two capabilities (state snapshot, stop
  predicate) and nothing else.

## 3. The loop

```
trunk = branch("trunk", worktree from repo HEAD, fresh conversation)
round = 0
loop:
    result = run(trunk, stop_when = fork trigger)
    if result.stop_reason != "fork_point": break            # done | max_steps | token_budget | repeated | error
    round += 1; if round > fork.max_rounds: break
    base   = commit WIP on trunk's branch                    # git add -A; commit --allow-empty
    hyps   = diversify(trunk.state, result.last_test_output)   # W distinct hypotheses (one model call)
    kids   = [branch(f"r{round}-{i}", worktree from base, conversation = trunk.state + nudge(hyps[i]))
              for i in 1..fork.width]
    results = run all kids in parallel, each with stop_when = fork trigger and max_steps = fork.child_steps;
              the first kid whose run_tests passes cancels its siblings and keeps running to "done"
    winner = kid that passed, else best_effort(kids)           # §3.4
    trunk  = winner                                            # its worktree, branch and conversation carry on
report: teams = every branch; winner = trunk; winning_branch = trunk's git branch
```

### 3.1 Fork trigger
A branch is stuck when its last `fork.after_failures` (default 2) `run_tests` calls all failed. The first failure
is normal (tests written before code); the second says the fix did not work. Evaluated in the team loop after
every step through the new `TeamContext.stop_when` predicate, which the orchestrator supplies with its own
per-branch counter. A branch that hits the trigger returns `stop_reason="fork_point"` with its state snapshot and
the tail of the last test output. The trigger also fires on `repeated_action` (today's stuck signal) so a looping
branch forks instead of dying.

### 3.2 Snapshot and resume
`TeamResult.state: dict | None` and `TeamContext.initial_state: dict | None`. AutoGen: `agent.save_state()` /
`agent.load_state()` (both exist on `AssistantAgent` and its model context). Native: the message list. The child
starts with the parent's messages verbatim (prefix-cache hit on the whole history), then one appended user
message, the nudge: branch label, the failing test tail, the hypothesis to pursue, and "do not repeat the previous
approach". Children get the parent's git state through a worktree added at the WIP commit (`add_worktree(...,
start_point=sha)`), so files, including untracked ones, carry over.

### 3.3 Diversify (`fork.diversify`)
- `hypotheses` (default): one `judge`-role call on the small model with the goal, the last edit (the diff of the
  WIP commit against the previous fork base or HEAD) and the failing test tail: "List W distinct root-cause
  hypotheses, each with a different fix approach, one line each." Parsed as numbered lines; short or unparsable
  output falls back to `temperature`.
- `temperature`: identical nudge, children sample at `fork.temperatures[i]` (default `[0.3, 0.7]`), via a copy of
  the role with the sampling changed.

### 3.4 Resolve
- A child whose `run_tests` returns ok is the winner; the `ForkObserver` sees that step and cancels the siblings
  (same cancellation path as `stop_on_first_pass`). The winner keeps running to `done` so the final message exists.
- Nobody passes within `fork.child_steps` (default 15): best effort = the child with the fewest failing tests
  parsed from its last `run_tests` output (`N failed` for pytest; any other runner: no parse, fall through), then
  the fewest steps. It becomes the trunk and may fork again next round.
- `fork.max_rounds` (default 3) reached, or the trunk stops for a non-fork reason: the run ends; the hidden
  grading in `eval`/`bench` decides pass/fail as today.

### 3.5 Budgets
`teams.max_steps` bounds any single branch; `fork.child_steps` bounds a child before resolve; `fork.max_total_steps`
(default 120) bounds the whole run across branches; `teams.token_budget` applies per branch as today. The slot
scheduler is unchanged: at most `width` branches generate at once.

## 4. Repo map (all topologies)
`src/airlie/repo_map.py`: deterministic, no model call. Tree to depth 3 with sizes, test command, and for Python
files the top-level `def`/`class` names with signatures (via `ast`); other languages get the tree only. Capped at
`repo_map.max_chars` (default 6000), cached in `<state_dir>/repo_maps/<sha of tracked file list + mtimes>.md`.
Injected once, as the first user message before the goal (`_goal_message` gains a `preamble`). `repo_map.enabled`
defaults to true for every topology so the bench compares topologies, not exploration.

## 5. Configuration

```yaml
topology:
  kind: teams                 # teams (spec: K parallel teams + LTS) | fork (this design)
fork:
  width: 2                    # children per fork point
  after_failures: 2           # consecutive failing run_tests that mark a branch stuck
  child_steps: 15             # steps a child gets before resolve
  max_rounds: 3
  max_total_steps: 120
  diversify: hypotheses       # hypotheses | temperature
  hypothesis_role: judge
  temperatures: [0.3, 0.7]
repo_map: { enabled: true, max_chars: 6000, depth: 3 }
```
`teams.k`, `teams.max_steps`, `teams.token_budget`, `teams.role`, `teams.orchestrator` keep their meaning; `fork`
uses `teams.orchestrator` for its branches. `task run`, `task eval`, `task serve` and the bench read
`topology.kind`; `--topology fork` overrides it on the command line.

## 6. Code shape

| Piece | Change |
| --- | --- |
| `teams/protocol.py` | `StopReason` += `"fork_point"`; `TeamContext` += `initial_state`, `nudge`, `stop_when: Callable[[StepRecord], bool] \| None`, `preamble`; `TeamResult` += `state: dict \| None`, `last_test_output: str` |
| `teams/autogen_team.py`, `teams/native_team.py` | honour `initial_state` (load before the first turn; the nudge is the first task), `preamble`, evaluate `stop_when` after each `after_step`; return state on `fork_point` |
| `teams/worktree.py` | `add_worktree(..., start_point: str \| None)`; `commit_wip(path, msg) -> sha` |
| `topology/protocol.py` | `Topology` protocol: `execute_task(query, *, repo, test_cmd) -> RunReport`; `build_topology(cfg, engine, run_id)` |
| `topology/fork.py` | `ForkOrchestrator` (§3): branches, `ForkObserver` (tracks run_tests outcomes, fires sibling cancel), `diversify`, `best_effort`, report |
| `orchestrator.py` | `ParallelAgentTeamOrchestrator` unchanged except: `RunReport` moves to `obs/report.py` so both topologies share it; `_run_team` takes `preamble` |
| `repo_map.py` | §4 |
| `bench/topology_bench.py`, `task bench:topology` | rows per (task, topology, repeat): wall, steps, tokens, forks, rounds, winner graded; baseline `single` = `teams` with `k: 1`, `lts.enabled: false`; verdict = wall-clock vs `single` and pass rate not regressed. `bench:lts` unchanged |
| `cli.py`, `serve/app.py`, `Taskfile.yml` | topology selection, `--topology`, `bench:topology` |
| `prompts/roles/coder.md` | a short "Branch" paragraph: what a nudge is, do not repeat the failed approach |
| `prompts/roles/judge.md` | hypotheses instruction (used only by `diversify: hypotheses`) |

Events: `fork_point` (round, parent, reason, children, test tail), `fork_resolved` (round, winner, how:
`visible_pass` / `best_effort` / `none`), `branch_done` (every branch, same fields as `team_done`), `repo_map`
(chars, cached). `run_done` keeps its fields plus `forks`, `rounds`.

## 7. Error handling
- A child that errors (`engine_error`) is dropped from resolve; if every child errors the trunk resumes from its
  own snapshot and the run ends with that reason.
- WIP commit failure (nothing to commit is not a failure: `--allow-empty`) ends the run as `engine_error`-class
  `fork_error` in events; the trunk's result stands.
- `stop_when` and the observer never raise into the team loop; exceptions are emitted as events.
- Cancelled siblings are recorded as branches with `stop_reason="cancelled"` as today.

## 8. Testing
- Unit, `FakeEngine`, both teams: state round-trip (snapshot after two steps, resume continues with the same
  message count + nudge); `stop_when` returns `fork_point` with state and last test output; `preamble` is the
  first user message.
- Unit, `ForkOrchestrator` on a temp git repo with a test script that fails until a marker file exists: trunk
  forks after two failures, two children spawn from the WIP commit (file written by the trunk is present in
  both), the child whose fake replies write the marker wins, the sibling is cancelled, report names the winner
  and its branch, events carry `fork_point` and `fork_resolved(how=visible_pass)`; a second test with no passing
  child exercises `best_effort` and `max_rounds`; a third exercises `diversify` fallback on unparsable output.
- Unit, repo map: deterministic output for a fixture tree, cap honoured, cache hit on second call.
- Live: `task eval -- inventory_cli --topology fork`; `task bench:topology -- --topologies single,teams,fork
  --tasks inventory_cli,log_pipeline,lang_detect,size_parse --repeats 3`.

## 9. Success criterion
Against `single` on the four tasks, three repeats: fork does not lower the pass rate and either raises it or cuts
mean wall-clock on the solved runs by ≥25 % (the spec's number, applied to the right baseline). The bench also
reports `teams` so the three topologies sit in one table. If `single` wins outright, that is the answer and the
default becomes `teams` with `k: 1`.

## 10. Open decisions for Noah
1. Default `fork.width` 2 (recommended: two children keep decode throughput per branch high) vs 3.
2. `diversify: hypotheses` as default (one extra small-model call per fork) vs `temperature` (free, less targeted).
3. Whether `topology.kind` should flip to `fork` by default once the bench says so, or stay `teams` for spec
   conformance with `fork` opt-in.
