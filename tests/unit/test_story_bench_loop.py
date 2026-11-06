"""The story bench's reliability loop (2026-11-06): cases that publish nothing, build on a pinned base in a
clone of their own and have no oracle; the first-try measures of a record; the sweep that runs cases in
turn; and `airlie bench story|sweep` over a stubbed pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from airlie import cli
from airlie.bench.story_bench import (
    TRY_COUNTS,
    StoryCase,
    SweepRow,
    clone_problems,
    compare,
    find_case,
    grade,
    local_branches,
    previous_record,
    remove_run_worktrees,
    sweep_lines,
    tries,
    verdict,
)
from airlie.story import factory
from airlie.story.state import StoryState
from tests.unit.git_helpers import git
from tests.unit.test_story_bench import CASES, write_events
from tests.unit.test_story_pipeline import STORY_ROLES
from tests.unit.test_studio_orchestrator import cfg_for

BASE = "c0399cdf5c2ee2d1d8eddc08812ea5e0a4a4ebb0"
LOOP = ("air55", "air59", "air61", "air62")
runner = CliRunner()


def repo(path: Path, origin: str | None = "git@github.com:o/r.git") -> Path:
    """A repository with one commit and, unless None, an origin that names `origin` (never fetched)."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
    if origin is not None:
        git(path, "remote", "add", "origin", origin)
    return path


def case_for(clone: Path, **fields: Any) -> StoryCase:
    values: dict[str, Any] = {
        "id": "c", "request": "Delete comments\n\nAirlie tickets need a way to delete comments",
        "repo": "o/r", "base": "HEAD", "oracle": None, "mutate": None, "clone": clone, **fields,
    }  # fmt: skip
    return StoryCase(**values)


@pytest.mark.parametrize("case_id", LOOP)
def test_the_loop_cases_rerun_a_card_on_a_pinned_main_in_their_own_clone_publishing_nothing(
    case_id: str,
) -> None:
    case = find_case(CASES, case_id)
    assert (case.repo, case.base, case.pin_base, case.publish) == ("weyenk/airlie", BASE, True, False)
    assert case.oracle is None and case.mutate is None and case.gate_timeout_s == 1800
    assert case.budgets == {"wall_min": 240, "first_try": True}
    assert case.clone == Path("~/.local/state/airlie-bench/repos/airlie")
    home = Path.home() / "git" / "airlie"
    assert case.checkout(Path("~/git")) == Path.home() / ".local/state/airlie-bench/repos/airlie" != home
    title, blank, *_ = case.request.splitlines()  # the card's title, then its description as written
    assert title and blank == ""


def test_a_cards_description_is_kept_as_written_and_the_title_leads() -> None:
    case = find_case(CASES, "air55")
    assert case.request.startswith("Update the running airlie to the newest release tag when idle\n\n")
    assert "\n## Idle: all of these hold, or there is no check\n" in case.request
    assert (
        find_case(CASES, "air61").request == "Delete comments\n\nAirlie tickets need a way to delete comments"
    )
    roman = find_case(CASES, "roman")  # a folded request is still one line
    assert "\n" not in roman.request and roman.publish and not roman.pin_base and roman.clone is None


def test_the_wiki_case_is_built_in_a_clone_of_its_own_not_the_home_checkout() -> None:
    assert find_case(CASES, "wiki").clone == Path("~/.local/state/airlie-bench/repos/airlie")


