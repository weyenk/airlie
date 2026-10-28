# Planning line performance plan

Status: draft for Noah's review, 2026-10-27. Nothing here is built yet, apart from lever P1, which is committed but not
yet measured.

## Goal

Make a story run faster without lowering its quality. Every lever is a config flag that is off until it passes its gate.
Once a lever passes, it is on by default.

Where the time goes today: decode, meaning the model writing, is most of every run. It runs at about 23 tok/s on a
fresh context and falls to 13–16 tok/s at the context sizes the drafting conversation reaches. Most of the architect's
calls are file reads that an earlier step already made (reads were 27 of 37 package calls in run5). So the plan cuts
calls first, then cuts context and output, and only then makes the writing itself faster.

## The quality bar (every lever, every run)

A lever passes only when all of these hold against the current baseline. "Baseline" means the last passing
configuration, not the original pipeline.

| Check | Roman (`task bench:story`) | Wiki |
|---|---|---|
| Finishes | Published, no block | Published, no block |
| Oracle | 18/18 | Wiki oracle all green (step P0) |
| Mutation score on the changed code | ≥ baseline (49/50) | ≥ baseline |
| Tests / asserts | ≥ baseline (28/41) | ≥ baseline |
| Gate | Green on every layer, first try or within baseline retries | Same |
| Critic objections and re-plans | ≤ baseline + 1 | ≤ baseline + 1 |
| Reviewer (Muse) verdict on each PR | No worse than baseline | No worse than baseline |
| Repository test gate (AGENTS.md) | Green | Green |

**Speed bar:** median wall clock over 2 runs must beat the baseline by more than the noise band. The noise band is
measured in P0, and I expect it to be about 10%. A lever that is faster but misses any quality check does not ship as
it is. It goes back to Noah with what failed, rather than being dropped quietly.

## P0 — Measure before changing anything

1. Let wiki run B finish and record it. That is the lever 1 + 2 + 3 wiki baseline.
2. **Wiki oracle.** The wiki has no oracle yet, so add `evals/stories/wiki_oracle.py`. It checks that:
   - the wiki route renders
   - every `docs/wiki` page is reachable
   - pages have infobox, contents and see-also sections
   - internal links resolve
   - search finds a known page
3. **Bench counters.** Add these per run to the story bench record:
   - calls per role
   - read-tool calls per role
   - output tokens per role (thinking and answer separately)
   - decode tok/s, bucketed by context size (<16k, 16–48k, >48k)
   - idle time (no model call in flight)
4. **Noise band.** Run roman twice on the same configuration and record how far apart the two runs are.

Gate: the counters appear in `story-*.json`, two roman runs are recorded, and the wiki oracle passes on run B's
published PRs (or names what they miss).

**P0 results (2026-10-27).**

**Counters:** all in place. Idle time is now 1.8 minutes of 48, so nearly all of a run is model time.

**Wiki baseline:** run B, recorded in `story-wiki-baseline.json`.
- Gate green; oracle 4/6.
- Refine narrowed the request to "a read-only Wiki tab", so the two Wikipedia-like checks fail.

**Noise band:** four roman runs on today's build.

| Run | Outcome | Wall time | Notes |
|---|---|---|---|
| 1 | Blocked | — | Malformed layer JSON, told only "no JSON object" (fixed in PR 175) |
| 2 | Blocked | — | Proof posted by the wrong role (fixed in PR 175) |
| 3 | Clean | 48.0 min | 1 layer |
| 4 | Clean | 75.0 min | 2 layers, 2 packages sent back on proof mismatches |

Both clean runs had oracle 18/18 and mutation all killed. The spread between them is **56%**. That is far beyond the speed bar's assumed 10%, so a single run per lever can't show a 10–30% gain.

The spread comes from two places:
- **How many layers the plan makes**, for one small module.
- **`proof_mismatch` rework**, where a package covers a test the agreed proof does not name: about 11 minutes a time.

Before the levers are screened, the noise has to come down, by cutting those two sources or by running each configuration about 4 times. Noah's call.

**Noise band after cutting the rework (2026-10-28).** Noah chose to cut the noise first.

PR 175 now also has these fixes:
- a test beyond the proof is kept and reported, not sent back
- a unittest name written `Class.test_x` is read as `Class::test_x`

