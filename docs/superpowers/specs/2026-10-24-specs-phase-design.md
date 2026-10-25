# The specs phase: initiative to epics (P4 of the planning line)

Status: agreed with Noah on 2026-10-25 (its decisions N1 to N7 are his). Plan:
`docs/superpowers/plans/2026-10-25-specs-phase.md`. Parent: `2026-10-24-planning-line-design.md`
(D6, N1, N4, and its sub-plan P4). Reads the epic in the shape P3 fixed (`2026-10-24-plans-phase-design.md`
D1) and extends it. Its plan follows once this is agreed.

## Why

Today a request enters airlie as a story: `task story -- --text "..."` or a card a person wrote at the story
level, refined by product into one thin slice. There is no place for a loose ask ("I want the board to feel
like Linear", "airlie should run overnight without me") to become a shape of work a person can weigh: what it
would take, whether it is viable, what it would cost in machine hours, and what the smallest version is. The
superpowers brainstorming skill does exactly that for a person working with Claude: one open question at a
time, a look at the code, three options to react to, a written design a builder can plan from. P4 lays that
skill over airlie's roles: product runs it, the specialists are consulted as needed, and the output is one or
several epic cards, each priced in hours, that P3 can plan stories from.

The deciding question, in Noah's words: does the compute time justify itself over other priorities. So the
epic is first a price next to a value, in a queue of other prices, and the phase is shaped around making that
decision easy and well-founded.

## Goals

- A paragraph from a person becomes one or more epic cards they recognise as what they meant, with the
  questions product asked them recorded on the initiative.
- Each epic states its value, what it takes at the epic's grain, a viability verdict with reasons, and an
  hours estimate with its basis, next to the hours already queued.
- The initiative is cut into a core epic and optional extensions, each priced on its own, so a person trims
  by leaving extensions in the queue rather than saying no to the whole.
- For web work, ux and design draw three mockup options that differ; the person picks one before the epic can
  be approved; the pick travels into the stories (planning-line N4, D6).
- Product asks a handful of questions first, only what the repository cannot answer, then works with the
  specialists and comes back; the person is never asked to sit through the imagining.
- Everything product and the specialists did is on record: the Q&A, each consultation, the planner's read,
  the estimate's arithmetic.

## Non-goals

- Creating epics in Jira (planning-line D1: the local board is the line's board).
- The Vue screens that show the dialogue, the mockups and the queue: P4 provides the flow API rows and the
  files; the Vue work renders them.
- Several initiatives in flight at once, or an initiative worked in parallel with a story (the one lock).
- An estimate model beyond medians from the ledger with a calibration factor; the line's own history is the
  only basis the number needs.
- Product building anything, or approving an epic itself.

## Anti-goals

- An epic approved by the line. The hours decision is the person's; the epic waits for them.
- A question flood: more than a handful of questions, or two at once, or a question the README answers.
- Three mockups that are one mockup three times.
- A number without its arithmetic, or a verdict without its reasons.
- A specialist's answer rewriting what the person said: the initiative text and the person's answers are
  quoted, never paraphrased into the epic as theirs.

## The shape

```
  a person writes an initiative card (a paragraph; repo: labels; an optional budget) and sets it approved
      │  the tick starts the specs phase (one piece of work, the story lock held)
      ▼
  QUESTIONS   product asks one question at a time as comments on the card, at most five, only what the code
      │       cannot answer; the person replies in a comment; "go" skips the rest. The card sits in review
      │       between a question and its answer; the tick resumes it on the reply.
      ▼
  IMAGINING   product leads, in rounds: it names the seats it wants to hear from (ux, design, security, docs,
      │       research, the architect; any role in specs.consultable) and the question for each; the pipeline
      │       runs them side by side, read-only, and hands the answers back into product's conversation.
      │       The architect's survey of each repository runs unasked beside the first round. For web work
      │       product commissions mockups: ux names three directions that differ, design renders each.
      ▼
  EPICS       product's final reply: a core epic and optional extensions, each a spec (why, goals, non-goals,
      │       anti-goals, decisions with reasons, constraints, criteria, a story sketch with sizes, risks,
      │       a viability verdict with reasons, what is open). The pipeline prices each from the ledger,
      │       runs the mechanical checks, and has the architect read each one as the planner it will meet.
      ▼
  CARDS       epic cards under the initiative, core first, each born in review with its document, its JSON
      │       block, its hours and the queue's hours; a delivery note on the initiative; mockups side by side.
      ▼
  THE PERSON  approves an epic (P3 plans it), picks a mockup, parks an extension in todo, or comments: a
              comment is one revision round by product (new document; new options when it is about them).
```

Mapping to the brainstorming skill: the first message's "skip the questions" line is product's first
comment; the one-question conversation is QUESTIONS; "offer recon" is the survey and the consultations;
"think wide privately" is the imagining round; the visual companion is the three mockups; "play it back" is
the delivery note's summary of each epic; "size the work" is the core and its extensions, each with hours;
the written design is the epic document; the builder check is the planner's read; the HARD GATE is the
person approving the epic card.

## Decisions

Noah's (2026-10-24):

- **N1.** The specs phase mimics `superpowers:brainstorming`, run by product. It takes an initiative with its
  loose ask and produces one or many epics: the first layer of imagining what is possible.
- **N2.** Product runs it and asks for input from ux, design, security, docs or any other agent, whatever it
  takes to reach a comprehensive idea of where the product should go and an epic-level understanding of what
  it will take, whether it is viable, and how many computing hours it needs.
- **N3.** Product asks the person a handful of questions first, the ones nothing in the repository can
  answer, then goes off with the specialists and comes back with the epics.
- **N4.** Whether an epic runs depends on whether its compute time justifies itself over other priorities:
  the hours figure is the price, weighed against the queue.
- **N5.** Trim rather than drop: an initiative is cut into a core epic and optional extensions, each priced
  on its own; the person approves the core and leaves the rest in the queue.
- **N6.** A person's answer runs product's next turn at once, through a serve route, with the tick as the
  fallback when the lock is held (2026-10-25).
- **N7.** An extension that depends on the core waits until the core's stories are in review, with a note on
  its card; no stacking across epics (2026-10-25).

Mine (say so if any should change):

- **D1. The initiative is a card, and the dialogue is its comments.** A board item at level `initiative`:
  the person's paragraph as the description (Markdown; links; files attached through the Chat uploads are
  linked by URL), one or more `repo:<owner>/<name>` labels naming the repositories it concerns, and an
  optional `budget:<hours>h` label. It is ready when a person sets it `approved` (planning-line D10). Three
  ways to write one: the Board screen (create at level initiative, approve); `task initiative -- --text "..."
  --repo owner/name [--budget 20]`, which creates and approves it; and the Chat's `/initiative` skill, the
  twin of `/story`: the chat role turns the conversation into an initiative text the person edits and starts,
  and the app posts it to the board. Product's questions are comments by `product`; the person answers in a
  comment, and the app, the chief and the task each call the turn route (D15) so the next turn runs at once;
  the OpenCode chief reaches the same card through two new tools of the `airlie mcp` server,
  `initiative({text, repo, budget})` and `answer({card, text})`, and follows the turn's job with `status`. One
  record, three doors. The card's comments distinguish a person from a role as `LocalBoard._is_note` does
  today: a comment whose author is not a role name is the person's.
- **D2. Questions: one at a time, at most five, each a reply the pipeline waits for.** The questions stage is
  one `Asker` turn of product per tick, each a fresh turn whose brief carries the Q&A so far: nothing is
  resumed from a saved conversation across processes (the record is the conversation, as in Noah's middle
  ground for the amigos), and within one tick a turn is continued through `follow_up` as `_ask_in_rounds`
  continues the architect's.
  The opening brief carries the initiative, the repositories' READMEs (the first 4,000 characters of each),
  the budget when there is one, and the rules: ask one open question about a real moment or a concrete
  picture; ask only what the code and the README cannot answer; never a list of options unless the person is
  stuck; stop when everything that matters is in the request. The reply is one JSON block, either
  `{"type": "question", "text": "..."}` or `{"type": "questions_done", "assumptions": ["..."]}`. A question
  is posted as a comment, the card moves to `review`, and the turn ends; the person's answer, posted as a
  comment, runs the next turn at once through the turn route (D15, N6), and the tick is the fallback that
  resumes an initiative whose last comment is a person's; either way the answer joins the Q&A in the next
  turn's brief. The first comment
  also says that "go" skips the rest, as the skill's first message does; a reply of "go" or "skip" ends the
  questions. The cap is `story.specs.max_questions` (5); at the cap product is told to state its assumptions
  and go on. An initiative whose text says to skip the questions gets none.
- **D3. Imagining: product leads in rounds, the specialists answer side by side.** After the questions,
  product gets the imagining brief, one tick's conversation that starts fresh with the Q&A, the survey notes
  as they come, the seats it may consult (`story.specs.consultable`, default `[architect, ux, design,
  security, docs, research]`, any role in `roles`), the epic contract (D5), the core-and-extensions rule (N5),
  the budget (D6), and the recent lessons for each repository (planning-line D7). Each round product replies
  with one JSON block: `{"type": "consult", "asks": [{"seat": "ux", "question": "..."}, {"seat": "design",
  "kind": "mockups", "epic": "E1", "brief": "..."}]}` or `{"type": "epics", ...}` (D5). The pipeline runs the
  asks together (`asyncio.gather`, each an `Asker._ask` of that role with the consult brief: the initiative,
  the Q&A, product's question, read-only tools, `story.specs.consult_steps` (8) steps; the engine batches
  them), and appends the answers to product's conversation as a follow-up (`orch.follow_up`), one section per
  seat, each capped at `story.specs.consult_chars` (4,000); the whole stays under
  `teams.fresh_continuation_tokens` (the eight consultations are about 8,000 tokens), so the conversation is
  never restarted mid-tick. Bounds: `story.specs.max_rounds` (3) rounds, `max_asks_per_round` (4),
  `max_consultations` (8) in all; at the last round product is told it must reply with the epics. The
  architect's survey (`survey_brief` over the initiative's text, `SURVEY_STEPS`) runs for every repository
  beside the first round without being asked, as `_survey` runs beside refine today, and its notes are in the
  brief of the second round (or the first, when the survey finishes first). Every consultation is an event and
  a record in the state (seat, question, answer, steps, seconds), and the epic document's "Consulted" section
  summarises them; they are not posted as comments. For an initiative that spans repositories, product's turn
  runs in a detached worktree of the first and the other READMEs are in the brief; the survey runs per
  repository.