def story_events(*middle: dict[str, Any], end: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    done = end if end is not None else {"kind": "story_done", "stage": "review", "blocked": None}
    return [{"ts": 0, "kind": "story_start"}, *middle, {"ts": 12, **done}]


def test_a_story_that_went_through_first_try_says_so(tmp_path: Path) -> None:
    story, runs = tmp_path / "s", tmp_path / "runs"
    write_events(
        story / "events.jsonl",
        story_events(
            {"ts": 1, "kind": "package_start", "layer": "01", "attempt": 1},
            {"ts": 2, "kind": "package_ready", "layer": "01", "attempt": 1},
            {"ts": 3, "kind": "layer_start", "layer": "01", "studio_run": "b1"},
            {"ts": 4, "kind": "layer_built", "layer": "01"},
            {"ts": 5, "kind": "package_start", "layer": "02", "attempt": 1},
            {"ts": 6, "kind": "layer_satisfied", "layer": "02"},  # nothing to build is no second try
        ),
    )
    got = tries(story, runs)
    assert got["first_try"] is True and got["why_not"] == [] and got["reached_end"] is True
    assert (got["package_tries"], got["layer_builds"], got["blocks"]) == (2, 1, 0)
    assert got["layers"]["02"]["satisfied"] and got["layers"]["01"]["built"]


def test_returned_packages_rebuilds_gate_failures_blocks_and_steering_keep_a_story_from_first_try(
    tmp_path: Path,
) -> None:
    """AIR-55 on 2026-11-05: its layer 01 draft came back unusable, its first build was re-planned (gate red),
    and it blocked; the architect's turns were cut and told to answer."""
    story, runs = tmp_path / "s", tmp_path / "runs"
    write_events(
        story / "events.jsonl",
        story_events(
            {"ts": 1, "kind": "run", "stage": "package", "studio_run": "p1"},
            # the drafting conversation's try has no package_start of its own
            {"ts": 2, "kind": "package_returned", "layer": "01", "attempt": 1, "kinds": ["package_unusable"]},
            {"ts": 3, "kind": "package_start", "layer": "01", "attempt": 2},
            {"ts": 4, "kind": "package_ready", "layer": "01", "attempt": 2},
            {"ts": 5, "kind": "layer_start", "layer": "01", "studio_run": "b1"},
            {"ts": 6, "kind": "layer_replanned", "layer": "01", "why": "gate_red"},
            {"ts": 7, "kind": "package_start", "layer": "01", "attempt": 1},
            {"ts": 7, "kind": "layer_start", "layer": "01", "studio_run": "b2"},
            {"ts": 8, "kind": "blocked", "stage": "layers", "reason": "gate_on_base"},
            end={"kind": "story_done", "stage": "layers", "blocked": "gate_on_base"},
        ),
    )
    write_events(
        runs / "p1" / "events.jsonl",
        [{"kind": "answer_after_cuts"}, {"kind": "answer_after_cuts"}, {"kind": "last_turn_tool_call"}],
    )
    write_events(runs / "b1" / "events.jsonl", [{"kind": "gate_failed"}, {"kind": "step"}])
    write_events(runs / "b2" / "events.jsonl", [{"kind": "gate_failed"}])
    got = tries(story, runs)
    assert got["first_try"] is False and got["reached_end"] is False
    counts = {k: got[k] for k in TRY_COUNTS}
    assert counts == {
        "package_tries": 3,
        "packages_returned": 1,
        "layer_builds": 2,
        "replans": 1,
        "gate_failures": 2,
        "blocks": 1,
        "answer_after_cuts": 2,
        "last_turn_tool_calls": 1,
    }
    assert got["returned_kinds"] == {"package_unusable": 1}
    assert got["layers"]["01"]["returned"] == [["package_unusable"]]
    assert got["why_not"] == [
        "layer 01: package returned (package_unusable)",
        "layer 01: 2 builds (re-planned: gate_red)",
        "layer 01: 2 gate failure(s) in its builds",
        "blocked 1 time(s)",
        "the story did not reach review",
    ]
    assert tries(tmp_path / "none", runs)["first_try"] is False  # a run with no events never reached the end


def test_a_case_with_no_oracle_is_graded_by_its_gate_and_its_layers_tests_only(tmp_path: Path) -> None:
    clone = repo(tmp_path / "clone")
    sha = git(clone, "rev-parse", "HEAD")
    tests = {"02": ["tests/b.py::test_b"], "01": ["tests/a.py::test_a"]}
    got = grade(case_for(clone), clone, sha, ["sh", "-c", "true"], tests)
    assert got == {
        "gate_passes": True,
        "layer_tests": {"01": ["tests/a.py::test_a"], "02": ["tests/b.py::test_b"]},
    }
    assert grade(case_for(clone), clone, sha, ["sh", "-c", "false"])["gate_passes"] is False


def loop_record(first_try: bool, wall: float = 100.0, **counts: int) -> dict[str, Any]:
    return {
        "blocked": None,
        "first_try": first_try,
        "tries": {"why_not": [] if first_try else ["layer 01: package returned (lint)"], **counts},
        "cost": {"wall_min": wall, "phases": [], "roles": [], "retries": {}},
        "quality": {"gate_passes": True, "layer_tests": {}},
    }


def test_a_loop_record_is_held_to_first_try_and_compares_its_counts_side_by_side() -> None:
    case = find_case(CASES, "air59")
    assert verdict(case, loop_record(True)) == []
    assert verdict(case, loop_record(False)) == ["not first try: layer 01: package returned (lint)"]
    failing = loop_record(True) | {"quality": {"gate_passes": False, "layer_tests": {}}}
    assert verdict(case, failing) == ["the top branch fails its gate"]
    lines = compare(
        loop_record(False, 200.0, package_tries=3, blocks=1), loop_record(True, 150.0, package_tries=2)
    )
    assert lines[:3] == ["wall min: 200 -> 150 (-25%)", "first_try: no -> yes", "package_tries: 3 -> 2"]
    assert "layer_builds: 0 -> 0" in lines and "blocks: 1 -> 0" in lines  # unchanged ones too: side by side
    old = {k: v for k, v in loop_record(True).items() if k not in ("first_try", "tries")}
    assert not any(x.startswith("first_try") for x in compare(old, loop_record(True)))


def test_a_runs_kept_worktrees_are_removed_so_its_branches_can_be_deleted(tmp_path: Path) -> None:
    clone, state = repo(tmp_path / "clone"), tmp_path / "state"
    git(clone, "worktree", "add", "-q", "-b", "AIR-1-x-01-a", str(state / "runs" / "r1" / "studio"))
    git(clone, "worktree", "add", "-q", "-b", "theirs", str(tmp_path / "elsewhere"))
    assert remove_run_worktrees(clone, state) == [str((state / "runs" / "r1" / "studio").resolve())]
    listed = git(clone, "worktree", "list", "--porcelain")
    assert "elsewhere" in listed and "r1" not in listed
    git(clone, "branch", "-D", "AIR-1-x-01-a")  # no worktree holds it now
    assert local_branches(clone) == {"main", "theirs"}


def test_a_clone_is_refused_when_it_is_the_running_checkout_or_another_repository(tmp_path: Path) -> None:
    clone = repo(tmp_path / "clone")
    git(clone, "worktree", "add", "-q", "--detach", str(tmp_path / "wt"))
    case = case_for(clone)
    assert clone_problems(case, clone, [tmp_path / "elsewhere"]) == []
    [shared] = clone_problems(case, clone, [tmp_path / "wt"])  # a worktree of it is the same repository
    assert (
        "is the airlie checkout this bench runs from" in shared and "clone: in evals/stories/c.yaml" in shared
    )
    [other] = clone_problems(case_for(clone, repo="weyenk/airlie"), clone, [])
    assert other.endswith("names o/r, not weyenk/airlie")


def test_a_missing_clone_is_made_from_its_source(tmp_path: Path) -> None:
    source = repo(tmp_path / "source", origin=None)
    clone = tmp_path / "bench" / "repos" / "r"
    [named] = clone_problems(case_for(clone), clone, [], source=str(source))
    assert (clone / ".git").is_dir() and "names no github.com repository" in named  # cloned: origin is source
    git(clone, "remote", "set-url", "origin", "https://github.com/o/r.git")
    assert clone_problems(case_for(clone), clone, [], source=str(source)) == []
    [failed] = clone_problems(case_for(tmp_path / "x"), tmp_path / "x", [], source=str(tmp_path / "nowhere"))
    assert failed.startswith(f"could not clone {tmp_path / 'nowhere'}")


def test_the_previous_record_is_the_latest_timestamped_one_and_never_a_baseline(tmp_path: Path) -> None:
    for name in ("story-air59-20261106-090000.json", "story-air59-20261107-010000.json",
                 "story-air59-baseline.json", "story-air591-20261108-000000.json"):  # fmt: skip
        (tmp_path / name).write_text("{}")
    found = previous_record(tmp_path, "air59")
    assert found is not None and found.name == "story-air59-20261107-010000.json"
    assert previous_record(tmp_path, "air62") is None


def test_a_sweep_summary_has_a_row_per_case_then_each_comparison() -> None:
    rows = [
        SweepRow("air59", loop_record(True, 95.5, package_tries=2, layer_builds=2),
                 previous=loop_record(False, 120.0), previous_name="story-air59-1.json"),
        SweepRow("air61", None, error="no such clone"),
    ]  # fmt: skip
    lines = sweep_lines(rows)
    assert lines[0].split() == [
        "case",
        "first_try",
        "wall_min",
        "tries",
        "builds",
        "blocks",
        "gate_fails",
        "before",
    ]
    assert lines[1].split() == ["air59", "yes", "95.5", "2", "2", "-", "-", "no"]
    assert lines[2].split() == ["air61", "error", "-", "-", "-", "-", "-", "-"]
    assert "air59 vs story-air59-1.json:" in lines and "  first_try: no -> yes" in lines
    assert "air61: no such clone" in lines


# ---- the CLI, over a stubbed pipeline ----


def bench_setup(tmp_path: Path, *cases: dict[str, Any]) -> tuple[Path, Path]:
    """A config, and a cases directory holding `cases` (each case's yaml fields)."""
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {}})
    assert cfg.source_path is not None
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    for c in cases:
        (cases_dir / f"{c['id']}.yaml").write_text(yaml.safe_dump(c))
    return cfg.source_path, cases_dir


