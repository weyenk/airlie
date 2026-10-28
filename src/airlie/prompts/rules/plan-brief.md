---
kind: rule
roles: [architect]
fields: [max_stories, max_scope_files]
governs:
  plan.unit: smallest-reviewable-story
  reading: first
why: |
  The opening of the architect's story-plan brief, the call that turns an approved epic into stories (the plans
  phase, P3 of the planning line, 2026-10-24). It is Noah's framing of `superpowers:writing-plans` done by the
  architect: the plan is for people who have not seen the code or the spec, a unit is the smallest one with
  its own reviewer's gate, files and interfaces are named, criteria are the proof, order follows
  dependency, constraints are copied and risks pinned. The size numbers are story.plan.max_stories and
  max_scope_files, which the parser enforces too.
---
Turn this epic into a story plan, the way a careful engineer writes an implementation plan for people who have not seen the codebase or the spec: read the code the epic touches first, then write the smallest units of work that can each be delivered and reviewed on their own. A story is the smallest unit that ends in a pull request stack a reviewer can judge alone: too small and its criteria cannot be judged without the next story, too big and it takes two reviewers' afternoons. For each story name the files it touches, the interfaces it consumes from earlier stories and the ones it produces for later ones, and its acceptance criteria, which are the proof that it is done. List the stories in dependency order, each after every story it depends on, and mark a dependency only where the story needs the code of another. Copy the epic's constraints into the plan, and pin each risk the epic implies to the acceptance criterion of the one story that would catch it. Do not decide how the code reads; that is for the people who build each story. A story is later split into layers that a coder builds one at a time within the budget given below, so a story that would need many layers is too big. Plan at most {max_stories} stories, each touching at most {max_scope_files} files.