- **D4. Mockups: three directions from ux, each rendered by design, served to the app.** A `mockups` ask
  (D3) runs two steps: ux writes `{"type": "directions", "directions": [{"name": "...", "idea": "...",
  "screen": "what is on it, top to bottom", "differs": "how it differs from the other two"}]}` with exactly
  `story.specs.mockup_options` (3) entries that differ in flow or layout, not in colour; then design renders
  one self-contained HTML page per direction, one call each (design's `max_tokens` holds one page, not
  three), with the look in the brief (D14: a hub text with airlie's tokens, or the repository's own look file; dark by
  default, no network, no script) and the direction's screen. The
  pages land in `initiatives/<key>/mockups/<epic id>/option-<n>.html`, are served by the flow API
  (`GET /v1/airlie/initiatives/{key}/mockups/{epic}/{file}`), and are the epic's `links` and
  `mockups.options`. The person picks by `POST /v1/airlie/epics/{key}/mockup {"option": 2}` (the app's
  button) or by a comment that reads "option 2"; the pick is written into the epic's JSON block
  (`mockups.picked`), a `mockup:2` label, and a `pick` event. An epic with options and no pick cannot be
  planned: the tick leaves it in `review` with a note, whatever its status. A comment about the mockups is a
  revision (D8) that draws three new options from the comment. An epic product marks `needs_mockups: false`
  waits for nothing (N4: an epic with no screen does not wait).
