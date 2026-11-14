"""`airlie bench trace-diff` over two bench state dirs on disk, with a real supervisor ledger."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from airlie import cli
from airlie.bench.trace_diff import diff_story_runs
from airlie.supervisor.ledger import Intervention, Ledger, Outcome


def state_dir(root: Path, name: str, *, outcome: Outcome = "helped", extra_node: bool = False) -> Path:
    """What a bench run leaves: stories/<key>/events.jsonl, runs/<run>/events.jsonl and its prompt, and the
    ledger with one row for the story."""
    state = root / name
    story = state / "stories" / "air-1"
    story.mkdir(parents=True)
    events = [{"kind": "story_start", "ts": 1.0}, {"kind": "stage", "name": "build", "ts": 2.0}]
    (story / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    run = state / "runs" / "20261112-010101-abcd"
    (run / "trace" / "build").mkdir(parents=True)
    studio = [{"kind": "step", "role": "architect", "ts": 3.0, "path": str(state / "wt")}]
    if extra_node:
        studio.append({"kind": "node_started", "node": "place"})
    (run / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in studio))
    (run / "trace" / "build" / "prompt.json").write_text(
        json.dumps(
            {
                "role": "architect",
                "attempt": 0,
                "messages": [{"role": "user", "content": f"build in {state}/wt"}],
            }
        )
    )
    ledger = Ledger(state / "supervisor.sqlite")
    row = Intervention("air-1", "01", "r", "s", "architect", "red_streak", (), None, "no answer", "m", 0.0, 0)
    ledger.outcome(ledger.record(row), outcome)
    ledger.close()
    return state


def test_two_runs_that_did_the_same_thing_compare_clean_whatever_their_paths_and_node_events(
    tmp_path: Path,
) -> None:
    a = state_dir(tmp_path, "a")
    b = state_dir(tmp_path, "b", extra_node=True)
    assert diff_story_runs(a, b).clean


def test_one_changed_ledger_outcome_is_one_ledger_line(tmp_path: Path) -> None:
    a = state_dir(tmp_path, "a")
    b = state_dir(tmp_path, "b", outcome="not_helped")
    diff = diff_story_runs(a, b)
    assert diff.events == [] and diff.prompts == []
    assert len(diff.ledger) == 1 and "not_helped" in diff.ledger[0]


def test_the_command_prints_the_lines_and_exits_one_when_not_clean(tmp_path: Path) -> None:
    a = state_dir(tmp_path, "a")
    b = state_dir(tmp_path, "b", outcome="not_helped")
    runner = CliRunner()
    assert runner.invoke(cli.app, ["bench", "trace-diff", str(a), str(a)]).exit_code == 0
    diff = runner.invoke(cli.app, ["bench", "trace-diff", str(a), str(b)])
    assert diff.exit_code == 1 and "ledger row 0" in diff.output


def split_run(state: Path) -> None:
    """The one studio run's events moved into a second run: the same events and prompts in all."""
    first = state / "runs" / "20261112-010101-abcd"
    second = state / "runs" / "20261112-010102-beef"
    second.mkdir()
    (second / "events.jsonl").write_text((first / "events.jsonl").read_text())
    (first / "events.jsonl").write_text("")


def test_a_run_split_in_two_is_not_clean(tmp_path: Path) -> None:
    a = state_dir(tmp_path, "a")
    b = state_dir(tmp_path, "b")
    split_run(b)
    lines = diff_story_runs(a, b).events
    assert "runs: A has 1 studio runs, B has 2" in lines
    assert "run 0: A has 1 events, B has 0" in lines


def two_runs(root: Path, name: str, ids: tuple[str, str], *, repo: Path | None = None) -> Path:
    """A story whose state.json names two studio runs in the order `ids` gives (a refine, then a layers run)
    and the repository it was built in (its own per side, `repo`, unless given): each run's start names it."""
    state = root / name
    story = state / "stories" / "air-1"
    story.mkdir(parents=True)
    repo = repo or root / f"{name}-repo"
    (story / "state.json").write_text(json.dumps({"runs": list(ids), "repo": str(repo)}))
    (story / "events.jsonl").write_text(json.dumps({"kind": "story_start", "repo": str(repo)}) + "\n")
    for run_id, goal in zip(ids, ("refine", "layers"), strict=True):
        (state / "runs" / run_id).mkdir(parents=True)
        event = {"kind": "run_start", "goal": goal, "repo": str(repo), "path": f"{repo}/calc.py"}
        (state / "runs" / run_id / "events.jsonl").write_text(json.dumps(event) + "\n")
    return state


def test_runs_pair_in_the_order_the_story_named_them_and_the_repository_is_spelled_the_same(
    tmp_path: Path,
) -> None:
    """Two runs started in the same second sort by their random suffix: the story's state.json says which
    came first. Each side's repository (from its state.json) is `<repo>`."""
    a = two_runs(tmp_path, "a", ("20261112-010101-aaaa", "20261112-010101-ffff"))
    b = two_runs(tmp_path, "b", ("20261112-010101-ffff", "20261112-010101-aaaa"))
    assert diff_story_runs(a, b).clean


def test_a_repository_named_on_the_command_line_is_spelled_the_same(tmp_path: Path) -> None:
    ids = ("20261112-010101-aaaa", "20261112-010102-bbbb")
    a = two_runs(tmp_path, "a", ids, repo=tmp_path / "one")
    b = two_runs(tmp_path, "b", ids, repo=tmp_path / "two")
    for side in (a, b):  # a state.json that names no repository (an old run)
        f = side / "stories" / "air-1" / "state.json"
        f.write_text(json.dumps({"runs": list(ids)}))
    assert not diff_story_runs(a, b).clean
    assert diff_story_runs(a, b, repo_a=tmp_path / "one", repo_b=tmp_path / "two").clean
    args = ["bench", "trace-diff", str(a), str(b), "--repo-a", str(tmp_path / "one")]
    assert CliRunner().invoke(cli.app, [*args, "--repo-b", str(tmp_path / "two")]).exit_code == 0


def test_a_ledger_row_for_another_subtask_role_kinds_owner_or_layer_is_not_clean(tmp_path: Path) -> None:
    a = state_dir(tmp_path, "a")
    b = state_dir(tmp_path, "b")
    ledger = Ledger(b / "supervisor.sqlite")
    other = Intervention(
        "air-1", "02", "r", "fix", "architect", "red_streak", ("x",), None, "no answer", "m", 0.0, 0
    )
    ledger.outcome(ledger.record(other), "helped")
    ledger.close()
    ledger = Ledger(a / "supervisor.sqlite")
    same = Intervention(
        "air-1", "01", "r", "s", "architect", "red_streak", (), None, "no answer", "m", 0.0, 0
    )
    ledger.outcome(ledger.record(same), "helped")
    ledger.close()
    [line] = diff_story_runs(a, b).ledger
    assert line.startswith("ledger row 1: ") and "'architect'" in line and "'02'" in line


def test_a_prompt_for_another_role_or_attempt_or_without_messages_is_not_clean(tmp_path: Path) -> None:
    a = state_dir(tmp_path, "a")
    b = state_dir(tmp_path, "b")
    f = b / "runs" / "20261112-010101-abcd" / "trace" / "build" / "prompt.json"
    saved = json.loads(f.read_text())
    f.write_text(json.dumps({**saved, "role": "architect", "attempt": 2}))
    assert any("role=" in line for line in diff_story_runs(a, b).prompts)
    f.write_text(json.dumps({"role": "architect", "attempt": 0}))
    assert any("no messages key" in line for line in diff_story_runs(a, b).prompts)
