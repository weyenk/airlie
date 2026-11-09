"""The whole-project mutation run, weekly on this Mac (launchd: docs/integrations/mutation-weekly.plist).

It waits for the line's two lanes to be free (polling every POLL_S, giving the week up at WAIT_UNTIL_H:00),
makes a fresh detached worktree of origin/main under ~/.local/state/airlie/mutation, runs
`scripts/mutate_changed.py --scope project` there and writes the backlog report to `<day>.md` beside
it, keeping the newest KEEP_REPORTS. `caffeinate -w <pid>` keeps the Mac awake while the script runs.

A leftover worktree (a SIGKILL's, a power cut's) is removed first, deleted outright when git no longer
knows it. A failed `git fetch` is not fatal (offline: origin/main is what the last fetch left, and the event
says `stale_base`); a failed `git worktree add` or `uv sync` stops the run before mutmut. Each step runs in
its own process group: on SIGTERM (launchd stopping the job) the running step's group gets SIGTERM, then
SIGKILL after STOP_GRACE_S, and the worktree is removed whatever happens, with further SIGTERMs ignored
until it is. The exit code is 0 whatever mutmut found, so launchd never respawns it; the outcome is a line
event: `mutation_project`, `mutation_project_failed` (with the step) or `mutation_project_skipped`.

    uv run python scripts/mutate_local.py [--max-children N]
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import Any, NamedTuple

from airlie.config import load_config
from airlie.story.factory import stories_dir
from airlie.story.fixes import home_checkout, line_event
from airlie.story.state import Lane, lock_held

ROOT = Path(__file__).resolve().parents[1]
OUT = Path("~/.local/state/airlie/mutation")
WAIT_UNTIL_H = 6  # local hour at which a busy line skips the week
POLL_S = 600
KEEP_REPORTS = 12
STOP_GRACE_S = 30  # SIGTERM to SIGKILL for a step's group; the plist's ExitTimeOut leaves room for it
LANES: tuple[Lane, ...] = ("plan", "build")


class ProjectRun(NamedTuple):
    code: int | None  # mutmut's exit code; None when a step before it failed
    failed: str | None = None  # that step: "worktree add" or "uv sync"
    stale_base: bool = False  # `git fetch` failed: origin/main is what the last fetch left


def lanes_busy(stories: Path) -> list[str]:
    """The lanes whose lock another holder has right now."""
    return [lane for lane in LANES if lock_held(stories, lane)]


def wait_for_lanes(stories: Path, *, now: Callable[[], datetime], sleep: Callable[[float], None]) -> bool:
    """True once both lanes are free; False when the local time reaches WAIT_UNTIL_H:00 first."""
    while lanes_busy(stories):
        if now().hour >= WAIT_UNTIL_H:
            return False
        sleep(POLL_S)
    return True


def run_project(
    home: Path, out: Path, *, run: Callable[[list[str], Path], int], max_children: int, day: str
) -> ProjectRun:
    """The whole project mutated in a fresh worktree of origin/main, which is always removed."""
    tree = out / "worktree"
    git = ["git", "-C", str(home), "worktree"]
    stale = run(["git", "-C", str(home), "fetch", "origin", "main"], home) != 0
    if tree.exists():  # an interrupted run's, which a SIGKILL or a power cut left
        _remove(tree, run=run, git=git, home=home)
    run([*git, "prune"], home)
    if run([*git, "add", "--detach", str(tree), "origin/main"], home) != 0:
        return ProjectRun(code=None, failed="worktree add", stale_base=stale)
    try:
        if run(["uv", "sync", "--frozen"], tree) != 0:
            return ProjectRun(code=None, failed="uv sync", stale_base=stale)
        mutate = ["uv", "run", "python", "scripts/mutate_changed.py", "--scope", "project"]
        code = run([*mutate, "--max-children", str(max_children), "--report", str(out / f"{day}.md")], tree)
        return ProjectRun(code=code, stale_base=stale)
    finally:
        _remove(tree, run=run, git=git, home=home)


def _remove(tree: Path, *, run: Callable[[list[str], Path], int], git: list[str], home: Path) -> None:
    """`git worktree remove`, or, for a directory git does not know as a worktree, delete it."""
    if run([*git, "remove", "--force", str(tree)], home) != 0 and tree.exists():
        shutil.rmtree(tree, ignore_errors=True)


def prune_reports(out: Path, keep: int = KEEP_REPORTS) -> None:
    """Remove all but the newest `keep` reports (the day-named `*.md` files sort by date)."""
    reports = sorted(out.glob("*.md"))
    for old in reports[: max(0, len(reports) - keep)]:
        old.unlink(missing_ok=True)


def _ignore_term() -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)


class Runner:
    """Runs each step in its own process group, so a stop reaches every process the step started (uv,
    mutate_changed.py and mutmut's workers), not only the step's own process."""

    def __init__(
        self,
        *,
        popen: Any = subprocess.Popen,
        killpg: Any = os.killpg,
        timer: Any = threading.Timer,
        ignore_term: Callable[[], None] = _ignore_term,
    ) -> None:
        self._popen, self._killpg, self._timer, self._ignore_term = popen, killpg, timer, ignore_term
        self.child: Any = None  # the step running now
        self.stopped: Any = None  # the step a SIGTERM stopped
        self._grace: Any = None

    def __call__(self, args: list[str], cwd: Path) -> int:
        child = self.child = self._popen(args, cwd=cwd, start_new_session=True)
        try:
            code: int = child.wait()
        finally:
            self.child = None
        if child is self.stopped:
            if self._grace is not None:
                self._grace.cancel()
            raise SystemExit(143)  # unwinds run_project's finally, so the worktree goes
        return code

    def stop(self, _signum: int, _frame: FrameType | None) -> None:
        """The SIGTERM handler: stop the running step's group, SIGKILL it after STOP_GRACE_S."""
        self._ignore_term()  # the worktree's removal must not be interrupted by a second one
        child = self.child
        if child is None:
            raise SystemExit(143)
        self.stopped = child
        self._signal(child.pid, signal.SIGTERM)
        self._grace = self._timer(STOP_GRACE_S, lambda: self._signal(child.pid, signal.SIGKILL))
        self._grace.daemon = True
        self._grace.start()

    def _signal(self, pgid: int, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            self._killpg(pgid, sig)


def keep_awake(spawn: Any = subprocess.Popen) -> None:
    """caffeinate for as long as this process lives (`-w`), in its own session so a stop leaves it alone."""
    spawn(
        ["caffeinate", "-i", "-w", str(os.getpid())],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main(argv: list[str] | None = None, *, spawn: Any = subprocess.Popen) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--max-children", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    args = ap.parse_args(argv)
    home = home_checkout()
    if home is None:
        print("mutate_local: airlie does not run from a git checkout here; nothing to mutate")
        return 2
    cfg = load_config(ROOT / "airlie.yaml")
    state_dir = cfg.observability.state_dir
    out = OUT.expanduser()
    out.mkdir(parents=True, exist_ok=True)
    runner = Runner()
    signal.signal(signal.SIGTERM, runner.stop)
    keep_awake(spawn)
    if not wait_for_lanes(stories_dir(cfg), now=datetime.now, sleep=time.sleep):
        busy = lanes_busy(stories_dir(cfg))
        print(f"mutate_local: lanes {busy} stayed busy until {WAIT_UNTIL_H}:00; skipping this week")
        line_event(state_dir, "mutation_project_skipped", busy=busy)
        return 0
    day = datetime.now().strftime("%Y-%m-%d")
    result = run_project(home, out, run=runner, max_children=args.max_children, day=day)
    if result.failed is not None:
        print(f"mutate_local: {result.failed} failed; mutmut did not run")
        line_event(state_dir, "mutation_project_failed", step=result.failed, stale_base=result.stale_base)
        return 0
    report = out / f"{day}.md"
    prune_reports(out)
    stale = " (on a stale origin/main: the fetch failed)" if result.stale_base else ""
    print(f"mutate_local: mutmut exited {result.code}; report {report}{stale}")
    line_event(
        state_dir, "mutation_project", code=result.code, report=str(report), stale_base=result.stale_base
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
