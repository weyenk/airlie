"""scripts/mutate_local.py: the weekly whole-project run waits for the line and uses a fresh worktree."""

from __future__ import annotations

import importlib.util
import plistlib
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from airlie.story.state import lane_lock

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "mutate_local.py"
PLIST = REPO / "docs" / "integrations" / "mutation-weekly.plist"
spec = importlib.util.spec_from_file_location("mutate_local", SCRIPT)
assert spec and spec.loader
ml = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ml)


def clock(hour: int) -> Callable[[], datetime]:
    return lambda: datetime(2026, 11, 15, hour, 0)


def no_sleep(_: float) -> None:
    raise AssertionError("a free line is not waited for")


def test_a_free_line_runs_at_once(tmp_path: Path) -> None:
    assert ml.wait_for_lanes(tmp_path, now=clock(1), sleep=no_sleep) is True


def test_lanes_busy_names_the_lane_another_holder_has(tmp_path: Path) -> None:
    assert ml.lanes_busy(tmp_path) == []
    with lane_lock(tmp_path, "build") as ok:
        assert ok
        assert ml.lanes_busy(tmp_path) == ["build"]


def test_a_busy_lane_is_waited_for_until_it_frees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    polls = iter([["build"], ["build"], []])
    monkeypatch.setattr(ml, "lanes_busy", lambda _stories: next(polls))
    slept: list[float] = []
    assert ml.wait_for_lanes(tmp_path, now=clock(1), sleep=slept.append) is True
    assert slept == [ml.POLL_S, ml.POLL_S]


def test_a_lane_still_busy_at_six_skips_the_week(tmp_path: Path) -> None:
    now = datetime(2026, 11, 15, 5, 0)

    def tick() -> datetime:
        return now

    def sleep(s: float) -> None:
        nonlocal now
        now += timedelta(seconds=s)

    with lane_lock(tmp_path, "plan") as ok:
        assert ok
        assert ml.wait_for_lanes(tmp_path, now=tick, sleep=sleep) is False
    assert now.hour >= ml.WAIT_UNTIL_H


def test_the_run_uses_a_fresh_worktree_of_origin_main_and_always_removes_it(tmp_path: Path) -> None:
    home, out = tmp_path / "home", tmp_path / "out"
    calls: list[tuple[list[str], Path]] = []

    def fake(args: list[str], cwd: Path) -> int:
        calls.append((args, cwd))
        return 1 if "scripts/mutate_changed.py" in args else 0

    assert ml.run_project(home, out, run=fake, max_children=4, day="2026-11-15") == 1
    tree = out / "worktree"
    cmds = [a for a, _ in calls]
    add = ["git", "-C", str(home), "worktree", "add", "--detach", str(tree), "origin/main"]
    assert add in cmds
    mut = next(c for c in calls if "scripts/mutate_changed.py" in c[0])
    assert mut[0][-6:] == [
        "--scope",
        "project",
        "--max-children",
        "4",
        "--report",
        str(out / "2026-11-15.md"),
    ]
    assert mut[1] == tree
    assert cmds.index(add) < cmds.index(mut[0])
    assert ["uv", "sync", "--frozen"] in cmds
    assert cmds[-1] == ["git", "-C", str(home), "worktree", "remove", "--force", str(tree)]


def test_a_worktree_left_by_an_interrupted_run_is_removed_first(tmp_path: Path) -> None:
    home, out = tmp_path / "home", tmp_path / "out"
    (out / "worktree").mkdir(parents=True)
    cmds: list[list[str]] = []

    def fake(args: list[str], cwd: Path) -> int:
        cmds.append(args)
        return 0

    ml.run_project(home, out, run=fake, max_children=1, day="d")
    rm = ["git", "-C", str(home), "worktree", "remove", "--force", str(out / "worktree")]
    prune = ["git", "-C", str(home), "worktree", "prune"]
    add = next(c for c in cmds if "add" in c)
    assert cmds.index(rm) < cmds.index(prune) < cmds.index(add)


def test_the_worktree_is_removed_even_when_the_run_is_stopped(tmp_path: Path) -> None:
    home, out = tmp_path / "home", tmp_path / "out"
    cmds: list[list[str]] = []

    def fake(args: list[str], cwd: Path) -> int:
        cmds.append(args)
        if "scripts/mutate_changed.py" in args:
            raise SystemExit(143)
        return 0

    with pytest.raises(SystemExit):
        ml.run_project(home, out, run=fake, max_children=1, day="d")
    assert cmds[-1][-3:-1] == ["remove", "--force"]


def test_reports_beyond_the_newest_twelve_are_pruned(tmp_path: Path) -> None:
    for i in range(15):
        (tmp_path / f"2026-11-{i + 1:02d}.md").write_text("x")
    (tmp_path / "keep.txt").write_text("x")
    ml.prune_reports(tmp_path)
    assert sorted(p.name for p in tmp_path.glob("*.md")) == [f"2026-11-{i:02d}.md" for i in range(4, 16)]
    assert (tmp_path / "keep.txt").exists()


def test_the_plist_runs_weekly_on_sunday_at_one_through_caffeinate() -> None:
    plist = plistlib.loads(PLIST.read_bytes())
    assert plist["Label"] == "dev.airlie.mutation-weekly"
    assert plist["StartCalendarInterval"] == {"Weekday": 0, "Hour": 1, "Minute": 0}
    assert "exec caffeinate -i uv run" in plist["ProgramArguments"][-1]
