# The plans phase: an epic becomes prioritized stories, by the architect, the way writing-plans does

Status: draft for Noah, 2026-10-24. Sub-plan P3 of `2026-10-24-planning-line-design.md`; it sits on P1
(`2026-10-24-layer-package.md`) and P2 (`2026-10-24-three-amigos-design.md`), which must be built first.

## Why

A story enters airlie today as a card a person wrote, or a paragraph; nothing turns a larger piece of work
into stories. The architect plans only inside a story, into layers. The line needs the stage above: an epic,
with its spec and what else was learned about it, turned into the smallest units of work that can each be
delivered on their own, in an order, on the board, so that each can go to its own three amigos meeting.

Noah's framing (2026-10-24): this phase mimics the `superpowers:writing-plans` skill, done by the architect.
It takes the spec and the other information stored in the epic and turns it into the smallest deliverable
units of work. The stories are the input of the three amigos phase. The writing-plans skill says how specific
to get; the difference is that the output here is prioritized stories on the board, not a document.

What writing-plans does, and what it becomes here:

| writing-plans | the plans phase |
| --- | --- |
| a plan for an engineer who has not seen the codebase or the spec | a story plan for three amigos who will see only their story and the epic's header |
| header: goal, architecture, tech stack, the spec it argues from | the plan's header: the epic's goal, the shape of the change, its constraints, the epic it argues from |
| Global Constraints copied verbatim from the spec | the epic's constraints, carried into every story's brief |
| Review Focus: the failure modes the spec implies, each pinned to a task's test | the epic's risks, each pinned to one story's acceptance criterion |
| a task: the smallest unit with its own test cycle, worth a reviewer's gate; files, interfaces consumed and produced, the failing test first | a story: the smallest unit that ends in a pull request stack a reviewer can judge alone; its files, the interfaces it consumes from earlier stories and produces for later ones, its acceptance criteria as the test that proves it |
| steps, as code | not here: the amigos split a story into layers, and the architect's package carries the tests as code |
| tasks in order, each committed | stories in dependency order, each a card on the board, ranked |

## Goals

- An approved epic becomes, with no person in the loop, a set of stories on the board under it: each the
  smallest unit that can be delivered and reviewed on its own, each with testable acceptance criteria, its
  repository, the files it may touch, the interfaces at its boundaries, and what it depends on; in an order
  the tick can follow.
- The stories are good input for the three amigos: the epic's header and constraints travel with each, so
  the meeting does not re-read the epic, and product's requirements review in the meeting is the first and
  only refine a story born from an epic gets.
- The plan is checked mechanically before any card is made: every epic criterion is covered by a story,
  dependencies are acyclic and name known stories, scope paths exist on the repository's trunk or are marked
  new, each story is within the size rule, the repository is one of the epic's.
- The epic's own record (state, events, the plan as written) exists beside the story's, so a person can see
  why the stories are what they are and what came back from the architect.

## Non-goals

- Generating the epic (P4). Until P4, an epic is a card a person writes in the spec's shape, or
  `task epic -- --file spec.md`.
- Mockups (P4), the human pause for them (planning-line N4), Jira's create side (planning-line D1), the
  React screens (the flow API gains an epic row; the React work renders it).
- Running stories in parallel, or merging anything. One story at a time, as today; a human merges.
- Re-planning an epic on every event. Re-plan happens on a story block and on request (D8), not after each
  story.

## Anti-goals

- Stories that are layers in disguise: a "story" whose criteria cannot be judged by a reviewer from its PR
  stack alone is too small; one that needs two reviewers' afternoons is too big. The size rule and the
  reviewer's-gate test (D2) hold both ends.
- A plan that decides the implementation. A story names files, interfaces and behaviour; it does not say
  how the code reads. That is the amigos' and the coder's.
- A dependent story built on code a person has not merged, *quietly*. Stacking across stories is the design
  (N2, D5); every PR of a dependent story says which story's PRs it stacks on, and a change to the lower
  story is replayed, not ignored.