def fake_run(tmp_path: Path, seen: list[dict[str, Any]]) -> Any:
    """A run_one that records what it was asked, makes a layer branch and a kept worktree in the clone as a
    stopped layer would, writes a first-try story's events and returns it at review."""

    async def run_one(cfg, *, board, card, repo, test_cmd, pinned_base):  # type: ignore[no-untyped-def]
        state = cfg.observability.state_dir
        seen.append(
            {"publish": cfg.story.publish, "pinned_base": pinned_base, "repo": repo, "title": card.title}
        )
        git(repo, "worktree", "add", "-q", "-b", f"{card.key}-01-a", str(state / "runs" / "r1" / "studio"))
        path = state / "stories" / card.key
        write_events(
            path / "events.jsonl",
            story_events(
                {"ts": 1, "kind": "package_start", "layer": "01", "attempt": 1},
                {"ts": 2, "kind": "layer_start", "layer": "01", "studio_run": "r1"},
                {"ts": 3, "kind": "layer_built", "layer": "01"},
            ),
        )
        return StoryState(
            path=path, card=card.to_dict(), repo=str(repo), stage="review", base={"sha": pinned_base}
        )

    return run_one


def test_bench_story_no_publish_builds_on_the_pin_asks_github_nothing_and_clears_the_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = repo(tmp_path / "clone")
    sha = git(clone, "rev-parse", "HEAD")
    case = {"id": "c", "request": "Delete comments\n\nAirlie tickets need a way to delete comments",
            "repo": "o/r", "base": sha, "pin_base": True, "clone": str(clone)}  # fmt: skip
    config, cases_dir = bench_setup(tmp_path, case)
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(factory, "run_one", fake_run(tmp_path, seen))

    def no_gh(*args: Any, **kwargs: Any) -> list[str]:
        raise AssertionError("a no-publish run closed PRs on GitHub")

    monkeypatch.setattr("airlie.bench.story_bench.close_prs", no_gh)
    out = tmp_path / "records"
    args = ["bench", "story", "c", "--no-publish", "--cases-dir", str(cases_dir), "--out-dir", str(out),
            "--state-root", str(tmp_path / "bench-state"), "--config", str(config)]  # fmt: skip
    r = runner.invoke(cli.app, args)
    assert r.exit_code == 0, r.output
    assert seen == [
        {"publish": "none", "pinned_base": sha, "repo": clone.resolve(), "title": "Delete comments"}
    ]
    [path] = out.glob("story-c-*.json")
    record = json.loads(path.read_text())
    assert record["publish"] is False and record["prs"] == [] and record["closed"] == []
    assert record["base"] == {"expected": sha, "built_on": sha, "same": True, "pinned": True}
    assert record["first_try"] is True and record["tries"]["layer_builds"] == 1
    assert len(record["worktrees_removed"]) == 1 and record["branches_deleted"][0].endswith("-01-a")
    assert local_branches(clone) == {"main"} and "verdict: within its budgets" in r.output


