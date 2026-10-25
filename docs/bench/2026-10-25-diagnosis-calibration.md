# The diagnosis rules, calibrated on the recorded blocks (2026-10-25)

`scripts/calibrate_diagnosis.py` gathers and classes every block of every story in
`~/.local/state/airlie/stories` (planning-line P5a, `airlie.story.diagnosis`). It found 58 blocks (the
performance review of 2026-10-23 counted 57; one story blocked since). The rules are in
`diagnosis.classify`; seven of the blocks are fixtures in `tests/unit/data/blocks` that pin them
(`scripts/block_fixture.py`).

## The distribution

| class | blocks | kinds |
|---|---|---|
| content | 44 | gate_failed 21, precode_failed 14, changed_nothing 7, not_actionable 1, unusable_reply 1 |
| config | 8 | max_steps 4 (a planning role out of steps), cut_off 4 (a coder cut off at its token cap) |
| transient | 4 | engine_error 3, fetch_failed 1 |
| process | 2 | hold_trap 2 (AIR-17 layer 02, both blocks) |
| environment | 0 | none of the recorded blocks had a gate that was red on the base |

## Against the performance review's table

The review's table (27 gate, 14 pre-code, 9 changed nothing, 5 roles unfinished, 2 GitHub) was read from the
reasons' text. The rules read the evidence, so the same blocks fall differently, and the sums agree:

- **Gate, 27** = content/gate_failed 21 + the 4 coders cut off at their cap (config/cut_off: AIR-31 #4,
  AIR-5 #4th #4, AIR-7 #2, HDS-5 #8) + the 2 hold traps (process/hold_trap: AIR-17 #1 and #2). Their reasons
  all read "fails on"; their causes are not the gate.
- **Pre-code, 14** = content/precode_failed 14 (the 15th text match, AIR-9 #3, is a tester whose engine
  stopped: transient/engine_error).
- **Changed nothing, 9** = content/changed_nothing 7 + the 2 coders whose engine stopped (AIR-28 #1, AIR-31
  #1: transient/engine_error). The layer changed nothing because the engine failed under it.
- **Roles unfinished, 5** = config/max_steps 4 + content/unusable_reply 1 (HDS-3: the architect's reply had no
  JSON).
- **GitHub, 2** = transient/fetch_failed 1 (AIR-1) + content/not_actionable 1 (AIR-7 #1, product's "cannot be
  built as written", which the table counted with the stops that are not the model's work).

## What the rules got wrong, and what is left

- **The two engine stalls are classed transient, not content.** The plan expected AIR-31 layer 02's "changed
  nothing" to be content. Its coder stopped with `engine_error` (the engine failed under it); retrying is the
  right answer and a re-plan is not. `engine_error` is a stop reason of a subtask, not an event kind (the plan
  named it as one), so the pack records it as a failure line and the rule reads that. The fixture
  `engine_stall` pins it; the `changed_nothing` fixture is HDS-5's seventh block instead.
- **Hold traps before 2026-10-20 cannot be seen.** AIR-7 #3 and #6 to #8 ended `repeated_action` with
  write-deadline misses but no `read_refused` events (the event did not exist yet), so they class content/
  gate_failed. Only AIR-17's two blocks, after it did, class process/hold_trap. From now on a hold trap that
  refuses three reads is a fix card.
- **The last subtask decides the stop reason.** AIR-30 #3 had its tester cut off and its coder finish
  (`['cut_off', 'done']`): it classes content/gate_failed, and the cut-off tester is in the pack's counts
  (`refused=10`) but not the class. A block where an earlier subtask's stop is the cause would need the rules
  to read every subtask; no recorded block shows a case that pays for it.
- **Old content blocks have the stage as `where`.** The gate's output was not on record before this change
  (`gate_tail` events, and `output` on the studio's `gate_failed`); the 21 gate blocks therefore sign as
  `content:gate_failed:layers`. New ones carry the failing test id.
- **Nothing recorded is a traceback or `process` through a bug in airlie's code.** The `error` event is rare
  (none in these 58); the traceback rules are pinned by synthetic cases, not by a recorded block. The first
  real one will be the first calibration of that rule.
