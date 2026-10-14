"""`airlie story run|tick|check`: arguments, setup problems, exit codes. The pipeline itself is stubbed here
(tests/unit/test_story_pipeline.py drives it for real)."""

from __future__ import annotations

import random
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from airlie import cli
from airlie.story import factory
from airlie.story.state import StoryState, story_lock
from tests.unit.test_fork_orchestrator import repo_with_marker_tests
from tests.unit.test_story_board import FakeJira
from tests.unit.test_story_board import board as jira_board_for
from tests.unit.test_story_pipeline import STORY_ROLES
from tests.unit.test_studio_orchestrator import cfg_for

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_real_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Should a story command build a JiraBoard without a mock transport, its request stops here."""

    async def refuse(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a unit test sent a real request: {request.method} {request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


def story_cfg(tmp_path: Path, **story: object) -> Path:
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": story})
    assert cfg.source_path is not None
    return cfg.source_path


def test_run_needs_exactly_one_of_an_issue_or_text(tmp_path: Path) -> None:
    p = str(story_cfg(tmp_path))
    assert runner.invoke(cli.app, ["story", "run", "--config", p]).exit_code == 2
    assert runner.invoke(cli.app, ["story", "run", "AIR-7", "--text", "x", "--config", p]).exit_code == 2


def test_run_names_what_is_missing(tmp_path: Path) -> None:
    r = runner.invoke(cli.app, ["story", "run", "--text", "avatars", "--config", str(story_cfg(tmp_path))])
    assert r.exit_code == 1 and "no repository" in r.output


def test_a_text_story_prints_its_prs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = repo_with_marker_tests(tmp_path)
    p = story_cfg(tmp_path, test_cmd="./check.sh")
    seen: dict[str, object] = {}

    async def fake_run_one(cfg, *, board, card, repo, test_cmd):  # type: ignore[no-untyped-def]
        seen.update(card=card.title, repo=repo, test_cmd=test_cmd, board=type(board).__name__)
        prs = [{"number": 11, "branch": "b1", "url": "https://github.com/o/r/pull/11"}]
        return StoryState(path=tmp_path / "s", card=card.to_dict(), repo=str(repo), stage="review", prs=prs)

    monkeypatch.setattr(factory, "run_one", fake_run_one)
    args = ["story", "run", "--text", "Let users upload an avatar", "--repo", str(repo), "--config", str(p)]
    r = runner.invoke(cli.app, args)
    assert r.exit_code == 0, r.output
    assert seen == {
        "card": "Let users upload an avatar",
        "repo": repo.resolve(),
        "test_cmd": "./check.sh",
        "board": "NullBoard",
    }
    assert "pull/11" in r.output


def test_a_blocked_story_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = repo_with_marker_tests(tmp_path)
    p = story_cfg(tmp_path, test_cmd="./check.sh", repo=str(repo))

    async def blocked(cfg, *, board, card, repo, test_cmd):  # type: ignore[no-untyped-def]
        why = "tests fail: [/x] is not markup"  # model text reaches the terminal as text
        return StoryState(
            path=tmp_path / "s", card=card.to_dict(), repo=str(repo), stage="layers", blocked=why
        )

    monkeypatch.setattr(factory, "run_one", blocked)
    r = runner.invoke(cli.app, ["story", "run", "--text", "x", "--config", str(p)])
    assert r.exit_code == 1 and "blocked" in r.output and "tests fail: [/x] is not markup" in r.output


def test_tick_without_a_board_says_so(tmp_path: Path) -> None:
    r = runner.invoke(cli.app, ["story", "tick", "--config", str(story_cfg(tmp_path))])
    assert r.exit_code == 0 and "no board configured" in r.output


def test_check_lists_problems(tmp_path: Path) -> None:
    r = runner.invoke(cli.app, ["story", "check", "--config", str(story_cfg(tmp_path))])
    assert r.exit_code == 1 and "no repository" in r.output


# ---- run ----


def test_an_issue_is_fetched_through_the_board_and_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    p = story_cfg(tmp_path, test_cmd="./check.sh", repo=str(repo))
    jira = FakeJira()
    jira_board = jira_board_for(jira)
    monkeypatch.setattr(factory, "make_board", lambda cfg: jira_board)
    seen: list[tuple[object, str, str]] = []

    async def fake_run_one(cfg, *, board, card, repo, test_cmd):  # type: ignore[no-untyped-def]
        seen.append((board, card.key, card.title))
        return StoryState(path=tmp_path / "s", card=card.to_dict(), repo=str(repo), stage="review")

    monkeypatch.setattr(factory, "run_one", fake_run_one)
    r = runner.invoke(
        cli.app, ["story", "run", "https://acme.atlassian.net/browse/AIR-7", "--config", str(p)]
    )
    assert r.exit_code == 0, r.output
    assert seen == [(jira_board, "AIR-7", "Avatar upload")]


def test_an_issue_without_a_jira_project_points_to_text(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    p = story_cfg(tmp_path, test_cmd="./check.sh", repo=str(repo))
    r = runner.invoke(cli.app, ["story", "run", "AIR-7", "--config", str(p)])
    assert r.exit_code == 1 and "--text" in r.output and "Traceback" not in r.output


def test_a_story_already_in_flight_is_a_message_not_a_crash(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"test_cmd": "./check.sh", "repo": str(repo)}})
    assert cfg.source_path is not None
    with story_lock(factory.stories_dir(cfg)) as held:
        assert held
        r = runner.invoke(cli.app, ["story", "run", "--text", "x", "--config", str(cfg.source_path)])
    assert r.exit_code == 1 and "another story is in flight" in r.output


def test_setup_problems_print_as_text_not_as_markup(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    p = story_cfg(tmp_path, test_cmd="./check.sh", repo=str(repo), product_role="[/x]")
    r = runner.invoke(cli.app, ["story", "run", "--text", "x", "--config", str(p)])
    assert r.exit_code == 1 and "story.product_role" in r.output and "[/x]" in r.output
    assert isinstance(r.exception, SystemExit)  # not a MarkupError


def test_a_story_topology_that_does_not_load_is_named(tmp_path: Path) -> None:
    p = story_cfg(tmp_path, topology="nope")
    r = runner.invoke(cli.app, ["story", "run", "--text", "x", "--config", str(p)])
    assert r.exit_code == 1 and "unknown topology 'nope'" in r.output


# ---- tick and check ----


def test_a_tick_prints_one_log_line_with_the_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_tick(cfg):  # type: ignore[no-untyped-def]
        return "AIR-7 [/x] " + "a long title " * 20 + ": review"

    monkeypatch.setattr(factory, "tick", fake_tick)
    r = runner.invoke(cli.app, ["story", "tick", "--config", str(story_cfg(tmp_path))])
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()  # a log line is never wrapped at the terminal's width
    assert len(lines) == 1 and re.fullmatch(
        r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d AIR-7 \[/x\] .*: review", lines[0]
    )


def test_a_tick_that_cannot_run_is_a_message_and_exit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_tick(cfg):  # type: ignore[no-untyped-def]
        raise factory.StorySetupError("no repository: set story.repo")

    monkeypatch.setattr(factory, "tick", fake_tick)
    r = runner.invoke(cli.app, ["story", "tick", "--config", str(story_cfg(tmp_path))])
    assert r.exit_code == 1 and "no repository" in r.output


def test_a_tick_needs_the_story_roles(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path)  # no product or architect
    assert cfg.source_path is not None
    r = runner.invoke(cli.app, ["story", "tick", "--config", str(cfg.source_path)])
    assert r.exit_code == 1 and "story.product_role" in r.output


def test_check_is_ready_when_nothing_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Path | None, str | None]] = []

    async def fake_check(cfg, repo, test_cmd):  # type: ignore[no-untyped-def]
        seen.append((repo, test_cmd))
        return []

    monkeypatch.setattr(factory, "check", fake_check)
    args = ["story", "check", "--repo", str(tmp_path), "--test-cmd", "make test", "--config"]
    r = runner.invoke(cli.app, [*args, str(story_cfg(tmp_path))])
    assert r.exit_code == 0 and "ready" in r.output
    assert seen == [(tmp_path, "make test")]


def test_check_prints_each_problem_as_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_check(cfg, repo, test_cmd):  # type: ignore[no-untyped-def]
        return ["gh is not logged in: run `gh auth login`", "project AIR has no status '[/x]'"]

    monkeypatch.setattr(factory, "check", fake_check)
    r = runner.invoke(cli.app, ["story", "check", "--config", str(story_cfg(tmp_path))])
    assert r.exit_code == 1 and "gh auth login" in r.output and "no status '[/x]'" in r.output


def test_every_story_command_runs_its_coroutine_so_that_a_signal_stops_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[str] = []
    real = cli._run_stoppable

    def spy(coro):  # type: ignore[no-untyped-def]
        ran.append(coro.__qualname__.rsplit(".", 1)[-1])
        return real(coro)

    async def fake_run_one(cfg, *, board, card, repo, test_cmd):  # type: ignore[no-untyped-def]
        return StoryState(path=tmp_path / "s", card=card.to_dict(), repo=str(repo), stage="review")

    async def fake_tick(cfg):  # type: ignore[no-untyped-def]
        return "idle: no issue matches the intake query"

    async def fake_check(cfg, repo, test_cmd):  # type: ignore[no-untyped-def]
        return []

    monkeypatch.setattr(cli, "_run_stoppable", spy)
    monkeypatch.setattr(factory, "run_one", fake_run_one)
    monkeypatch.setattr(factory, "tick", fake_tick)
    monkeypatch.setattr(factory, "check", fake_check)
    repo = repo_with_marker_tests(tmp_path)
    p = str(story_cfg(tmp_path, test_cmd="./check.sh", repo=str(repo)))
    for args in (["run", "--text", "x"], ["tick"], ["check"]):
        r = runner.invoke(cli.app, ["story", *args, "--config", p])
        assert r.exit_code == 0, r.output
    assert ran == ["go", "fake_tick", "fake_check"]  # the coroutines of run, tick and check


def test_the_taskfile_runs_the_story_commands() -> None:
    root = Path(__file__).resolve().parents[2]
    tasks = yaml.safe_load((root / "Taskfile.yml").read_text())["tasks"]
    assert {name: tasks[name]["cmds"] for name in ("story", "story:tick", "story:check")} == {
        "story": ["{{.RUN}} airlie story run {{.CLI_ARGS}}"],
        "story:tick": ["{{.RUN}} airlie story tick {{.CLI_ARGS}}"],
        "story:check": ["{{.RUN}} airlie story check {{.CLI_ARGS}}"],
    }
    assert all(tasks[name]["desc"] for name in ("story", "story:tick", "story:check"))


# ---- SIGTERM and SIGHUP stop a story the way SIGINT does ----

CHILD = """
import signal, sys
from pathlib import Path

