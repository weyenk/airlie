# Remove the legacy story pipeline; finish the generic-node world

2026-11-19. Brainstormed with Noah in chat. Nothing on a screen changes; this retires plumbing,
not visuals. Sits on top of `2026-11-06-generic-nodes-design.md` and
`2026-11-08-nodes-are-config-design.md`: this document ends what they started.

## Intent

The hand-written story pipeline — `src/airlie/story/` and everything wired to it — is a long
detour on the way to *generic nodes*: config-driven boxes a workflow wires together. That
detour is over. Work starts from generic nodes and a workflow builder even though neither is
perfect yet; everything below assumes that, not the reverse.

## Decisions Noah made in chat

- **Two node kinds, not more.** One *generic work* node covers almost every step — plan,
  build, review, and anything else today's `model`/`check`/`gate`/`bundle` nodes did,
  including steps that only run code ("if a snapshot runs code, I don't see why a model
  couldn't handle that"). Everything else that survives is a kind whose *inside* is a
  conversation between agents: the three-amigos meeting, AutoGen teams, supervisor with
  workers, k-independent teams. If an edge case seems to need a third kind, fold it into one
  of these two by editing the prompt — do not add a kind.
- **Gates are evidence, not a node.** The author of a node writes its definition of
  "finished"; a gate is how the generic node knows it got there. That definition may be a
  script to run, a prompt to judge, or a step count — it does not have to be tests, and it
  does not have to be code that runs *between* nodes.
- **`when` and `loop` are the same thing: a condition on a wire.** Every condition
  answers "the condition isn't met — take *this* path; it is met — take the *other*
  path," and **any node may loop back to any earlier node** (a node that isn't done yet
  routes back through a previous one; that *is* what loops are). The author of the
  workflow must say where the second path goes — an unmet condition is never a silent
  dead end.
- **Lines only route.** A connector carries its condition and the artifacts its source
  produces, and nothing else: no logic in wires beyond "when this holds, take that
  edge; otherwise this one."
- **`flow/` is purge; work from worktrees.** The `airlie.flow` engine's runtime — composer,
  runners, team, store, upgrade — and anything that preserves yesterday's run state in the
  shape yesterday's code wrote, goes. **Tart VMs and `softnet` go too** (`boxes/image`,
  `boxes/transit`, `boxes/mirror`, the `vm` engine kind): repositories are only ever built
  in git worktrees under the run directory.
- **Costs and checks face outward.** Token and wall-time tracking come from `mlx-vlm` / the
  engines directly as much as possible; `story/costs.py` and friends are reporting, not
  workflow steps.
- **A workflow can start three ways.** A slash command in chat, a story card that picks a
  workflow, and the job-hub bus are all *triggers* for the same run — chat's "Start
  story," serve's minute-by-minute beat, launchd every 15 minutes, and `task story:tick`
  (which already announces itself as obsolete) all become **one** trigger kind that begins
  when chat says so, when a card asks for it, or when the hub does.
- **The story pipeline and its labels are gone.** A workflow may still start from a card;
  that's a workflow, not a story pipeline.

## Still to be built from scratch

The workflow YAML (`config/workflows/*.yaml`) is hand-wired to today's node kinds; that
wiring and the old `airlie story …` / `task story*` / `task line:*` plumbing around it get
rebuilt from zero. **`src/airlie/story/` stays until that rebuild lands** — much of that
package is code for the generic runtimes or data they read, not code to delete in this
cut.

## Where this lands

- **Design doc:** this file.
- **Implementation plan:** `docs/superpowers/plans/2026-11-19-legacy-story-removal.md` —
  several numbered parts; each part ends with something that runs without the old pipeline.
- **Review before building.** No task in that plan gets built until its full description
  has your approval; the first chunk of questions and options covers anything this page
  leaves open.

## What to keep in mind while planning

1. `story/` imports its own siblings as if they were first-class (`flow/runners.py` pulls
   `_cap`/`_reason` from `story/pipeline`; `config/schema.py` imports `story.flow`'s catalog
   lazily to dodge a cycle). The rebuild should *replace* these with real generic nodes and
   a workflow file that owns that flow, not keep lazy imports to hide that nothing else
   changed.
2. Don't leave two ways to launch the same work; when chat, cards, and the hub all need a
   workflow, that's one seam and three triggers.
3. If a gate is a script or an opinion a person wrote, the next person must be able to tell
   that work finished or stalled *without* digging through code.
4. Every test, prompt file, and Taskfile target that names `task story …` or
   `story.<key>` is in scope: the gate is `uv run pytest` and friends *after* the rename,
   not before.
5. `config/workflows/` currently holds only `story.yaml` and `layer.yaml`; "a project with a
   written design" may mean one workflow rebuilt in this plan while the *idea* of "any
   workflow a builder publishes" stays open — say which.