def test_bench_sweep_runs_each_case_in_turn_and_a_case_that_cannot_start_is_a_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = repo(tmp_path / "clone")
    sha = git(clone, "rev-parse", "HEAD")
    good = {"id": "good", "request": "Delete comments", "repo": "o/r", "base": sha, "pin_base": True,
            "publish": False, "clone": str(clone)}  # fmt: skip
    elsewhere = {**good, "id": "elsewhere", "repo": "o/other"}
    config, cases_dir = bench_setup(tmp_path, good, elsewhere)
    out = tmp_path / "records"
    out.mkdir()
    (out / "story-good-20261101-000000.json").write_text(json.dumps(loop_record(False, 300.0)))
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(factory, "run_one", fake_run(tmp_path, seen))
    args = ["bench", "sweep", "good", "elsewhere", "--cases-dir", str(cases_dir), "--out-dir", str(out),
            "--state-root", str(tmp_path / "bench-state"), "--config", str(config)]  # fmt: skip
    r = runner.invoke(cli.app, args)
    assert r.exit_code == 1, r.output  # one case did not run
    assert [s["publish"] for s in seen] == ["none"]
    rows = [line.split() for line in r.output.splitlines()]
    assert ["good", "yes", "0.2", "1", "1", "0", "0", "no"] in rows
    assert ["elsewhere", "error", "-", "-", "-", "-", "-", "-"] in rows
    assert "good vs story-good-20261101-000000.json:" in r.output and "  first_try: no -> yes" in r.output
    assert "names o/r, not o/other" in r.output
    assert len(list(out.glob("story-good-*.json"))) == 2