import typer

shell, hangup = sys.argv[1:3]
sys.argv = sys.argv[:1]  # typer parses what is left
# a test run can be started with a hangup ignored (nohup): say which of the two cases this child is
signal.signal(signal.SIGHUP, signal.SIG_IGN if hangup == "nohup" else signal.SIG_DFL)

from airlie.cli import _run_stoppable
from airlie.story.publish import run_cmd


async def work():
    try:
        await run_cmd(["sh", "-c", shell], Path("."))
    finally:
        print("cleaned up", flush=True)


def command():
    _run_stoppable(work())


typer.run(command)  # as in the CLI, where click turns typer.Exit into the exit status
"""


def running(pattern: str) -> bool:
    """Whether some process has `pattern` in its command line."""
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


def eventually(check: Callable[[], bool], within: float = 20.0) -> bool:
    deadline = time.monotonic() + within
    while not check():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


@contextmanager
def running_story(tmp_path: Path, hangup: str) -> Iterator[tuple[subprocess.Popen[str], str]]:
    """A child process running a stoppable coroutine that runs `sh` with a long sleep under it, as gh runs
    git; yielded once the sleep itself is up, and killed at the end if it still runs."""
    sleeper = f"sleep 30.{random.randrange(10**8, 10**9)}"  # no other process has this command line
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, f"{sleeper} & wait", hangup],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert eventually(lambda: running(f"^{sleeper}") or child.poll() is not None)
        if child.poll() is not None:
            pytest.fail(f"the child ended before it started the sleep: {child.communicate()}")
        yield child, sleeper
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        for pipe in (child.stdout, child.stderr):
            if pipe:
                pipe.close()


@pytest.mark.parametrize(("sig", "status"), [(signal.SIGTERM, 143), (signal.SIGHUP, 129)])
def test_a_term_or_hangup_cancels_the_story_and_kills_what_gh_started(
    tmp_path: Path, sig: signal.Signals, status: int
) -> None:
    with running_story(tmp_path, "default") as (child, sleeper):
        child.send_signal(sig)
        out, err = child.communicate(timeout=10)  # at once, not when the sleep has run its 30 s out
    assert child.returncode == status  # 128 + the signal, as a shell reports a process a signal ended
    assert "cleaned up" in out  # the cancel ran the coroutine's cleanup
    assert sig.name in err
    assert eventually(lambda: not running(sleeper))  # nothing gh started is left running


def test_a_hangup_that_was_ignored_when_airlie_started_stays_ignored(tmp_path: Path) -> None:
    with running_story(tmp_path, "nohup") as (
        child,
        sleeper,
    ):  # `nohup task story ... &`: it outlives its terminal
        child.send_signal(signal.SIGHUP)
        time.sleep(0.5)
        assert child.poll() is None and running(f"^{sleeper}")
        child.send_signal(signal.SIGTERM)  # a TERM still stops it
        out, _ = child.communicate(timeout=10)
    assert child.returncode == 143 and "cleaned up" in out
    assert eventually(lambda: not running(sleeper))


def test_a_stoppable_run_is_asyncio_run_for_everything_else() -> None:
    async def ok() -> int:
        return 42

    async def boom() -> int:
        raise ValueError("engine fell over")

    assert cli._run_stoppable(ok()) == 42
    with pytest.raises(ValueError, match="engine fell over"):
        cli._run_stoppable(boom())