| Run | Outcome | Wall time | Layers | Oracle | Mutation | What happened |
|---|---|---|---|---|---|---|
| 5 | Clean | 51.7 min | 2 | 18/18 | 36/38 | A 27-minute meeting that ended with an owner deciding |
| 6 | Blocked | — | — | — | — | Dotted test names (now fixed) |
| 7 | Clean | 57.3 min | 1 | 18/18 | 49/57 | One package sent back |
| 8 | Clean | 72.3 min | 1 | 18/18 | 91/91 | The coder's first build went red, so the layer was re-planned and built again (about 30 min) |

The three clean runs on the current build average **60.4 minutes**, a spread of ±17%.

Each of the original sources of variance (the meeting's length, a package sent back, a gate re-plan) is now a model's own mistake, not a pipeline rule, so it can't be removed by a check.

**What that means for screening:**
- A lever that saves under about 20% can't be told apart from noise on 1–2 runs.
- So either run each configuration 3 times, or bundle the levers (strategy C), whose combined effect should be well past 20%.
- Quality held on every clean run: the oracle was 18/18 each time.
- Mutation varied from 64% to 100% of mutants killed. Roman's red check never runs, because its gate is unittest and the check is pytest-only, so a test that would pass without the code is never caught. Fixing that is a quality item, separate from speed.

## The levers, in order

Each lever has its own flag and its own measured run (roman to screen it, wiki to confirm it), and passes the quality
bar above plus the lever's own check listed below.

### P1 — Reflection turns know no tools are left (built: 209dff5)

- **What:** a tool-less reflection request now says so, so meeting turns stop ending in "let me read…" with no verdict.
- **Expected:** meeting 15–25% shorter on wiki; little change on roman.
- **Own check:** `verdict_missing` events in the meeting = 0.

### P2 — Story dossier

- **What:** the survey writes a dossier: the files that matter, key excerpts (capped, as lever 2's sources are), and
  its notes. Planning, every meeting role, drafting, the critic, re-plans and the coder get it in their brief. A role
  may still read a file the dossier lacks, and that read is appended to the dossier for later steps.
- **Expected:** read calls down 40–60%. Wall clock down 15–30%.
- **Own check:**
  - read calls per role drop by at least 40%
  - re-plans and critic objections do not rise (a sign the dossier is missing something)
  - the dossier's size stays under its cap
- **Risk:** a stale dossier after a layer lands. The coder's built diff digest already updates later layers, and the
  dossier entry for a changed file is refreshed from the layer's commit.

### P3 — Compact the drafting conversation between layers

- **What:** after each package is accepted, the drafting conversation's history is replaced by a summary that keeps
  the agreement, the dossier, the accepted package's interfaces and tests, and the open questions. Tool transcripts and
  earlier drafts are dropped.
- **Expected:** decode in later layers stays near the fresh-context rate (about 20 tok/s rather than 13–16). Drafting
  is 20–40% faster from layer 2 on.
- **Own check:**
  - decode tok/s in the >48k bucket disappears or shrinks
  - each later package's critic verdict is no worse than layer 1's
  - no package contradicts an earlier one (the critic's cross-layer check)

### P4 — Per-role thinking budgets

- **What:** lever 1 tuned the architect only. Set budgets for the tester, critic, meeting roles and refine from P0's
  counters: start each role at about its 75th-percentile actual thinking length, not at a guess.
- **Expected:** 10–20%.
- **Own check:** no rise in `runaway_repeat` or empty-reply retries, and no thinking cut off at the budget on a turn
  that then fails.

### P5 — Shorter outputs

- **What:**
  - packages describe changes to existing files as edits (anchor and replacement) rather than whole files
  - meeting and critic replies use the terse structured formats they are parsed into
  - no restating the brief
- **Expected:** decode 5–15% lower.
- **Own check:** the package parser's rejection rate does not rise, and the coder's first-try gate pass rate does not
  fall.

### P6 — One planning round (lever 4)

- **What:** the survey and layer planning become one architect conversation, so the survey's reads are in context
  when the layers are split.
- **Expected:** 5–10%. P2 overlaps with this, so measure it after P2.
- **Own check:** the layer split is no larger and no worse (meeting revisions ≤ baseline).

### P7 — Overlap the work and batch it

- **What:**
  - drafting layer N+1 and critiquing layer N run beside the build of layer N−1
  - the gate and tests run while the next model call is in flight
  - the engine serves two streams at once
- **Expected:** 10–20% from the overlap, more with more layers. Batching gives about 1.5–1.8× total throughput, while
  each stream slows a little.
- **Own check:**
  - published PRs are identical in structure to a serial run
  - no lock contention or SQLite errors
  - memory peak stays under the limit (RAM was the reason a run was killed on 2026-10-27)
- **Risk:** RAM. Two streams need two KV caches, so measure the peak before turning batching on by default.

### P8 — Speculative decoding (same-family drafter)

- **What:** a small Qwen3 drafter (mlx-community) proposes tokens and the 27B verifies them. The output distribution is
  unchanged by construction, so quality risk is nil. The cost is memory and the engine work to support it.
- **Expected:** 1.5–2× on code and JSON, 1.2–1.4× on thinking text.
- **Own check:**
  - the acceptance rate is logged
  - outputs are identical at temperature 0 on a fixed prompt set
  - memory peak is under the limit
- **Status: blocked by the engine (checked 2026-10-27).** vllm-mlx 0.5.0, the installed version, has no draft-model
  speculative decoding. Its only draft model is SpecPrefill, which speeds up reading, not writing. `--enable-mtp`
  exists in 0.5.0 and 0.6.0, but it needs a model with MTP heads, which mlx-community's Qwen builds lack, so MTP
  stays parked. P8 waits on upstream or an MTP-capable build.
- **Batching (P7) is already available:** airlie runs with `continuous_batching: true` and `max_num_seqs: 16`, so
  P7 needs no engine work, only the pipeline overlap and a memory measurement.

### Out of scope for this plan: model changes

A lower-bit generalist (1.3–1.8×) and an MoE model for low-stakes roles (3–4×) are the biggest single multipliers, but
both change the model, so they can lower quality by definition. They belong to the fidelity experiment and need
Noah's call, not this plan.

## Expected cumulative effect (estimates, not measurements)

| After | Wall clock vs today | Effective decode |
|---|---|---|
| P1–P2 | −25 to −40% | unchanged |
| P3–P6 | −35 to −50% | ~20 tok/s held across layers |
| P7–P8 | −50 to −60% (≈2–2.5× faster) | ~25–35 tok/s effective |

## Implementation strategies

### A. Strict ladder

One lever, one roman run and one wiki run, then decide. This is the one-lever rule exactly as it stands.

- **For:** clean attribution. Any quality drop is traced to one change.
- **Against:** slow. Eight levers × (about 40 min of roman + 3–5 h of wiki) is roughly 5–6 days of engine time.

### B. Flags first, roman screens, wiki confirms (recommended)

- Build all of P2–P6 behind flags in one pass. Unit tests come with each lever.
- Screen each lever alone on roman: about 40 minutes per lever, 2 runs each, overnight in a batch script.
- Levers that pass go together into one wiki confirmation run.
- If the wiki run misses the bar, bisect by turning flags off, half at a time.
- P7 and P8 are engine-side and come after, each screened the same way.

**For:** keeps one lever per measurement where it is cheap (roman) and spends wiki hours only on survivors. About
1.5–2 days of engine time.

**Against:** levers can interact on wiki in ways roman can't show. Bisecting covers that, at the cost of extra runs
when it happens.

### C. Bundles

Three bundles, each measured as a unit:

- **Calls:** P1, P2, P6
- **Context:** P3, P4, P5
- **Throughput:** P7, P8

**For:** fastest, about 3 wiki runs. This is a bet that the levers are all positive, and the cost is paid only when
the bet loses. A bundle of 3 that misses the bar costs about 2 extra runs to bisect (log₂ 3, rounded up). Every lever
here is a flag that only cuts calls, context or output, so most should be positive, and the expected cost is low.

**Against:**
- It relaxes the one-lever rule.
- Interactions are hidden. One lever that helps can mask another that slightly hurts, as long as the bundle as a whole
  still passes the bar.
- The per-lever counters from P0 limit the hiding: each lever's own check (read calls, decode by context size,
  retries) is still read per lever inside the bundle.

### D. Throughput first

Do P8 (speculative decoding) and P7 (batching) before the pipeline levers.

- **For:** these levers don't change what the pipeline produces, so quality risk is lowest. They speed up every later
  benchmark run too, so the rest of the plan finishes sooner.
- **Against:** engine work with unknown vllm-mlx support, and RAM pressure. If speculative decoding is unsupported, we
  lose days before any pipeline gain.

## Order of work (strategy B)

1. P0: counters, wiki oracle, noise band (finish run B first).
2. Build P2–P6 behind flags, each passing the repository test gate.
3. Overnight roman screen: baseline ×2, then each flag alone ×2.
4. Wiki confirmation with the passing flags on, and bisect on a miss.
5. Turn the passing flags on by default, and record the run in `docs/bench/README.md`.
6. Check vllm-mlx support for speculative decoding and two streams, then screen P8, then P7.
