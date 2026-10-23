---
kind: rule
roles: [any]
fields: [worktree_hint]
governs:
  repo.edit: change
  done.when: tests-pass
  answer.form: summary
yields_to:
  - brief-survey-task
  - tests-only
  - role:aggregator
  - role:architect
  - role:chat
  - role:chief
  - role:design
  - role:docs
  - role:hypothesizer
  - role:judge
  - role:product
  - role:research
  - role:reviewer
  - role:security
  - role:summarizer
  - role:supervisor
  - role:tester
  - role:ux
why: |
  The closing instruction of the goal message since the native team loop was built (M2, 2026-10-12,
  17a912f): the role works in a git checkout, uses the tools, and stops with a short summary when the
  tests pass. `_goal_message` puts the preamble, `Goal:`, the goal and `You are <team>.` before it; a
  role with no tools gets goal-no-tools instead.
---
You are working inside a git checkout of the repository ({worktree_hint}). Use the tools to inspect and change it, run the tests, and stop with a short summary when they pass.
