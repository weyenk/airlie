# roman on the composer (2026-11-07)

Task 13 of `docs/superpowers/plans/2026-11-06-generic-nodes-part-1.md`: the roman story run through the
workflow composer (`story.composer: true`) on the real engine, no publish.

- Run: `task bench:story -- roman --no-publish -c airlie.bench.yaml` on bb70386 (bench config: `airlie.yaml`
  plus `package_tools: true`, `package_steps: 32`, `composer: true`).
- State: `~/.local/state/airlie-bench/roman/20261107-094424`. Record: `story-roman-20261107-105254.json`.
- Verdict: **within its budgets**. First try, two layers built, no replans, no gate failures, no blocks.

| phase | min |
|---|---|
| survey done | 2.0 |
| plan done (one round; the plan's own verdict ended it, `plan_check`) | 4.8 |
| meeting (cap reached, owner decided) | 19.8 |
| layer 01 built | 47.7 |
| layer 02 built, story done | 68.5 |

The story's `events.jsonl` carries `node_started` / `node_finished` / `node_skipped` for every node of
`config/workflows/story.yaml` and `layer.yaml`, in the diagram's order: intake, base, gate, refine with
survey, layers, amigos, planned, then per layer covered, drafts (01 only), package, build, and publish,
review.

## Event-kind diff against the last driver run

Compared with the recorded driver run of 2026-10-31 (`story-roman-20261031-063020.json`, state
`20261031-055944`), kinds minus `node_*`. Every difference has a cause other than the composer:

- **Story shape.** This run planned two layers and the meeting ran to its turn cap (`amigos_owner_decided`,
  `amigos_unresolved`, `lesson_recorded`); the October run planned one layer and agreed (`amigos_agreed`),
  and its package needed a retry (`retry`, `package_returned`, `package_resumed`).
- **Merged since 2026-10-31.** `amigos_start`, `package_draft_start`, `package_written`,
  `package_checks_start`, `brief`, `test_checks_skipped` come from work that landed in the meantime.
- **This run's flags.** `publish_skipped` instead of `published` (`--no-publish`); `plan_check` from the
  plan's own verdict, turned on in bb70386.

No kind is unique to the composer path except the node events. The same-commit equivalence is pinned by the
side-by-side driver/composer tests in `tests/unit/test_story_flow.py`, which compare full event dicts and
`state.json`; the bench shows the composed story runs end to end on the real engine.