- **D5. The epics are product's final reply, one JSON contract (`story/initiative.py`, `EPICS_CONTRACT`):**
  ```
  {"type": "epics", "summary": "what the initiative is, in two sentences, in the person's terms",
   "epics": [
     {"id": "E1", "kind": "core", "title": "...", "why": "the value, in product's words",
      "goals": ["..."], "non_goals": ["..."], "anti_goals": ["..."],
      "decisions": [{"what": "...", "why": "..."}], "constraints": ["..."],
      "criteria": [{"id": "EC1", "text": "one line, at the epic's grain"}],
      "repositories": ["owner/name"],
      "stories": [{"title": "...", "size": "S", "scope": "one line"}],
      "risks": ["..."], "viability": {"verdict": "viable", "reasons": ["..."]},
      "needs_mockups": false, "depends_on": [], "open": ["what the planner cannot settle alone"]}
   ]}
  ```
  Rules the parser enforces (`parse_epics`, findings as `StoryError` text back to product up to
  `story.specs.attempts` (2), as `_usable` does): exactly one `kind: core`, zero or more `extension`; ids
  `E<n>` unique; `depends_on` names sibling ids, acyclic, an extension may depend on the core, never the
  reverse; at least one criterion per epic with unique ids; every story sketch has a `size` in `S|M|L` (the
  estimate needs it; P3 plans the real stories later); `verdict` in `viable|risky|not_viable`, with at least
  one reason; every repository is one of the initiative's `repo:` labels. A `not_viable` core is allowed:
  the epic is written and marked, and the person sees why (the brainstorming skill brings bad news to the
  person, it does not hide it). An epic's card is the document rendered from these fields in the spec's
  order (Why; Goals; Non-goals; Anti-goals; Decisions; Constraints; Acceptance criteria; Stories, a sketch;
  Risks; Viability; Estimate; Consulted; Questions asked; Open) closed by the JSON block P3 reads, which is
  P3 D1's shape with these keys added:
  ```
  {"type": "epic", "repositories": [...], "criteria": [...], "constraints": [...], "links": [...],
   "initiative": "AIR-40", "kind": "core", "depends_on": ["AIR-41"],
   "stories": [{"title": "...", "size": "S", "scope": "..."}],
   "estimate": {"hours": 12.5, "basis": "3 S at 2.1 h (median of 7), 2 M at 3.1 h (median of 4), x1.2 calibration", "at": "2026-10-24T18:02:00Z"},
   "viability": {"verdict": "viable", "reasons": ["..."]},
   "mockups": {"options": ["mockups/E1/option-1.html", "..."], "picked": null}}
  ```
  P3's `EpicSpec` gains these as optional fields and its parser keeps keys it does not know; a P3 epic
  written by hand (`task epic:add`) has none of them and plans as before.
