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

## Overnight 2026-10-28/29: making the bench fair, then the dossier A/B

Noah's brief: "wake up to a reliable pipeline with options for improvements tried; the priority is a pipeline
reliable enough that the tests are fair."

### Why the first A/B was not fair

The first dossier A/B decided runs by failures the dossier has nothing to do with. Four runs on 2026-10-28
missed the oracle three times, and the failures, not the lever, set the times.

| Run | Dossier | Time | Oracle | What decided it |
|---|---|---|---|---|
| 1 | off | 48.7 min | 18/18 | Clean |
| 2 | on | 105.8 min | 16/18 | Refine invented a wrong example ('MMMDCCCLXXXIX' for 3999); a 31-minute coder fix round no code could win; a thin re-planned package |
| 3 | off | 49.9 min | 17/18 | Valid numerals refused; 9 tests, none over the range |
| 4 | on | 30.8 min | 16/18 | Malformed numerals accepted; 7 tests |

### What was wrong, and what changed (PRs 175 and 178, all on the bench branch)

Ordered by the order they showed up.

| Problem | Fix |
|---|---|
| The meeting's roles could not tell who said what: AutoGen sends the speaker only as `name`, which the generalist's chat template never reads. Product posted the tester's proof; the tester took it for its own. | `add_name_prefixes`: every message starts "<role> said:" (PR 177, folded into 175) |
| A non-thinking coder looped red, writing the same file three times. | `think_when_red`: it thinks only while red, gets the `red-fix` instruction, and may not re-read files it wrote |
| Thinking, it gamed a wrong locked test (`if n == 1984: return "MCMXCIV"`). | Coder prompt: never special-case a test's inputs; report a wrong test instead |
| A 35-minute red streak, every edit different, and the supervisor never looked. | `red_streak` supervisor trigger, every 3 red runs in a row |
| A wrong-test diagnosis still got a coder fix round. | `wrong_acceptance_test` routing: the coder stops, the gate skips the fix round, and the architect is told to correct that test |
| Refine invented computed examples, and some were wrong. | No invented examples; a range or a refusal is one whole-domain criterion |
| Thin tests let bugs through. | A range is proven over every value; a refusal by one exhaustive test (every string of up to 4 characters, accepted exactly when it is a valid output) |
| A later layer's draft created a file an earlier layer made (`file_exists`). | Its brief lists the files earlier layers create |
| Titles bounced plans (8 of 20 plan fixes). | Titles are mended, not sent back |
| Pinned examples were wrong even when the criteria were right (MCMXCIV as 1984, three times in two days). | Beside a whole-range test, only the request's own examples are pinned |

### Reliability round: dossier off, the build before the last rule

All three runs passed the oracle at 18/18 and kept every quality check.

