# Test suite by layer: the prune (Task 1)

Base: main at 7ddcdf0 plus the plan commit a2542de. Run before the prune:
`uv run pytest -q -n auto --maxprocesses 8 tests/unit --quarantined include -p no:randomly` gave
4262 passed, no failures or errors, and exited 1 on four stale `quarantine.toml` entries: tests #270 deleted
(after #271 had quarantined them) whose lines stayed.

Searched for dead tests: every `airlie.*` name a test imports that nothing in `src/` uses; tests naming
vllm-mlx, llama-server, the engine patches and parsers, the Kev decision engine, the v1 workflow keys
(`next`/`branch`/`parallel`); import fallbacks and skips; names `src/` keeps "for a release". The engine swap
(#270) and the workflow refactor (#261/#263) had already removed or rewritten their tests; what is left is below.

| node id | verdict | reason |
| --- | --- | --- |
| `tests/unit/test_hybrid_prefix_cache.py::test_a_rotating_clone_written_in_place_leaves_the_original_alone` | delete (quarantine line) | the file left with `engine/patches/hybrid_prefix_cache.py` in #270; only the stale quarantine line was left |
| `tests/unit/test_hybrid_prefix_cache.py::test_a_store_that_fails_is_a_warning_and_the_prefill_still_returns_its_logits` | delete (quarantine line) | as above |
| `tests/unit/test_hybrid_prefix_cache.py::test_the_patch_replaced_both_prefill_functions_and_the_layer_copy` | delete (quarantine line) | as above |
| `tests/unit/test_process.py::test_start_times_out_with_loading_state` | delete (quarantine line) | the test was deleted in #270 with the vllm-mlx process code; only the stale quarantine line was left |
| `tests/unit/test_story_amigos_agreement.py::test_the_agreement_is_said_on_the_card_with_criteria_layers_and_the_proof_table` | delete | pins behaviour removed on purpose: #252 (AIR-59) stopped posting the agreement note on the card; no `src/` code calls `amigos.agreement_note` now. `test_story_pipeline.py` keeps the test that the note is not posted |
| `tests/unit/test_address.py` (all 10 tests, via `_parse_address`) | fix | the helper's `except ImportError` fallback to `airlie.story.board.parse_address` is a shim to a path that never existed; the tests import `airlie.story.address.parse_address` directly, bodies unchanged |

Kept, though no other `src/` code calls the name they import (each is a thin wrapper over live code, or a
deliberate test helper): `amigos.latest_parts` (over `Standing`), `package.test_files_in` (over
`_test_blocks`), `diversify.nudge_text` (over `branch_nudge`), `epic_plan.plan_id_of`, `lessons.ENDINGS`,
`supervisor.answer.CATEGORIES`, `train.reward.loss_terms`, `bridge.clock.FakeClock`,
`learned.tiny_test_backbone`. `test_cli_story.py::test_story_tick_still_works_and_says_it_is_line_tick_now`
covers the `story tick` alias kept "for a release", which `Taskfile.yml` `story:tick` still calls, so it stays.
`airlie.story.address.parse_address` itself has no caller in `src/` (the @-address relay was never wired);
its tests stay until that module is kept or removed.

## Triage counts after the prune

No triage row names a deleted test and no file left the suite, so the per-file verdicts stand (pruned copies:
`triage-a.pruned.csv`, `triage-b.pruned.csv`, `triage-merged.pruned.txt`; `test_process.py`'s quarantined count
goes 1 → 0):

| files | unit | integration | functional | contract | mixed |
| --- | --- | --- | --- | --- | --- |
| 204 | 93 | 24 | 11 | 4 | 72 |
