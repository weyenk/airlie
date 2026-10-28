---
kind: rule
roles: [architect]
fields: [max_stories, max_scope_files]
governs:
  plan.unit: smallest-reviewable-story
  replan.kept: untouched
  reading: first
why: |
  The opening of the architect's re-plan brief, the call that revises an epic's story plan after a story the
  line built stopped twice, or on `task epic:replan` (D8 of the plans phase, P3 of the planning line,
  2026-10-24). A re-plan that rewrote everything would redo work a reviewer is already reading, so it says what
  is kept, what may be changed, dropped or replaced, and that a replacement does the stopped story's work
  another way.
---
This epic was planned into stories earlier, and some have been built, are in review, are merged or have stopped. Plan again from where it stands, the way you would revise an implementation plan after its first tasks met the code. A story that has started, is in review or is merged, and a story that stopped with a layer built, are kept exactly as they are: do not list them and do not write a story that redoes their work, but a new story may depend on one by its id. List only the stories still to be done: a story that has not started that you keep or change (the same id), and new stories with new ids, including the ones that replace a stopped story with no layer built, which is retired. A story that has not started and that you leave out is dropped. A replacement must do the stopped story's work another way, given what its stop says: smaller, split differently, or with its files and interfaces named more exactly. Every epic criterion must still be covered by a kept story or one you list. Plan at most {max_stories} stories, each touching at most {max_scope_files} files.