| Run | Time | Oracle | Mutation | Notes |
|---|---|---|---|---|
| 1 | 51.8 min | 18/18 | 59/60 | 2 layers, exhaustive refusal test present |
| 2 | 28.6 min | 18/18 (8/8 extra) | 28/35 | 6 tests: spec fully met, leaner tests |
| 3 | 119.8 min | 18/18 | 57/59 | Two recoveries: an unrunnable 16–20-character exhaustive proof (the architect's `proof_change` fixed it), and a wrong pin the coder reported instead of gaming (re-plan) |

Run 3 is why the last rule exists: both of its detours came from model-made numbers.

### The dossier A/B (in progress)

Six runs, alternating dossier off and on, on build 24a9138: every fix above, plus the no-pin rule.

## Levers added 2026-10-29: retries

Measured today across 9 roman runs, retries cost:

| Retry | Cost |
|---|---|
| A gate re-plan | ~47 min: ~31 for the failed build, then ~15 to re-package and rebuild |
| A package sent back | ~26 min |
| A proof revision | ~16 min |

The costliest part is the architect starting over in a cold conversation. In one-by-one package asks, 60 of 68
calls were reads, at ~104 s each, because every call thinks to the 2048 budget first.

- **P9: a retry resumes the drafting conversation.** A package sent back or a gate re-plan is asked in the
  architect's own drafting conversation, which still holds the files, the agreement and its drafts. It is
  compacted as P3 describes so it does not grow unbounded.
  - Measured: minutes, calls and reads per retry, against the baseline above.
- **P4b: per-step thinking.** The full budget only on the turn that writes the plan or package; a small one on
  read steps.
  - Measured: seconds per architect call.
  - It is its own bundle: it changes every call's time, which would blur P9's numbers.
- **R1: a failed build is time-limited.** After N red runs with no progress, the layer goes back to the
  architect early, with the supervisor's diagnosis.
  - Measured: minutes of failed build before the re-plan (baseline ~31).

**Bundle 2 = P9 + R1 + Q1.** Q1 is a meeting finding when a refusal criterion is proven by a list instead of an
exhaustive test. Each touches a different stage and has its own counter, so a bundle run shows each separately.

### Bundle 1 result: the dossier A/B (2026-10-29, build d05a3b7, roman, dossier off and on alternating)

| Run | Dossier | Layers | Minutes | Oracle | Mutation | Reads / calls | What happened |
|---|---|---|---|---|---|---|---|
| 1 | off | 1 | 46.7 | 18/18 | 24/35 | 33 / 57 | Clean |
| 2 | on | 1 | 34.1 | 18/18 | 65/65 | 18 / 36 | Clean |
| 3 | off | 2 | 113.5 | 14/18 | 95/100 | 63 / 112 | Proof revised twice; blocked at publish by stale gh-stack records (bench bug, fixed in 4972986) |
| 4 | on | 2 | 54.5 | 18/18 | 23/35 | 34 / 64 | Clean |
| 5 | off | 1 | 86.9 | 18/18 | 25/34 | 41 / 95 | Red build, re-planned |
| 6 | on | 1 | 81.0 | 18/18 | 53/53 | 42 / 63 | Package sent back and redone cold (P9's target); the repo test suite ran beside it, taking CPU |

**Results:**
- **Time:** dossier on averaged 56.5 minutes against 82.4, 31% faster (median 54.5 against 86.9). Like for like,
  one-layer clean runs were 34.1 against 46.7 minutes (27%), and two-layer runs 54.5 against 113.5.
- **Work:** 31% fewer reads and 38% fewer model calls on average, the mechanism the dossier was built for.
- **Quality:** the oracle passed 3/3 with the dossier on and 2/3 with it off. The miss is the sampled-refusal
  gap that Q1 targets. Mutation varies in both arms, with test thinness, not with the dossier.

**Verdict:** a real gain at no cost in quality, beyond the ±17% noise band on time and consistent in reads and
calls. `story.dossier` should be on by default.

### Bundle 2 result: P9 + R1 + Q1 (2026-10-29, build 91369da, roman, dossier on)

| Run | Layers | Minutes | Oracle | Mutation | What happened |
|---|---|---|---|---|---|
| 1 | 2 | 74.8 | 18/18 + 8/8 | 88/89 | Clean; P9 resumed one sent-back package in 6.6 minutes (a cold redo took ~26) |
| 2 | 1 | 40.1 | 18/18 + 8/8 | 27/35 | Clean; no lever triggered |
| 3 | 1 | 74.6 | n/a | n/a | Blocked: the architect's third draft was cut at max_tokens 16384 |

R1 and Q1 never triggered. Run 3 had three faults, fixed in 7250e2c before bundle 3:
- **The goal's closing line contradicted a read-only role.** `goal-tools` told the architect to "change it, run
  the tests" while it held read tools only. On its third draft it spent the whole thinking budget on the
  contradiction, kept reasoning in its answer and hit the cap. Roles whose tools only read now get
  `goal-read-tools`. The same line had reached every read-only role since the team loop was built.
- **The meeting's tests were wrong, twice.** It listed `VI`, `LX` and `XI` as forbidden pairs. The architect caught
  it both times, but each draft after a revision ran cold (~25 and ~15 minutes): P9 resumed only on a send-back.
  The ask after a proof revision is now a turn of the drafting conversation (`brief-proof-revised`).
- **The tester labelled its revised tests with the story's slug.** A label that names no layer is now the
  revised layer's; another layer's name is still refused.
