"""scripts/mutate_local.py: the weekly whole-project run waits for the line and uses a fresh worktree."""

from __future__ import annotations

import importlib.util
import plistlib
import signal
import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

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

    assert ml.run_project(home, out, run=fake, max_children=4, day="2026-11-15") == ml.ProjectRun(code=1)
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


def test_a_failed_worktree_add_stops_before_mutmut_and_says_so(tmp_path: Path) -> None:
    home, out = tmp_path / "home", tmp_path / "out"
    cmds: list[list[str]] = []

    def fake(args: list[str], cwd: Path) -> int:
        cmds.append(args)
        return 128 if "add" in args else 0

    assert ml.run_project(home, out, run=fake, max_children=1, day="d") == ml.ProjectRun(
        code=None, failed="worktree add"
    )
    assert not any("scripts/mutate_changed.py" in c for c in cmds)
    assert ["uv", "sync", "--frozen"] not in cmds


def test_a_failed_uv_sync_stops_before_mutmut_and_removes_the_worktree(tmp_path: Path) -> None:
    home, out = tmp_path / "home", tmp_path / "out"
    cmds: list[list[str]] = []

    def fake(args: list[str], cwd: Path) -> int:
        cmds.append(args)
        return 2 if args[:2] == ["uv", "sync"] else 0

    assert ml.run_project(home, out, run=fake, max_children=1, day="d").failed == "uv sync"
    assert not any("scripts/mutate_changed.py" in c for c in cmds)
    assert cmds[-1] == ["git", "-C", str(home), "worktree", "remove", "--force", str(out / "worktree")]


def test_a_leftover_dir_git_cannot_remove_is_deleted_before_the_add(tmp_path: Path) -> None:
    home, out = tmp_path / "home", tmp_path / "out"
    tree = out / "worktree"
    (tree / "mutants").mkdir(parents=True)  # not a worktree: `git worktree remove` refuses it
    (tree / "mutants" / "stale.py").write_text("x")
    seen: dict[str, bool] = {}

    def fake(args: list[str], cwd: Path) -> int:
        if "remove" in args and "add" not in seen:
            return 128
        if "prune" in args:
            seen["gone_at_prune"] = not tree.exists()
        if "add" in args:
            seen["add"] = True
        return 0

    assert ml.run_project(home, out, run=fake, max_children=1, day="d").code == 0
    assert seen == {"gone_at_prune": True, "add": True}


def test_a_failed_fetch_runs_on_a_stale_base_and_says_so(tmp_path: Path) -> None:
    home, out = tmp_path / "home", tmp_path / "out"

    def fake(args: list[str], cwd: Path) -> int:
        return 1 if "fetch" in args else 0

    assert ml.run_project(home, out, run=fake, max_children=1, day="d") == ml.ProjectRun(
        code=0, stale_base=True
    )


class FakeChild:
    """A step's process. launchd's SIGTERM lands while the script waits on it: `wait` calls the handler."""

    def __init__(self, pid: int, on_wait: Callable[[], None] | None = None) -> None:
        self.pid = pid
        self.on_wait = on_wait

    def wait(self) -> int:
        if self.on_wait is None:
            return 0
        self.on_wait()
        return -15


class FakeTimer:
    """threading.Timer's shape; nothing runs until a test calls `fire`."""

    made: list[FakeTimer]

    def __init__(self, delay: float, fn: Callable[[], None]) -> None:
        self.delay, self.fn, self.cancelled = delay, fn, False
        FakeTimer.made.append(self)

    def start(self) -> None:
        pass

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.fn()


def make_runner(popen: Any, killed: list[tuple[int, int]], ignored: list[bool] | None = None) -> Any:
    FakeTimer.made = []
    return ml.Runner(
        popen=popen,
        killpg=lambda pgid, sig: killed.append((pgid, sig)),
        timer=FakeTimer,
        ignore_term=lambda: (ignored if ignored is not None else []).append(True),
    )


def test_sigterm_stops_the_steps_process_group_then_the_worktree_is_removed(tmp_path: Path) -> None:
    home, out = tmp_path / "home", tmp_path / "out"
    killed: list[tuple[int, int]] = []
    ignored: list[bool] = []
    started: list[tuple[list[str], dict[str, Any]]] = []

    def popen(args: list[str], **kw: Any) -> FakeChild:
        started.append((args, kw))
        if "scripts/mutate_changed.py" in args:
            return FakeChild(42, on_wait=lambda: runner.stop(signal.SIGTERM, None))
        return FakeChild(len(started))

    runner = make_runner(popen, killed, ignored)
    with pytest.raises(SystemExit) as stopped:
        ml.run_project(home, out, run=runner, max_children=1, day="d")
    assert stopped.value.code == 143
    assert killed == [(42, signal.SIGTERM)]  # the group: uv, mutate_changed.py and mutmut's workers
    assert ignored == [True]  # a second SIGTERM cannot interrupt the removal
    assert all(kw.get("start_new_session") is True for _, kw in started)
    assert started[-1][0] == ["git", "-C", str(home), "worktree", "remove", "--force", str(out / "worktree")]
    (grace,) = FakeTimer.made
    assert grace.delay == ml.STOP_GRACE_S and grace.cancelled  # it ended inside its grace: no KILL


