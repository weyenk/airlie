"""The check on a satisfied reply (review of PR #244, 2026-11-06): a layer is let off building only on
evidence. Each earlier layer the reply names must have been built below the layer, and each test it names
(path::name) must exist and pass on the layer's base, run with the red check's machinery (the gate's pytest
step, confined, in a detached checkout of the base removed after; a Vitest file with `vitest_red`). What does
not hold is a `satisfied_unproven` finding, which goes back to the architect as a package's findings do. The
tests' text is never imported here."""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from pathlib import Path

from airlie.story.js_tests import vitest_red
from airlie.story.layer_checks import ChangedTest, Finding, PytestChecks, Said
from airlie.story.package import Satisfied
from airlie.teams.worktree import add_worktree, remove_worktree
from airlie.tools.scope import is_js_test

_PARAMS = re.compile(r"\[.*\]$")
_FAILS = (
    "satisfied, but it fails on the base ({outcome}): it does not prove the layer's criteria hold already"
)
_MISSING = "satisfied, but it does not exist on the base: no run there reported it"
_NO_PYTEST = (
    "satisfied, but the gate runs no pytest, so airlie cannot run it on the base: name an earlier layer, or "
    "write the layer's package"
)


def _unproven(test: str, why: str) -> Finding:
    return Finding("satisfied_unproven", test, why, "")


def _reported(test: str, outcomes: dict[str, str]) -> str | None:
    """The outcome pytest gave `test` (a run from a rootdir below the repository's root names it shorter)."""
    key = f"{test.partition('::')[0]}::{_PARAMS.sub('', test.partition('::')[2])}"
    return next((o for k, o in outcomes.items() if key == k or key.endswith("/" + k)), None)


async def unproven(
    satisfied: Satisfied,
    *,
    built: Collection[str],
    repo: Path,
    base: str,
    scratch: Path,
    checks: PytestChecks | None,
    node_cache: Path,
    timeout_s: float,
) -> list[Finding]:
    """The `satisfied_unproven` findings on `satisfied` for a layer whose base is `base`: a layer number not
    in `built` (the layers built below it), and each named test that does not exist or does not pass there.
    `checks` is the gate's pytest check (None: the gate runs no pytest, and a pytest test cannot be run)."""
    found = [
        _unproven(item, f"satisfied, but layer {item} was not built below this one, so it holds nothing")
        for item in satisfied.by
        if "::" not in item and item not in built
    ]
    tests = [t for t in satisfied.by if "::" in t]
    if not tests:
        return found
    py = [t for t in tests if not is_js_test(t.partition("::")[0])]
    js = [t for t in tests if is_js_test(t.partition("::")[0])]
    if py and checks is None:
        found += [_unproven(t, _NO_PYTEST) for t in py]
        py = []
    if not py and not js:
        return found
    remove_worktree(repo, scratch)  # one a killed run left behind
    add_worktree(repo, scratch, branch=None, start_point=base)
    try:
        if py:
            assert checks is not None
            outcomes = await checks.outcomes(scratch, py)
            for t in py:
                # a run of all of them stops at an id pytest cannot find: each is then run alone
                got = outcomes if outcomes is not None else await checks.outcomes(scratch, [t])
                outcome = _reported(t, got) if got is not None else None
                if outcome is None:
                    found.append(_unproven(t, _MISSING))
                elif outcome != "PASSED":
                    found.append(_unproven(t, _FAILS.format(outcome=outcome.lower())))
        if js:
            changed = [ChangedTest(t.partition("::")[0], t.partition("::")[2], "", True) for t in js]
            ran = await vitest_red(scratch, changed, node_cache, timeout_s=timeout_s)
            passed = {f.test for f in ran if f.kind == "passes_before_code"}
            stopped = next((f.why for f in ran if f.kind == "suite_did_not_run"), None)
            for t in js:
                if t not in passed:
                    why = (
                        f"satisfied, but vitest did not run it on the base: {stopped}"
                        if stopped
                        else _FAILS.format(outcome="not passed")
                    )
                    found.append(_unproven(t, why))
    finally:
        remove_worktree(repo, scratch)
    return found


def satisfied_feedback(found: Sequence[Finding], *, tools: bool = False) -> tuple[str, tuple[str, ...]]:
    """What the architect is told when its satisfied reply does not hold, and the hub ids in it, in order (the
    pipeline emits a `steer` event for each): `checks-found`, a line per finding, then
    `satisfied-unproven` (`satisfied-unproven-tools` when the package is written with the file tools)."""
    said = Said()
    lines = [said("checks-found"), *(f"- {f.test}: {f.why}" for f in found)]
    lines.append(said("satisfied-unproven-tools" if tools else "satisfied-unproven"))
    return "\n".join(lines), tuple(said.ids)
