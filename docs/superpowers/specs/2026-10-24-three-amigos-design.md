# The three amigos: product, architecture and testing agree a story's requirements, changes and proof

Status: draft for Noah, 2026-10-24. Sub-plan P2 of `2026-10-24-planning-line-design.md`; it builds on P1
(`docs/superpowers/plans/2026-10-24-layer-package.md`) and changes how a story goes from refine to its
first layer.

## Why

A story today is refined by product alone and split by the architect alone, and the first time anyone checks
that the criteria can be tested, or that two of them agree with each other, is the tester inside layer N,
twenty steps deep. AIR-22 layer 02's tester spent forty minutes on a contradiction between AC3 ("the sizing
rule is always appended") and AC4 ("the brief is byte-for-byte as before"); AIR-22's architect had its
second plan refused for listing one criterion under two layers. Fourteen of fifty-seven story blocks were a
layer's tests failing the pre-code check after a retry, and most of those tests were testing the wrong thing,
at the wrong level, or a criterion that could not be made red.

Noah's framing (2026-10-24): the goal is for product, architecture and testing to agree, within the context
of a story, on three things: the feature's requirements, the changes that will be made, and how delivery will
be proven. Testing's job includes knowing the repository's testing guidelines, so each proof lands at the
right level of the test pyramid.

## Goals

- Every story that reaches a layer carries an **agreement**: the criteria as product finally words them, the
  layers as the architect finally splits them (with the interfaces at their boundaries), and a **proof plan**
  that names, per criterion, the tests that will prove it, where each lives, at what level of the pyramid,
  under which of the repository's guidelines, what it asserts and what state it needs.
- The three parts are checked against each other before any layer is built: a criterion with no proof, a
  proof at the wrong level, a layer whose scope names nothing that exists, two criteria that contradict each
  other, all come back in the meeting, not in a layer.
- The architect's layer package (P1) is transcription of the agreed proof: its tests are the ones the proof
  plan names, and a tester critic reads them as code before the mechanical checks run.
- No person in the meeting. When the rounds run out, each role has the last word on its own part and the
  story proceeds, with the objections on record.
- The meeting's cost is visible per story: calls, tokens, wall-clock, rounds, and which objections were
  later borne out.

## Non-goals

- A fourth or fifth amigo (ux, design, security). They join at the epic (P4) or as later critics; the room
  here holds three.