- Criteria restated from the epic without a repository in view. The architect reads the code before it
  writes stories, as `brief-layers-task` already demands for layers.

## The shape

```
 EPIC card (level epic, approved; description = the spec + one JSON block)
    │  the architect: reads the epic and the repository (survey), plans in rounds (story/rounds.py)
    ▼
 STORY PLAN (JSON): header, constraints, risks pinned to stories, stories in dependency order
    │  mechanical checks → back to the architect with findings, up to story.stage_attempts
    ▼
 STORY cards under the epic, ranked in plan order; the first ones ready, dependents waiting (D5)
    │  task line:tick / task story:tick takes the top ready story
    ▼
 the three amigos (P2) → layers (P1) → PR stack → the dependents become ready, stacked on it (D5)
    │                                                    a person merges, bottom up; merged stacks are restacked onto the trunk
    ▼
```

## Decisions

Noah's (2026-10-24):

- **N1.** The plans phase mimics `superpowers:writing-plans`, done by the architect, from the spec and other
  information stored in the epic, into the smallest deliverable units of work. The stories are the input of
  the three amigos. The writing-plans skill calibrates how specific they are. The output is prioritized
  stories on the board.
- **N2.** A dependent story does not wait for its dependency to merge: it builds on the dependency's branch
  stack, one stack across the epic's stories, and the line moves forward.

Mine (say so if any should change):