def test_a_step_that_outlives_its_grace_is_killed() -> None:
    killed: list[tuple[int, int]] = []
    runner = make_runner(None, killed)
    runner.child = FakeChild(7)
    runner.stop(signal.SIGTERM, None)
    assert killed == [(7, signal.SIGTERM)]
    FakeTimer.made[0].fire()  # the grace ran out with the group still up
    assert killed[-1] == (7, signal.SIGKILL)


def test_a_stop_between_steps_exits_at_once() -> None:
    runner = make_runner(None, [])
    with pytest.raises(SystemExit):
        runner.stop(signal.SIGTERM, None)


def test_a_group_already_gone_is_not_an_error() -> None:
    def gone(pgid: int, sig: int) -> None:
        raise ProcessLookupError

    runner = ml.Runner(popen=None, killpg=gone, timer=FakeTimer, ignore_term=lambda: None)
    runner.child = FakeChild(9)
    runner.stop(signal.SIGTERM, None)
    assert runner.stopped is runner.child


@pytest.fixture
def main_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    env: dict[str, Any] = {"events": [], "spawned": [], "handlers": []}
    cfg = type("Cfg", (), {"observability": type("O", (), {"state_dir": tmp_path / "state"})()})()
    monkeypatch.setattr(ml, "OUT", tmp_path / "out")
    monkeypatch.setattr(ml, "home_checkout", lambda: tmp_path / "home")
    monkeypatch.setattr(ml, "load_config", lambda _p: cfg)
    monkeypatch.setattr(ml, "stories_dir", lambda _c: tmp_path / "stories")
    monkeypatch.setattr(ml, "wait_for_lanes", lambda *_a, **_k: True)
    monkeypatch.setattr(ml, "line_event", lambda _d, kind, **f: env["events"].append((kind, f)))
    monkeypatch.setattr(ml.signal, "signal", lambda sig, h: env["handlers"].append((sig, h)))
    env["spawn"] = lambda args, **kw: env["spawned"].append((args, kw))
    return env


def test_main_keeps_the_mac_awake_for_as_long_as_it_runs(
    main_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ml, "run_project", lambda *a, **k: ml.ProjectRun(code=0))
    assert ml.main([], spawn=main_env["spawn"]) == 0
    ((args, kw),) = main_env["spawned"]
    assert args == ["caffeinate", "-i", "-w", str(ml.os.getpid())]
    assert kw["start_new_session"] is True and kw["stdout"] is subprocess.DEVNULL
    assert [sig for sig, _ in main_env["handlers"]] == [signal.SIGTERM]


def test_main_records_a_failed_step_as_its_own_event(
    main_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ml, "run_project", lambda *a, **k: ml.ProjectRun(None, "worktree add", stale_base=True)
    )
    assert ml.main([], spawn=main_env["spawn"]) == 0
    assert main_env["events"] == [("mutation_project_failed", {"step": "worktree add", "stale_base": True})]


def test_main_records_mutmuts_code_and_a_stale_base(
    main_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ml, "run_project", lambda *a, **k: ml.ProjectRun(code=1, stale_base=True))
    assert ml.main([], spawn=main_env["spawn"]) == 0
    ((kind, fields),) = main_env["events"]
    assert kind == "mutation_project" and fields["code"] == 1 and fields["stale_base"] is True


def test_the_plist_runs_weekly_on_sunday_at_one_with_uv_as_its_process() -> None:
    plist = plistlib.loads(PLIST.read_bytes())
    assert plist["Label"] == "dev.airlie.mutation-weekly"
    assert plist["StartCalendarInterval"] == {"Weekday": 0, "Hour": 1, "Minute": 0}
    command = plist["ProgramArguments"][-1]
    assert "exec uv run --quiet python scripts/mutate_local.py" in command
    assert "caffeinate" not in command  # the script starts it: launchd's SIGTERM must reach the script
    # launchd sends SIGKILL ExitTimeOut seconds after its SIGTERM: the grace and the removal must fit in it
    assert plist["ExitTimeOut"] >= ml.STOP_GRACE_S + 60