- Changing refine. Product's story stays the meeting's first requirements proposal; the meeting may revise it.
- A human gate. The only pause the line has is the mockup pick (planning-line N4).
- Replacing the mechanical checks with model judgement. What a program can check, a program checks first.
- Jira; parallel meetings; the React screens (the agreement is a card comment and state; the flow API gains
  the meeting's events; the React work renders them).

## Anti-goals

- A meeting that ends by exhaustion with no decision. Owner decides, and the card says who objected and why.
- A critic that rewrites another role's part. A critic objects with a claim and a suggestion; only the owner
  changes the text.
- Agreement by sameness. The tester critic runs on the cross-family model (Muse), as the reviewer does, so
  its reading of the architect's split and tests is not the architect's own.
- A proof plan with tests the repository cannot run: a level that is no directory of its test tree, a
  guideline the library does not have, a test that needs a service the gate does not start.

## The shape

```
 refine (product) ──► story v1 ─┐
 layers (architect) ──► split v1 ┼──► the story meeting ──► AGREEMENT ──► per layer: the package meeting ──► P1 checks
 survey (architect) ─────────────┘      product · architect · tester            architect writes the tests as code,
                                        proof plan proposed, three reviews,     the tester critic reads them against
                                        owners revise, ≤ rounds, owner decides  the proof plan and the guidelines
```

**The story meeting.** One meeting per story, as an AutoGen group chat (N5): product, the architect and the
tester are three agents in one shared conversation, with airlie's checker as a fourth, model-less
participant. The conversation opens with what exists, product's story (requirements v1) and the architect's
split (changes v1), both as today, and the tester's first turn adds the proof plan (proof v1), written with
the repository's test tree, gate and guideline library in view. From there any agent may address any other:
object to a part with a claim and a suggestion, ask a question, revise its own part, or agree. Every turn
ends with the agent's current verdict on the three parts. The checker speaks after every turn that changed
a part and posts the mechanical findings (D4) or nothing. The meeting ends when the latest turn of each of
the three agrees to all three parts, or at `story.amigos.max_turns`; then each owner's current version
stands and the objections still open are recorded. A story stops only when product says the request cannot
be built as written (today's `blocked` reply).

**The package critic turn.** Not a second meeting. Just before a layer is built, the architect writes the
layer's package (P1) from the agreed proof: its tests are the proof plan's tests for that layer, as code.
The tester critic reads the test files against the proof plan and the guidelines (right level, right
assertions, the state the plan named, nothing weakened, nothing added that the plan does not name) and
returns a verdict in one turn; an objection gives the architect one revision before the mechanical checks
run. Product is not in it: the requirements were agreed, and a package that strays from them fails the
proof check below.

## Decisions

Noah's (2026-10-24):

- **N1.** The goal of the meeting is agreement on requirements, changes and proof, within a story.
- **N2.** Testing's job includes the repository's testing guidelines, so each test is at the appropriate
  level of the pyramid.
- **N3.** The playback of 2026-10-24 ("this looks right"): the meeting may revise the story's criteria; each
  role owns one part and has the last word on it when the turns run out; the tester critic is cross-family;
  the meeting is one conversation with turns.
- **N4.** Product stays on the generalist for the meeting.
- **N5.** One meeting per story, in an AutoGen arrangement: any agent can talk to any other, and they reach
  the agreement within that one meeting. Not rounds of propose, review and revise driven from outside.

Mine (say so if any should change):

- **D1. The agreement is a document on the story's card and in its state.** `StoryState.agreement` holds
  `{"story": <the revised Story>, "layers": [<Layer with interfaces>], "proof": [<ProofTest>], "rounds": n,
  "unresolved": [<Objection>]}`; the card gets one comment with the agreement rendered for a person
  (criteria, layers, the proof table) and one with the unresolved objections when there are any. The layers'
  stage is renamed in the state from `layers` to `agreement`; a resume after it reads the agreement.
- **D2. The proof plan is the tester's part, as a JSON contract beside its parser (`story/amigos.py`):**
  ```
  {"type": "proof", "tests": [{"criterion": "AC1", "layer": "01", "file": "tests/unit/test_x.py",
    "name": "test_what_it_proves", "level": "unit", "guideline": "unit",
    "asserts": "the exact value, message or state users observe", "needs": "fixtures, fakes, state"}]}
  ```
  `level` is a directory of the repository's test tree (`tests/<level>/`, read from the base commit) and
  `file` lies under it; `guideline` names a file of the role's guideline library (`roles.tester.skills`,
  `skills/qa` in airlie: unit, functional, contract, integration, e2e, component, visual-regression,
  mutation, ai-skill) when the repository has one, else is omitted; a criterion with no test, a `layer`
  that is not in the split, a level that is no directory, a guideline that is no file, or two tests with
  one id, is refused at parse and the tester is asked again with the reason. For a layer that owns no
  criterion, tests carry `"criterion": null` and prove what its brief adds.
- **D3. Every turn ends with the speaker's verdict, one JSON object:**
  ```
  {"type": "verdict", "requirements": "agree" | "object", "changes": "agree" | "object", "proof": "agree" | "object",
   "objections": [{"part": "proof", "about": "AC2", "claim": "one sentence", "suggest": "one sentence"}]}
  ```
  A revised part travels in the same turn, as its own contract (the story JSON, the layers JSON, the proof
  JSON) before the verdict. A role never objects to its own part. An `object` with no objection for that
  part, or a turn with no verdict, is a checker finding on the next turn, not a stop.
- **D4. Mechanical checks are a participant.** airlie's checker is a fourth agent with no model: after every
  turn that carries a revised part it re-parses the three parts and posts the findings, or nothing. On the
  split: every criterion under exactly one layer (today's parse), every file path exists on the base or is
  marked new, the layers are at most `story.max_layers`, and each layer names at most
  `story.amigos.max_scope_files` files (the sizing rule made a number; default 5). On the proof: D2's rules,
  plus every criterion has at least one test and every layer has at least one. On the three together: a
  proof test's layer owns its criterion; a stale reference after a criterion was dropped or added (D13). The
  owner's next turn is expected to fix a finding; a finding that stands at the end is recorded with the
  unresolved objections.
- **D5. Who runs where.** Product is the `product` role on the generalist (N4), as in refine, since it owns the
  requirements and must speak in refine's voice. The architect is the `architect` role on the generalist, with
  the code in view (its survey and the base). The tester is the `tester` role, re-pointed at the reviewer
  model (Muse Glimmer) with read-only tools and its prompt rewritten for the critic's job; P1 retired its
  authoring role, so the seat is free.
- **D6. The tester reads the repository's testing conventions before it proposes.** Its brief carries the
  test tree to depth two with a file count per level, the gate's commands, the guideline library's summaries,
  and the full text of the guideline Kev routes for the story (today's `_skill` routing, reused), plus the
  repository's own testing notes when the base has `docs/testing.md`, a `## Testing` section in `AGENTS.md`
  or `CONTRIBUTING.md` (read as data, clipped). The level a test is proposed at must be one the tree has: a
  level is a directory under the test root that holds a test module (`test_*.py`, `*_test.py`) in it or
  below, `fixtures`, `data` and caches excluded (airlie: `unit`, `integration`, `e2e`, `live`); a
  repository with one flat `tests/` has one level, `tests`. A level with no guideline in the library (`live`)
  is allowed with `guideline` omitted.
- **D7. The meeting is `story/amigos.py`: one AutoGen group chat, three agents and the checker.** It
  reuses what `teams/autogen_team.py` and `teams/autogen_client.py` already give the AutoGen orchestrator:
  an `AssistantAgent` per role whose system message is the role's prompt, `RepairingOpenAIClient` per agent
  pointed at the engine with the role's served model (so product and the architect decode on the generalist
  and the tester on Muse), `_RegistryTool` for read-only repository tools confined to a detached worktree at
  the story's base, and the slot scheduler. The team is a `RoundRobinGroupChat` in the order product,
  architect, tester, checker (round robin, not a selector model: a turn that has nothing to say ends with its
  verdict alone, which costs one short reply, and no call is spent choosing a speaker); the termination
  condition is custom: the latest verdict of each of the three agrees to every part, or `MaxMessageTermination`
  at `story.amigos.max_turns` (default 12).
  This is the middle ground between fresh turns and continued conversations: each agent's conversation
  continues for the whole meeting (its system prompt, then the shared history, append-only, so the engine's
  prefix cache serves every turn after the first), and what it reads of the others is their artefacts and
  their words in the meeting, never their reading transcripts. The architect's twenty steps of reading from
  the layers stage are not in the room; its split is, as the summary of that reading, and it may read again
  in the meeting with its tools, bounded by `max_tool_iterations`. The meeting's shared history is saved to
  the state after every turn; a story that crashes mid-meeting resumes it by replaying the saved history
  into fresh agents (AutoGen's `load_state`), which the team loop already does for a resumed run.
  The client must pass each role's `thinking` and `thinking_budget` through (`extra_body` on the create
  call, which `RepairingOpenAIClient.create` can add); today's AutoGen path takes the server's defaults, and
  the meeting needs the architect and the tester to think as their roles are configured. *(My call; the
  builder wires it.)*
- **D8. Owner decides, on record.** When the meeting ends at `max_turns` with a part still objected to, the
  owner's current text stands; each open objection is an `amigos_unresolved` event and a line in the card's
  comment. The agreement records which parts were decided by their owner.
- **D9. The package meeting is one critic turn.** After the architect's package call (P1) and before the
  mechanical checks, the tester critic is asked once with the package's test files and the proof plan for
  that layer, and answers a `verdict` on `proof` alone. An objection is the architect's one revision
  (`package-fix`, with the objection as the finding), then the checks run. The checks gain one rule:
  `covers` must name every proof test of the layer, by id, and no other (`proof_mismatch`).
- **D10. Every objection and its outcome is an event in the story's log.** `amigos_objection` (role, part,
  about, claim, round), `amigos_resolved` (the owner revised and the objector agreed), `amigos_unresolved`,
  and later `amigos_borne_out`: when the layer a criterion belongs to comes back from a check or re-plans
  for a reason that names the same criterion or test id, the earlier objection on it is marked borne out.
  The record is the events, not a table; `task amigos:stats`, which scans them per role and part, is P5's,
  and P2 is done without it.
- **D11. Lessons reach the room.** The architect's and tester's briefs carry the repository's recent lessons
  (P1's store), and the meeting records a lesson when it ends by owner-decides (ending `owner_decided`,
  detail: the parts).
- **D12. The proof after a re-plan.** The proof plan is the tester's; the test bodies are the architect's.
  Every package, first or re-planned, must cover exactly the proof's test ids for the layer
  (`proof_mismatch`), and the tester critic reads every package, judging "weakened" against the proof's
  `asserts`, with the coder's account quoted as data, so a body that asserted the wrong value is fixed and
  passes. When the architect's re-plan finds the proof itself wrong, its package JSON carries
  `"proof_change": [{"test": "<id>", "why": "..."}]` instead of a package; the pipeline then asks the tester
  for a one-layer proof revision as a single fresh ask (D2's contract, that layer's tests only, the
  architect's reasons as data; not a new meeting), records `amigos_proof_revised`, and asks the architect
  again. P1's `_ready_package` gains that branch; its lesson for the re-plan keeps its ending (`gate_red`)
  and adds `proof_revised` to the detail.
- **D13. Criteria keep their ids.** Product may reword a criterion under its id, add one (the next number)
  or mark one `"dropped": "why"`; it never renumbers. A dropped or added criterion makes the split's
  `acceptance` and the proof's `criterion` references stale; the checker posts that, and the architect's and
  the tester's next turns fix it. The round robin gives the owners their natural order, requirements then
  changes then proof, each seeing the parts before it. At the end the agreement's story replaces `st.story`,
  the card gets a comment saying which criteria the meeting reworded, added or dropped, and every later
  brief and PR body reads the one story.
- **D14. A layer names its files.** `Layer` gains `files: tuple[ScopedPath, ...]` (`{"path": "...",
  "new": true|false}`) and `interfaces: tuple[str, ...]` (the signatures at its boundary that later layers
  import); `scope` stays as prose for people and old states (`from_dict` defaults the new fields to empty,
  and D4's path check then falls back to `named_paths(scope)`). `LAYERS_CONTRACT` and the architect's rules
  ask for both; `same_layers` still compares criteria only; P1's `map_focus` and the package brief use
  `files` when present.

## Constraints

- The planning-line constraints (local models, the board's vocabulary, the gate, Taskfile, the hub, the
  lock) apply. Everything a role says is data; the proof plan's `file` and `level` are checked against the
  tree before they are used in a path.
- The meeting runs on the story's base commit in detached worktrees, as refine and layers do; it writes
  nothing to the repository.
- P1 lands first: the agreement's layers feed P1's `_build`, the proof plan feeds P1's `package_brief`, and
  the tester critic's turn sits between P1's package call and its checks.

## What this changes in P1

- `_ready_package` runs the tester critic's turn between the package call and the checks (D9), handles a
  `proof_change` reply (D12), and the checks gain `proof_mismatch`.
- `package_brief` carries the layer's proof tests (ids, asserts, needs, level, guideline) and the layer's
  `files` and `interfaces` (D14); `map_focus` prefers `files`.
- `_layers` becomes the meeting's opening split, and the pipeline's `layers` stage becomes `agreement`
  (D1); `_build` reads the layers from the agreement.

## Left to the builder

- Field names of `Agreement`, `ProofTest`, `Objection`; how the agreement renders as a card comment.
- The opening message of the meeting (how the story, the split and the tester's task are laid out), the
  three role prompts for the meeting and the checker's wording, as hub files.
- How the checker finds the latest version of each part in the shared history (the last turn of its owner
  that carries that contract) and how the termination condition reads the verdicts.
- How the test tree is read (depth, which directories count as levels; `tests/fixtures` and `tests/data`
  are not levels) and how a repository's testing notes are found and clipped.
- `max_tool_iterations` per agent in the meeting, and how the saved history is replayed on a resume.

## Decided with Noah, 2026-10-24

1. Product stays on the generalist (N4).
2. One meeting per story, as an AutoGen group chat in which any agent can address any other, ending in
   agreement or at `max_turns` (N5); the budget is the turn cap, default 12, not rounds per part.
3. Continued conversations with artefacts as the summary, not fresh turns (D7), in answer to Noah's
   question whether summaries in a continued conversation were the middle ground: they are.

Settled by me (say so if any should change): the checker is a model-less participant (D4); the proof's test
ids are the tester's and the bodies the architect's, with a `proof_change` path when the architect finds the
proof wrong (D12); criterion ids are stable (D13); a layer names its files and boundary interfaces (D14);
the record is the story's events and the stats task is P5's (D10); the client passes each role's thinking
settings through (D7).