- **D1. The epic's shape, until P4 writes it.** An epic is a board item at level `epic` whose description is
  a document in the spec's shape (why, goals, non-goals, anti-goals, constraints, decisions, acceptance
  criteria at the epic's grain, what is left open) closed by one JSON block:
  ```
  {"type": "epic", "repositories": ["owner/name"], "criteria": [{"id": "EC1", "text": "one line, at the epic's grain"}],
   "constraints": ["one line each"], "links": ["https://… (a mockup, a design)"]}
  ```
  An epic's criteria are one line of text each, with an id: a spec's criteria are prose, and Given/When/Then
  belongs on the story, where the tests are. `task epic -- --file spec.md [--repo owner/name]` makes the card
  from a Markdown file (the JSON block written from `--repo` and the bullets of the file's `## Acceptance
  criteria` section, one criterion each, when the file has no block); a person may also write the card in
  the web app. The card is picked up once a person sets it `approved` (planning-line D10: a person's card is
  ready when a person says so). An approved epic with no JSON block (a Jira import) is set aside with a
  note, never planned from prose. P4 will write the same shape.
- **D2. The story plan is the architect's reply, a JSON contract beside its parser (`story/epic_plan.py`):**
  ```
  {"type": "story_plan",
   "summary": "the epic's goal and the shape of the change, three sentences",
   "constraints": ["copied from the epic, one line each"],
   "risks": [{"risk": "what would bite a person", "pinned": "S2/AC1"}],
   "stories": [{"id": "S1", "title": "imperative, under 80 chars", "why": "one sentence from the epic",
                "repository": "owner/name", "epic_criteria": ["EC1"],
                "scope": [{"path": "src/pkg/mod.py", "new": false}],
                "consumes": ["S0: def name(...) -> R"], "produces": ["def name(...) -> R"],
                "acceptance": [{"id": "AC1", "given": "...", "when": "...", "then": "..."}],
                "out_of_scope": ["..."], "assumptions": ["..."], "depends_on": ["S0"], "size": "S" | "M"}]}
  ```
  A story is the smallest unit that ends in a pull request stack a reviewer can judge alone: its criteria
  are what the reviewer checks, its scope is what the diff touches. Parsing refuses: a story id repeated or
  unknown in `depends_on` or `consumes`; a cycle; an epic criterion no story's `epic_criteria` names; a
  story with no acceptance criterion; a repository not among the epic's; more than `story.plan.max_stories`
  stories; a scope over `story.plan.max_scope_files` paths; a `pinned` that names no story criterion.
  Reasons are the architect's feedback (`brief-reply-again`), up to `story.stage_attempts`.
- **D3. The architect plans in rounds, with the repository in view.** The call is today's `_ask_in_rounds`
  (story/rounds.py: a plan after a few steps, refined while reading changes it) on a detached worktree at the
  repository's trunk, with read-only tools; the brief (`plan-brief`, a hub rule) carries the epic's document
  as data, the repository guide (`repo_guide` for the paths the epic names), the testing conventions (P2's
  reader, so criteria are written knowing what can be tested where), the recent lessons for the repository
  (P1's store), the sizing rule, and the story-plan contract. For an epic with several repositories the
  architect reads the first (`repositories[0]`) and plans the others' stories from the epic alone, naming
  their repository; a later design may plan per repository.
- **D4. Mechanical checks before any card is made.** D2's parse rules; every `scope` path not marked new
  exists on that repository's trunk or is marked new in an earlier story of the plan (a dependent story's
  files often come from the story before it; for repositories the line has no checkout of, paths pass
  unchecked); every dependency points at an earlier story in the order; every story's `acceptance` lines are
  non-empty Given/When/Then; `size` is S or M (an L is a finding: split it). A finding is feedback to the
  architect's next round, not a card.
  A story's `scope`, `consumes` and `produces` are the architect's opening proposal for the meeting, not a
  fence: the amigos may widen a scope (the architect owns changes), and when the agreed layers touch files
  outside it the pipeline records `scope_widened` on the story and the epic, so a plan that keeps
  under-sizing stories shows. The "too big" end is held at plan time (`max_scope_files`, `size`) and per
  layer by P2's size rule; P2's checker gains no rule from this.
- **D5. Stories land on the board under the epic, in plan order, and a dependent story stacks on its
  dependency (N2).** Each story becomes a `BoardStore.create(level="story", parent=<epic key>, title,
  description, labels=["repo:owner/name", "epic:<epic key>", "plan:S1"])` with the description holding the
  story as a person reads it and a closing JSON block with the story's contract (the amigos read it from
  there), ranked in plan order. A story with no dependency is born `approved` (planning-line D10: a card the
  line made is ready at birth); a story with a dependency is born `todo` with the label `after:<dependency
  key>` and is promoted to `approved` by the tick as soon as every dependency's story is in `review` (its
  PRs are open). Its base is then the dependency's top layer branch, not the trunk: `StoryState.base` records
  `{"ref": <that branch>, "sha": ..., "story": <dependency key>}`, the story's layers build on it as layer 02
  builds on layer 01 today, and its publish step stacks its branches on the dependency's top branch (`gh
  stack` with that branch as the base) so the epic's PRs are one stack, bottom up, each dependent PR's body
  naming the story it stacks on. Two consequences the line handles: when a reviewer changes a lower story
  (its branch moves), the dependent's layers are replayed onto the new base by `story:restack`'s machinery
  (`restack.replay`, today for trunk moves), triggered by the tick when a dependency's recorded sha differs
  from its branch; and when a lower story's PRs are merged, the dependent's base becomes the trunk, the same
  replay. A dependency whose PRs were closed unmerged blocks its dependents with a note on the epic.
- **D6. A story born from an epic skips refine.** Its card's JSON block is a `Story` (D2's story object maps
  onto `story/model.py`'s `Story`, plus `scope`, `consumes`, `produces`, `depends_on`, `epic_criteria`); the
  pipeline's `_refine` takes it as the story and goes to the split and the meeting, where product reviews
  the requirements (P2). A card without the block (a Jira issue, a person's story, `--text`) is refined as
  today.
- **D7. The epic's header travels with every story.** The plan's `summary`, `constraints` and the risk
  pinned to the story go into the story's card and from there into the amigos' opening and the layer
  package's brief, under `## The epic this story serves`, so a story's room sees the whole without reading
  the epic card.
- **D8. A blocked story is resumed once by the tick, then the epic is re-planned.** The tick moves a story
  the line blocked back to `approved` once (label `resumed:1`), so the common case, a model's bad day, costs
  a rebuild and not a person's morning; planning-line D10's fuller budget is P5's. When the story blocks
  again, or on `task epic:replan -- KEY`, the architect is asked again with the current plan, every story
  marked as not started, started, blocked (with its lesson and how far it got), in review or merged, and any
  comments a person left on the epic card since the plan, and returns a new plan for the stories that have
  not started and for the blocked one. The re-plan may **retire a blocked story that has no built layer**
  (its card goes to `done` with a note saying the re-plan retired it and what replaces it; its state
  directory is kept; its attempt's worktree and branch are removed as a resume would remove them) and
  replace it with stories that do its work differently; a blocked story with a built layer is kept, its
  layers are on the board for a person, and the re-plan may only add stories after it. Unstarted cards are
  changed in place (title, description, rank), new ones made, dropped ones moved to `done` with a note.
  Stories in review or merged are never changed. The re-plan brief forbids a new story that redoes a kept
  story's work. One automatic re-plan per block.
- **D9. The epic has a record.** `epics/<key>/state.json` (the plan as written, its attempts, the cards made,
  re-plans) and `events.jsonl` (`epic_start`, `plan_round`, `plan_returned` with finding kinds, `plan`,
  `stories_created`, `story_promoted`, `replan`, `blocked`); the epic card gets a comment when the stories
  are made (their keys and order) and on a re-plan. The flow API gains an epic row listing its stories and
  their states; the React work renders it.
- **D10. The tick learns the epic, and the epic's status gates its stories.** `task story:tick` (P5 renames
  it `line:tick`) adds, before it picks a story: take the top `approved` epic with no plan and plan it;
  promote waiting stories whose dependencies reached review (D5); replay a dependent story whose base moved
  (D5); resume a blocked story once (D8). Then, as today,
  build the top approved story. Planning and building are each a tick's one piece of work; promotion and the
  one resume are bookkeeping done on every tick. The epic's own status moves with its stories: `doing` when
  the first story is picked, `review` when every story is in review or done, `done` when every story is
  done. A person moving the epic to `blocked` or `todo` holds it whole: none of its stories is promoted or
  picked until the epic is `approved` or `doing` again, and `LocalBoard.next_card` reads the parent's status
  for that. A person who hand-approves a waiting story is honoured, as `task story -- KEY` is today.

## Constraints

- The planning-line constraints apply. The epic's text and the architect's plan are data; a path or
  repository name reaches git or gh only after the checks.
- The tick's one-story lock stays; planning an epic holds it too.
- `BoardStore` and the board API are the only writers of cards; Jira is read-only for the line.

## Left to the builder

- The field names of `EpicSpec`, `StoryPlan`, `PlannedStory`; how a story's card renders; the `task epic`
  command's options.
- `plan-brief`'s wording and the architect's role prompt's additions for story planning.
- How merged PRs are read for promotion (one `gh` call per PR, or `gh pr list --search`), and how a
  repository with no `gh` access is handled (the dependents wait, the epic gets a note).
- The epic's state directory layout and how it names the stories it made.

## Decided with Noah, 2026-10-24

1. A dependent story stacks on its dependency's branches and the line moves forward; a lower story's change
   or merge is a replay of the dependent's layers (N2, D5).

Settled by me (say so if any should change): an epic that spans repositories is planned reading the first
one only (D3), per-repository planning being a later design; epic criteria are one line of text
with an id, not Given/When/Then (D1); a story's scope is the meeting's opening proposal, widened on record,
not a fence, and a dependent story may name files an earlier story creates (D4); the tick resumes a blocked
story once and the re-plan may then retire it when it has no built layer (D8); the epic's status gates its
stories and moves with them (D10).