- **D6. The price is the ledger's medians, calibrated, with its arithmetic shown.** `story/costs.py`:
  `StoryCosts(state_dir)` reads every `stories/<key>/events.jsonl` whose story reached `review`
  (published) and takes its wall-clock as the sum over its runs of last event minus `story_start`; its size
  is the `size` in its card's JSON block when P3 wrote one (P3's story card contract carries `size`; its plan's
  Task 6 is amended for this), else by built layers (1 or 2 is `S`, 3 or 4 `M`,
  5 and more `L`). `median_hours(size)` is the median over at least `story.specs.min_samples` (3) stories of
  that size, else the overall median scaled by `size_scale` (`S` 0.6, `M` 1.0, `L` 1.8), else
  `story.specs.default_hours` (`S` 1, `M` 5, `L` 8: the rough medians of today's ledger, eight published
  stories on 2026-10-24, a one- or two-layer story about an hour, three or four layers about five, five layers
  about eight) with the basis saying "defaults".
  `calibration` is the ratio of actual to estimated hours over finished epics (1.0 until three have
  finished), applied and named in the basis. `estimate(stories) -> Estimate(hours, basis, at)` is one line of
  arithmetic a person can check. The estimate is revised when P3 plans the real stories (`estimate.revised`,
  written by `EpicPipeline.plan` from the planned sizes) and closed when the epic is `done`
  (`estimate.actual`, the sum of its stories' wall-clocks; an `estimate_missed` event and a comment on the
  initiative when actual is over 1.5x or under 0.5x the estimate). `queue_hours(board, state_dir)` is the
  hours still ahead: for every epic `approved` or `doing`, its latest estimate times the share of its
  stories not yet done. A `budget:<h>h` label is in product's brief as the ceiling for the core; a core over
  it is one finding (`over_budget`) back to product once, then written as it is with the overrun named.
