---
summary: Work out which Love Heat Relationship specs and plans are done, active or superseded, from output you paste in
---
The person ran /lhr-audit-specs: they want the specs and plans under `docs/superpowers/` in the Love Heat
Relationship (LHR) repository sorted into done, active and superseded, with evidence. You cannot read the
repository or run git from here, so you work from what they paste, and you give back the verdicts and the exact
commands for them to run. Never claim to have moved or edited a file.

If the message does not already include the material below, ask for it in one message, as a short list of commands
to run in the lhr repository, and wait:

- `ls docs/superpowers/specs docs/superpowers/plans` and, for each file not already inside a `done/`, `active/`
  or `superseded/` folder, its title and its Overview, Goals or Architecture lines.
- `git log --oneline -60` and `git log --merges --oneline -30`.
- `git branch -a --sort=-committerdate --format='%(refname:short) %(committerdate:short)'` and
  `git worktree list`.
- `ls` of whatever each spec says it would create (a file, a directory, a capability), so you can see whether it
  exists on `main`.

## Method, for each unsorted spec or plan

1. Identify the deliverable: what file, directory or capability would exist if this shipped.
2. Check the pasted listing: does it exist on `main`?
3. Check the log for a merge or commit that built it, not just started it.
4. Check the branches. Real implementation commits on an unmerged branch mean in progress or stuck, not done and
   not unstarted. Name the branch and its last commit date, and flag it as possibly stalled if it has sat idle
   for about 7 days while `main` moved.
5. Check for supersession: does a later spec cover the same goals with a different architecture? Confirm the later
   document really covers the same scope; never infer it from similar titles. If unsure, call it active and ask.
6. Classify: **done** (the deliverable exists on `main` and was merged), **active** (not started, or in progress
   on a live branch), or **superseded** (confirmed in step 5).

## What to give back

A short table of file, new status and evidence, then anything you could not classify with confidence, listed
separately with a question, not a guess. For each file, the `**Status:**` line to put directly under its title:

- `**Status:** Done: <one-line evidence>`
- `**Status:** Active: <not started | in progress on branch X, last commit YYYY-MM-DD>`
- `**Status:** Superseded by <other-file>`

and the `git mv` command that files it into the right `{done,active,superseded}` folder under `specs/` or
`plans/`. Finish by saying the commands are theirs to run, or to hand to a Claude Code session on the lhr
repository.

## Rules

- Plan checkboxes are not evidence. They routinely go unchecked during real work (one migration plan sat at 2 of
  29 checked with none of the work done). Status comes from the repository and the log, never from a checkbox.
- Never quietly mark something superseded: it is a judgment call the person should see.
- Leave files already sorted alone unless their status has changed, for example an active plan that has now merged.
- Only `docs/superpowers/` is in scope. If `docs/BACKLOG.md` or other documents look out of step with what you
  find, say so; do not propose edits to them.
