"""The whole-project mutation run, weekly on this Mac (launchd: docs/integrations/mutation-weekly.plist).

It waits for the line's two lanes to be free (polling every POLL_S, giving the week up at WAIT_UNTIL_H:00),
makes a fresh detached worktree of origin/main under ~/.local/state/airlie/mutation, runs
`scripts/mutate_changed.py --scope project` there and writes the backlog report to `<day>.md` beside
it, keeping the newest KEEP_REPORTS. The worktree is removed whatever happens, SIGTERM (launchd
stopping the job) included. The exit code is 0 whatever mutmut found, so launchd never respawns it; the
outcome is a line event, `mutation_project` or `mutation_project_skipped`.

    uv run python scripts/mutate_local.py [--max-children N]
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import FrameType

from airlie.config import load_config
from airlie.story.factory import stories_dir
from airlie.story.fixes import home_checkout, line_event
from airlie.story.state import Lane, lock_held

ROOT = Path(__file__).resolve().parents[1]
WAIT_UNTIL_H = 6  # local hour at which a busy line skips the week
POLL_S = 600
KEEP_REPORTS = 12
LANES: tuple[Lane, ...] = ("plan", "build")


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
) -> int:
    """mutmut's exit code for the whole project, run in a fresh worktree of origin/main, always removed."""
    tree = out / "worktree"
    git = ["git", "-C", str(home), "worktree"]
    run(["git", "-C", str(home), "fetch", "origin", "main"], home)
    if tree.exists():  # an interrupted run's, which a SIGKILL or a power cut left
        run([*git, "remove", "--force", str(tree)], home)
    run([*git, "prune"], home)
    try:
        run([*git, "add", "--detach", str(tree), "origin/main"], home)
        run(["uv", "sync", "--frozen"], tree)
        mutate = ["uv", "run", "python", "scripts/mutate_changed.py", "--scope", "project"]
        return run([*mutate, "--max-children", str(max_children), "--report", str(out / f"{day}.md")], tree)
    finally:
        run([*git, "remove", "--force", str(tree)], home)


def prune_reports(out: Path, keep: int = KEEP_REPORTS) -> None:
    """Remove all but the newest `keep` reports (the day-named `*.md` files sort by date)."""
    reports = sorted(out.glob("*.md"))
    for old in reports[: max(0, len(reports) - keep)]:
        old.unlink(missing_ok=True)


def _run(args: list[str], cwd: Path) -> int:
    return subprocess.run(args, cwd=cwd, check=False).returncode


def _stop(_signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(143)  # unwinds run_project's finally, so the worktree goes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--max-children", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    args = ap.parse_args()
    home = home_checkout()
    if home is None:
        print("mutate_local: airlie does not run from a git checkout here; nothing to mutate")
        return 2
    cfg = load_config(ROOT / "airlie.yaml")
    state_dir = cfg.observability.state_dir
    out = Path("~/.local/state/airlie/mutation").expanduser()
    out.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, _stop)
    if not wait_for_lanes(stories_dir(cfg), now=datetime.now, sleep=time.sleep):
        busy = lanes_busy(stories_dir(cfg))
        print(f"mutate_local: lanes {busy} stayed busy until {WAIT_UNTIL_H}:00; skipping this week")
        line_event(state_dir, "mutation_project_skipped", busy=busy)
        return 0
    day = datetime.now().strftime("%Y-%m-%d")
    code = run_project(home, out, run=_run, max_children=args.max_children, day=day)
    report = out / f"{day}.md"
    prune_reports(out)
    print(f"mutate_local: mutmut exited {code}; report {report}")
    line_event(state_dir, "mutation_project", code=code, report=str(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
