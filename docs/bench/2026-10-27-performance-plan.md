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
- **Status: built (2026-10-30, MTP rather than a separate drafter).** The 2026-10-27 note here, "blocked by the
  engine", was wrong. vllm-mlx 0.5.0 has MTP for this exact architecture (`patches/qwen3_5_mtp.py`, dense 27B,
  and the MLLM batched path). mlx-community publishes the head alone as `Qwen3.8-27B-MTP-bf16` (0.85 GB, BF16),
  which the 4-bit build drops. airlie serves the generalist from a directory that links the body and holds the
  head re-keyed under `mtp.` (`engine/mtp.py`, a model's `mtp:` in `config/models.yaml`), with `--enable-mtp`.
  - Smoke test, one draft token, temperature 0: 27.0–27.5 tok/s before, 32.6–36.2 after (1.19–1.34×).
  - Outputs diverge only at near-ties (the two-token verify changes the arithmetic).
  - The hybrid prefix cache still hits: a resumed 24k-token turn took 2.0 s against 28.7 s cold.
  - Two concurrent requests both finish. Resident memory is 19.6 GB.
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

### Bundle 3: reliability, until three clean runs (2026-10-29/30, roman, dossier on)

Bundle 2's three fixes (7250e2c) went in first. Then each failed run was stopped, its cause fixed, and the bench
restarted from run 1:

| Try | Build | What broke | Fix |
|---|---|---|---|
| 1 | 7250e2c | Refine's AC8 said no canonical numeral exceeds four characters (XVIII has five). The meeting agreed, the architect's tests encoded it, the gate went red, and on the re-plan the critic held the architect to the false bound | 7de5f2b: a `proof_change` may name `{criterion, why}`. Product rewords the criterion under its id, then the tester revises the tests |
| 2 | 7de5f2b | The architect wrote `_assert_malformed(s)` at the module's top level while the tests called `self._assert_malformed`. The coder, locked out of the test file, patched `unittest.TestCase` from `roman.py` | 3caade8: `unknown_method`, a test calling a `self.` method its class does not have |
| 2 (re-plan) | 7de5f2b | AC5 named every string of sixteen or more characters, and the architect swept `itertools.product(letters, repeat=16)` (7^16 cases). Scoped runs timed out at 600 s, and the coder spent an hour in `shell` | 6f6e8e8: `test_too_large`, an `itertools.product` sweep over ten million cases |

**Three clean runs on 6f6e8e8.** Each ran with no retry, no package sent back and no re-plan:

| Run | Layers | Minutes | Oracle spec | Oracle extra | Mutation |
|---|---|---|---|---|---|
| 1 | 1 | 23.4 | 18/18 | 8/8 | 31/38 |
| 2 | 2 | 55.1 | 18/18 | 6/8 | 55/55 |
| 3 | 1 | 27.6 | 18/18 | 8/8 | 33/33 |

Notes:
- Run 1's survivors are the numeral table's constants. Its AC1 only round-trips, so no test pins the canonical form.
- Run 2's two missed extras are type cases (a float or bool given to `to_roman`) that the request does not ask for.
- When a scoped run times out, the coder stops calling `run_tests`, so R1 (which counts red `run_tests`) never fires.
  `test_too_large` covers the case seen, but not the gap itself.

## Levers added 2026-10-30: overlap (P7 split; value them after the MTP work)

Noah's question: can the meeting, the architect, the coder and the reviewer work at the same time? Decoding is
memory-bound, so two batched streams give about 1.8× the throughput of one. That beats MTP's 1.3×, and the
single-stream MTP rule turns drafts off while requests overlap. These follow the MTP bundles and are valued
then. Riskiest last:

- **O1: the architect drafts ahead while the coder builds.** Today every package is drafted before the first
  layer is built (bundle 3 run 2: 24 minutes for two packages before any code). Instead the coder starts layer
  1 once its package passes the checks, while the architect drafts layers 2 to N and the critic reads them.
  Each package is still checked against its real base just before its layer, so correctness is unchanged.
  - Measured: minutes from the first package ready to the last layer built, on roman and on wiki.
  - Check: locks, the shared SQLite, the test lock and peak memory with two contexts loaded.
- **O2: the reviewer reviews layer N while layer N+1 is written.** N+1 is built on N's branch, so a review
  that asks for changes means a rebase and possibly rework.
  - First measure how often a review blocks. Then decide whether to overlap always, or only when the gate is
    green and the review is advisory.
- **O3: parallel meeting rounds.** Product, the architect and the tester answer the same state at once, then
  the checker runs: one call per round instead of three (meetings ran 6–12 turns, 8–15 minutes).
  - Risk: roles stop reacting to each other within a round, so parts can conflict and rounds can multiply.
  - Measured: meeting minutes and turns, and the agreed proof's quality (re-plans, proof revisions, oracle),
    against turn-based meetings.

### Bundle 4 result: MTP (2026-10-30, roman)

Built in four commits:
- **6f083ec:** the generalist serves its mlx-community MTP head.
- **2960f6d:** a request joins an MTP batch only once the batch is settled. vllm-mlx 0.5.0 otherwise fed rows
  a token their cache already held, which corrupted 3 of 15 concurrent replays (`"acceptacceptance"`,
  `<=read_file>`).
- **3d4ff59:** thinking requests draft while their budget processor would change nothing. Before it, 1,829 of
  4,552 requests carried the processor and got no MTP.
- **43f54d3:** MTP drafts only while one request decodes. With two rows, refine fell to 17.6 tok/s against 26
  without MTP.

Live checks:
- One request: 37 tok/s, against 27.
- Three at once: 23.8 tok/s each.
- Thinking budgets close on the exact token.
- The corruption replay: 0 of 15.

Three runs on 43f54d3, all published with the oracle spec at 18/18. A scan of all 199 replies found no dropped
or doubled token.

| Run | Minutes | Output tokens | Min per 1k tokens | Rework |
|---|---|---|---|---|
| 1 | 123.9 | 173,826 | 0.71 | The proof said no numeral exceeds 13 characters: red build, criteria reworded (AC4, AC6), proof revised |
| 2 | 26.9 | 43,361 | 0.62 | None |
| 3 | 47.2 | 68,621 | 0.69 | The architect's test compared `from_roman(s)` to `int(s)`: gate re-plan, and a package sent back once (`file_missing`) |

Bundle 3's runs without MTP were 0.71–0.76 min per 1k tokens.

**Decode per reply:**
- 31.3–32.7 tok/s with MTP, against 24.8–25.4 without (+25–30%).
- Architect 33.1–33.7, tester 32.0–36.1.
- The coder stays at about 26: its repetition penalty is a logits processor, so it gets no MTP.

Wall time per output token improves less, 10–18%. Prefill, test runs, tool calls and waiting are not decoding.

**Verdict:** MTP is a real per-token gain and keeps output correct with the guard. Two things outweigh it at the
run level:
- **How much the roles write:** the meeting wrote 9k–12.6k tokens across runs, the coder 1.2k–9.1k.
- **Rework from invented facts:** four runs in five had a false derived fact or a wrong expected value in the
  criteria, the proof or a test.

**Next, bundle 6:**
- **C1:** a sandboxed `compute` tool for product, the architect and the tester, with a rule that a stated
  number is computed first.
- **R2:** a wrong-acceptance-test triage ends the build and re-plans.
- **M1:** a test file the package writes in full, marked "modify" but missing on the base, is created.

## Parked 2026-10-30 (Noah)

- **Did MTP lower fidelity?** Unlikely in theory: the verify keeps the target distribution. The runs show
  about one rework per run, before MTP as after. The test is the same planning prompts 20 times with MTP on
  and 20 off (roles' temperatures, no `compute`), counting wrong derived facts.
- **The 8-bit generalist** (`mlx-community/Qwen3.8-27B-8bit`, ~29 GB).
  - Speed: about 0.55× decode, about 0.7× with MTP, whose acceptance should rise.
  - Judge it by wall-clock per built layer including fix cycles, as the fidelity-vs-intelligence experiment
    set out (2026-10-18). Value it after bundle 6 shows what `compute` does to rework.

### Bundle 6 result: compute (C1), R2, M1 (2026-10-30, roman)

The first two tries (924e8ba) blocked:
- **Try 1:** a layers follow-up was cut off at max_tokens.
- **Try 2:** the meeting's 1..15-character sweep was dropped without a proof_change, then a covers id carried a
  note. Both mended in 2d4b431.

On 2d4b431/9d3f2bb:

| Run | Layers | Minutes | Min per 1k tokens | Oracle | Rework | compute calls |
|---|---|---|---|---|---|---|
| 1 | 2 | 47.2 | 0.56 | 18/18, 8/8 | None | 24 |
| 2 | – | blocked | – | – | The meeting hit its turn cap: `proof_sampled` kept firing on AC6 and the tester posted no proof until turn 13 | 7+ |
| 3 | 2 | 71.7 | 0.55 | 18/18, 6/8 | Two proof revisions, one retried | 40 |

**What it showed:**
- **No invented derived fact reached a build.** Rework moved to the meeting and to proof revisions.
- **compute was overused.** In run 3 its steps produced 72,000 tokens (about 36 minutes of decoding), each call
  writing a fresh reference implementation. Layer planning in run 1 took 18.6 minutes.
- **The meeting never had compute:** its tool list was hard-coded to the read tools. The tester's proof called
  DCCCLXXXVIII "14 characters, the maximum".

### Reliability round 1 (2026-10-30)

- **The meeting gets compute.**
- **compute keeps a workspace per conversation**, so a reference module is saved once and imported later, and
  a conversation runs at most 6 programs.
- **`proof_sampled` stays quiet** when the proof already has an exhaustive test under any criterion.
- **`proof_too_large` (advice)** flags a proof sweep over a named alphabet that is too large to run.
- **A first cut-off at max_tokens is asked again, shorter.** A second cut-off in the same story, stage and role
  still blocks.
- **Left as is:** a timed-out test run already counts as red for R1. The gap was the coder switching to shell,
  which `test_too_large` (before the build) and R2 (the repeat guard) now cover.

**Round 1 result (28b4983): three clean runs in a row.** None had a retry, a package sent back, a proof
revision or a re-plan. All three passed the oracle spec at 18/18, and a scan of 175 replies found no dropped or
doubled token.

| Run | Layers | Minutes | Min per 1k tokens | Oracle extra | Mutation | compute calls |
|---|---|---|---|---|---|---|
| 1 | 1 | 38.8 | 0.79 | 8/8 | 55/55 | 8 |
| 2 | 1 | 27.7 | 0.63 | 6/8 | 33/33 | 9 |
| 3 | 1 | 33.3 | 0.59 | 8/8 | 58/59 | 14 |

compute calls fell from 24–40 a run to 8–14 with the workspace and the cap. Meetings still take 9–10 turns
(10–21 minutes), and that is now the largest stage: P5a (short turns) and the tester's proof in its first turn
are next.