def test_a_stopped_sweep_stops_at_the_case_it_was_in_and_clears_its_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop (SIGTERM: typer.Exit, which is a RuntimeError) is not one case failing: the sweep ends there."""
    import typer

    clone = repo(tmp_path / "clone")
    sha = git(clone, "rev-parse", "HEAD")
    first = {"id": "first", "request": "x", "repo": "o/r", "base": sha, "pin_base": True, "publish": False,
             "clone": str(clone)}  # fmt: skip
    config, cases_dir = bench_setup(tmp_path, first, {**first, "id": "second"})
    asked: list[str] = []

    async def stopped(cfg, *, board, card, repo, test_cmd, pinned_base):  # type: ignore[no-untyped-def]
        asked.append(card.key)
        kept = cfg.observability.state_dir / "runs" / "r"
        git(repo, "worktree", "add", "-q", "-b", "left-01", str(kept))
        raise typer.Exit(143)

    monkeypatch.setattr(factory, "run_one", stopped)
    where = ["--out-dir", str(tmp_path / "records"), "--state-root", str(tmp_path / "s")]
    args = [
        "bench",
        "sweep",
        "first",
        "second",
        "--cases-dir",
        str(cases_dir),
        *where,
        "--config",
        str(config),
    ]
    r = runner.invoke(cli.app, args)
    assert r.exit_code == 143 and len(asked) == 1
    assert local_branches(clone) == {"main"}  # the stopped run's worktree and branch are gone