- **D7. The planner's read: the architect reads each epic as the planner it will meet.** After the
  mechanical checks pass, one `Asker._ask` per epic, read-only, `story.specs.read_steps` (6): "could you plan
  stories from this without going back to the person?", answered as `{"type": "read", "epic": "E1", "ok":
  true, "questions": ["..."]}`. The questions go back to product in one more round (the same conversation),
  which answers what it can into the epic (decisions, constraints, the sketch) and leaves the rest in the
  epic's `open` list, which the document shows as "Open for you" so the person answers them when they
  review. One round, as the skill allows the builder check one round; a second read does not happen.
- **D8. The person's turn: approve, pick, park, or comment; a comment is one revision.** The epics land
  as cards under the initiative, in product's order (core first), each born in `review`: the one line-made
  card that waits for a person, because the hours decision is theirs (N4; an exception to planning-line
  D10's "ready at birth", stated here). The initiative moves to `review` with a delivery note: each epic's
  key, kind, title, hours, verdict, and the queue's hours, one line each. The person sets an epic `approved`
  (P3 plans it on the next tick, after its pick when it has options), moves an extension to `todo` to park
  it, or comments. A person's comment on an epic in `review` is one revision round by product
  (`story.specs.revisions`, 1): product gets the revise brief, a fresh turn carrying the epic as written, the
  consultations' answers and the comment, and replies with a new `epics` block for that epic alone; the
  card's document and JSON block are rewritten (a `revision` event, a comment naming what changed), and a
  comment about the mockups redraws three options (D4). After the revision the card stays in `review` for
  the person; a second comment is read by nobody until P5's control surface, and the note on the card says
  so. An approved extension whose core is not yet in `review` is held (N7): the tick does not plan it, its
  card gets one note naming the core it waits for, and the tick plans it once the core epic is `review` or
  `done` (P3 D10: an epic is `review` when every story of it is in review or done). Its stories then build
  on the trunk as it stands; nothing stacks across epics. The initiative is `done` when every epic under it
  is `done` or parked, or when a person sets it so.
- **D9. The initiative has a record.** `initiatives/<key>/state.json` (the stage: `questions`,
  `imagining`, `reading`, `delivered`, `revising`; the Q&A; the consultations; the mockup directions and
  files; the epics as written and as priced) and `events.jsonl` (`initiative_start`,
  `question`, `answer`, `questions_done`, `survey_done`, `consult_round`, `consult` with seat, chars and
  seconds, `mockups` with epic and options, `epics_returned` with finding kinds, `planner_read` with epic
  and question count, `epics` with keys, kinds and hours, `pick`, `revision`, `estimate_missed`, `blocked`).
  Comments on the initiative: each question, the delivery note, the revision note. The flow API gains
  `initiative_flow(state_dir, key)`: the dialogue, the consultations, the epics with kind, hours, basis,
  verdict, options and pick, the queue's hours, and what the initiative waits on (`waiting_on: person |
  line`, with the pending question when there is one); the Vue work renders it.
- **D10. The tick learns the initiative, as the fallback.** Before P3's epic step: resume an initiative in
  `review` whose last comment is a person's (an answer, or "go") and whose turn route was not reached (the
  lock was held, or the app was closed); revise an epic in `review` whose last comment is a person's and whose
  revision is unused; take the top `approved` initiative with no state and start the phase. Then P3's steps,
  with two more checks: an `approved` epic with mockup options and no pick is left in `review` with a note; an
  `approved` epic whose `depends_on` epic is not yet `review` or `done` is held with a note and planned on a
  later tick (N7). Each of these is a tick's one piece of work and holds the story lock. The phase has the
  planning-line D9 budgets, as config under `story.specs`: `max_questions`, `max_rounds`,
  `max_asks_per_round`, `max_consultations`, `consult_steps`, `product_steps` (12, product's steps per round),
  `read_steps`, `mockup_options`, `revisions`, `attempts`, `min_samples`, `default_hours`, `size_scale`,
  `consult_chars`; `story.specs.enabled` (true) and `story.specs.product_role` (`product`). Product's role
  gets `max_tokens: 8192` and `max_context: 131072` in `airlie.yaml` (an epic set is longer than a story, and
  the imagining conversation holds the consultations) and `thinking` stays off: the brief is the thinking.
- **D11. The prompts are hub texts, one per moment.** `specs-questions` (the opening, the rules of a good
  question, the skip line), `specs-imagine` (the seats, the consult protocol, the epic contract's rules, core
  and extensions, viability and its reasons, the sketch and its sizes, the budget), `specs-consult` (for a
  seat: answer the question from the code and your craft, bullets, say what you could not find),
  `specs-mockup-directions` (ux), `specs-mockup-render` (design: one page, self-contained, the look),
  `specs-read` (the architect as planner), `specs-revise` (product with the comment), `specs-document` (the
  document's sections and their order); product's, ux's, design's and the architect's role prompts gain a
  paragraph each on the specs phase. Golden snapshots as for every hub text.
- **D12. Attachments are read, not only linked.** A file the person attached (a Chat upload linked in the
  initiative, owner-gated as uploads are) is resolved into the briefs the way the Chat resolves it: text
  and PDF as text into product's brief, capped as the Chat caps it; pictures into the briefs of the vision
  seats (ux and design) for the mockup directions and a critique, and listed by name in product's. "Make the
  board feel like Linear" with a screenshot is the common initiative, and nobody reads a link.
- **D13. A mockup page is checked, then served locked down.** Before a rendered page is kept, a mechanical
  check rejects any `<script>`, `<link>`, `@import`, or URL that is not `data:`; a page that fails goes back
  to design once with the finding, then is dropped, and the person sees the options that passed with a note
  (two good pages, never a bad one). The flow API serves a page with
  `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; img-src data:` and
  `X-Content-Type-Options: nosniff`, and the Vue work shows it in a sandboxed iframe: a model-written page on
  the app's origin runs nothing of its own.
- **D14. The look is a hub text, or the repository's own.** `specs-mockup-look` carries airlie's tokens
  (the `:root` of `src/airlie/serve/web/app.css`: background, surface, accent, type; dark by default) and is
  the default; a repository with a `docs/design/look.md` has its own look read into the brief instead, so an
  initiative for another repository mocks up in that repository's look. `needs_mockups` is product's
  judgment that the epic has a screen, whatever the repository.
- **D15. The turn route: a person's move runs the next turn now (N6).** `POST
  /v1/airlie/initiatives/{key}/turn` starts a serve job (`initiative_job`, as `story_job` runs a story
  under the lock) that does the phase's next piece: the first question of a newly approved initiative, the
  next question or the imagining after an answer, the revision after a comment on an epic. It takes the
  story lock; when the lock is held it answers `409 busy` with one line saying the tick will pick the
  initiative up within its cadence, and the comment stays on the card as the record the tick reads (D10).
  The app's reply box posts the comment, then calls the route; the chief's `answer` tool does the same and
  follows the job with `status`; `task initiative` creates and approves the card, then calls the route, so
  the first question comes back in the same minute. The route's job id is what `status` follows; between
  turns `initiative_flow` says what the initiative waits on. The epic's `mockup` route (D4) and a person
  approving an epic do not start a turn: approval is P3's tick, the pick is a record.

## Constraints

- Built strictly after P3 lands: P4 reuses `Asker`, `EpicSpec`, `EpicPipeline.plan`, the tick's epic steps,
  the lessons store from P1 and the `size` on P3's story cards; its tasks are additions on P3's names, and
  its tests fake P3's pipeline, not today's.
- The planning-line constraints apply: the initiative's text, the person's answers, the specialists' answers
  and the mockup directions are data; a repository name reaches a checkout only through the `repo:` labels
  and `story.repos_root`; a mockup page is served as a file, never executed by the line.
- The one-story lock stays; every step of the phase holds it. The engine is not restarted by anything here.
- Mockup pages are self-contained and fetch nothing: the app works on the tailnet with no internet.
- `BoardStore` and the board API are the only writers of cards; the pipeline rewrites an epic's description
  only through them.
- The front end is the Vue app's work, in parallel: P4 adds flow API rows, file routes and the pick route,
  and changes nothing under `src/airlie/serve/web`.
- The test gate of `AGENTS.md`; no live engine or network in tests (a `ReplayChatCompletionClient` or the
  `Asker` seam for the model turns).
- Models: mlx-community only; every seat here runs on the generalist as today, product without thinking.

## Left to the builder

- The field names of `Initiative`, `EpicDraft`, `Estimate`, `Consultation`; how the delivery note and the
  epic document read; the `task initiative` options.
- The wording of the eight hub texts and the role prompts' paragraphs.
- How the saved agent state is keyed in `state.json`, and what a resume does when the state is missing (start
  the stage again with the Q&A so far in the brief).
- The exact wall-clock arithmetic of `StoryCosts` for a story with a cancelled run between two finished
  ones, and how a story with no `size` and no built layer is counted (left out).
- How the chief's two MCP tools report a question that waits (the `status` text).

## Decided with Noah, 2026-10-24

1. The specs phase is the brainstorming skill run by product; an initiative becomes one or many epics (N1).
2. Product consults any specialist it needs, on demand, to reach direction, what it takes, viability and
   compute hours (N2).
3. A handful of questions to the person first, then the specialists, then the epics (N3).
4. The decision is compute hours against other priorities (N4), so the epic shows its price, its basis and
   the queue.
5. Core and extensions, each priced, trim instead of drop (N5).
6. A person's answer runs product's next turn at once through a serve route; the tick is the fallback when
   the lock is held (N6, D15; 2026-10-25).
7. An extension that depends on the core waits until the core's stories are in review, with a note on its
   card; no stacking across epics (N7, D8, D10; 2026-10-25).

Settled by me (say so if any should change): the initiative is a card and the dialogue its comments, with the
Chat skill and the chief's tools as doors (D1); five questions at most, one per tick, "go" skips (D2);
consultations are rounds of side-by-side read-only asks, not a group chat (D3); three directions from ux, one
page per direction from design, a pick required before planning (D4); one `not_viable` core is written and
marked, never hidden (D5); medians by size from the ledger with a calibration factor and the arithmetic in the
card (D6); the architect's read is one round (D7); epics are born in `review`, the one line-made card that
waits for a person (D8); one revision per comment, then the card waits (D8); turns are fresh across ticks with
the record in the brief and continued within a tick (D2, D3, D8); attachments are resolved into the briefs
(D12); mockup pages carry no script and are served locked down (D13); the look is a hub text or the
repository's own file (D14); P4 is built after P3.
