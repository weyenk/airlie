"""`airlie story run|tick|check`: arguments, setup problems, exit codes. The pipeline itself is stubbed here
(tests/unit/test_story_pipeline.py drives it for real)."""

from __future__ import annotations

import gc
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from airlie import cli
from airlie.story import factory
from airlie.story.board import BoardError
from airlie.story.jira import JiraBoard
from airlie.story.publish import Checked, Publisher
from airlie.story.state import StoryState, story_lock
from tests.unit.git_helpers import git
from tests.unit.test_fork_orchestrator import repo_with_marker_tests
from tests.unit.test_story_board import FakeJira
from tests.unit.test_story_board import board as jira_board_for
from tests.unit.test_story_pipeline import STORY_ROLES, checkout_of
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
    p = story_cfg(
        tmp_path, test_cmd="pytest -q"
    )  # airlie_run's: a story's gate is AGENTS.md's, or --test-cmd
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
        "repo": repo,
        "test_cmd": None,
        "board": "NullBoard",
    }
    assert "pull/11" in r.output
    r = runner.invoke(cli.app, [*args, "--test-cmd", "make check"])
    assert r.exit_code == 0 and seen["test_cmd"] == "make check"  # a person's command wins


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
    p = story_cfg(tmp_path, repo=str(repo))  # a written request's: an issue's repository is its label
    jira = FakeJira()
    jira_board = jira_board_for(jira)
    monkeypatch.setattr(factory, "make_board", lambda cfg: jira_board)
    seen: list[tuple[object, ...]] = []

    async def fake_run_one(cfg, *, board, card, repo, test_cmd):  # type: ignore[no-untyped-def]
        seen.append((board, card.key, card.title, card.labels, repo, test_cmd))
        return StoryState(path=tmp_path / "s", card=card.to_dict(), repo=str(repo), stage="review")

    monkeypatch.setattr(factory, "run_one", fake_run_one)
    r = runner.invoke(
        cli.app, ["story", "run", "https://acme.atlassian.net/browse/AIR-7", "--config", str(p)]
    )
    assert r.exit_code == 0, r.output
    assert seen == [(jira_board, "AIR-7", "Avatar upload", ("repo:o/repo", "frontend"), None, None)]


def test_an_issue_without_a_repository_is_told_why_and_the_run_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = story_cfg(tmp_path, repos_root=str(tmp_path / "git"))  # no checkout of o/repo in it
    jira = FakeJira()
    monkeypatch.setattr(factory, "make_board", lambda cfg: jira_board_for(jira))
    r = runner.invoke(cli.app, ["story", "run", "AIR-7", "--config", str(p)])
    assert r.exit_code == 1 and "no checkout of o/repo in" in r.output and "Traceback" not in r.output
    posted = [r.url.path for r in jira.requests if r.method == "POST"]
    assert posted == ["/rest/api/3/issue/AIR-7/comment", "/rest/api/3/issue/AIR-7/transitions"]


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
    seen: list[tuple[Path | None, str | None, str | None]] = []

    async def fake_check(cfg, repo, test_cmd, ref=None):  # type: ignore[no-untyped-def]
        seen.append((repo, test_cmd, ref))
        return Checked([], [])

    monkeypatch.setattr(factory, "check", fake_check)
    args = ["story", "check", "--repo", str(tmp_path), "--test-cmd", "make test", "--config"]
    r = runner.invoke(cli.app, [*args, str(story_cfg(tmp_path))])
    assert (
        r.exit_code == 0 and r.output.splitlines()[-1] == 'ready: task story -- ISSUE (or --text "request")'
    )
    assert seen == [(tmp_path, "make test", None)]
    r = runner.invoke(cli.app, ["story", "check", "AIR-7", "--config", str(story_cfg(tmp_path))])
    assert r.exit_code == 0 and r.output.splitlines()[-1] == "ready: task story -- AIR-7"
    assert seen[-1] == (None, None, "AIR-7")


def test_check_with_an_issue_prints_its_repository_trunk_and_gate_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = checkout_of(tmp_path / "git", "o/repo", "uv sync", "uv run pytest -q")
    trunk = git(repo, "branch", "--show-current")
    sha = git(repo, "rev-parse", trunk)
    p = story_cfg(tmp_path, repos_root=str(tmp_path / "git"), trunk=trunk)
    jira = FakeJira()
    monkeypatch.setattr(factory, "make_board", lambda cfg: jira_board_for(jira))

    async def jira_is_fine(self: object) -> list[str]:
        return []

    async def publishing_is_fine(self: Publisher, repo: Path) -> Checked:
        return Checked([], [])

    monkeypatch.setattr(JiraBoard, "check", jira_is_fine)
    monkeypatch.setattr(Publisher, "check", publishing_is_fine)  # gh is not asked anything here
    r = runner.invoke(cli.app, ["story", "check", "AIR-7", "--config", str(p)])
    assert r.exit_code == 0, r.output
    assert r.output.splitlines() == [
        f"AIR-7 is for o/repo (its repo: label): {repo.resolve()}, whose remote 'origin' is "
        "git@github.com:o/repo.git",
        f"trunk: {trunk} at {sha[:12]}, as last fetched (a story fetches it first)",
        f"test gate (AGENTS.md at {trunk}): uv sync && uv run pytest -q",
        "ready: task story -- AIR-7",
    ]
    assert [r.method for r in jira.requests] == ["GET"]  # no note, no move
    assert not (tmp_path / "state" / "stories").exists()  # no story state

    git(repo, "rm", "-q", "AGENTS.md")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "no gate")
    r = runner.invoke(cli.app, ["story", "check", "AIR-7", "--config", str(p)])
    assert r.exit_code == 1
    assert f"✗ AIR-7 would be blocked: o/repo has no test gate on {trunk} (" in r.output
    assert "it has no AGENTS.md. Add a `## Test gate` section" in r.output


def test_check_prints_its_notes_and_is_ready_when_they_are_all_it_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = ["warning: /k.pem is readable by group or others: chmod 600 it", "publishing to o/r as app[bot]"]

    async def fake_check(cfg, repo, test_cmd, ref=None):  # type: ignore[no-untyped-def]
        return Checked([], notes)

    monkeypatch.setattr(factory, "check", fake_check)
    r = runner.invoke(cli.app, ["story", "check", "--config", str(story_cfg(tmp_path))])
    assert r.exit_code == 0
    assert r.output.splitlines() == [*notes, 'ready: task story -- ISSUE (or --text "request")']


def test_check_prints_each_problem_as_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_check(cfg, repo, test_cmd, ref=None):  # type: ignore[no-untyped-def]
        return Checked(["gh is not logged in: run `gh auth login`", "project AIR has no status '[/x]'"], [])

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

    async def fake_check(cfg, repo, test_cmd, ref=None):  # type: ignore[no-untyped-def]
        return Checked([], [])

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

shell, hangup, stderr = sys.argv[1:4]
sys.argv = sys.argv[:1]  # typer parses what is left
# a test run can be started with a hangup ignored (nohup): say which of the two cases this child is
signal.signal(signal.SIGHUP, signal.SIG_IGN if hangup == "nohup" else signal.SIG_DFL)

from airlie.cli import _run_stoppable
from airlie.story.publish import PersonGit, person_env, run_cmd

if stderr == "hung-up":  # a terminal that hung up: every write and flush to it fails, as on a dead pty
    import errno
    import io

    class HungUp(io.TextIOBase):
        def write(self, text):
            raise OSError(errno.EIO, "Input/output error")

        def flush(self):
            raise OSError(errno.EIO, "Input/output error")

    sys.stderr = HungUp()


async def work():
    try:
        await run_cmd(["sh", "-c", shell], Path("."), person_env(PersonGit()))
    finally:
        print("cleaned up", flush=True)


def command():
    _run_stoppable(work())


typer.run(command)  # as in the CLI, where click turns typer.Exit into the exit status
"""


needs_pgrep = pytest.mark.skipif(
    shutil.which("pgrep") is None,
    reason="these tests find the sleep a story starts with pgrep, which is missing",
)


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
def running_story(
    tmp_path: Path, hangup: str, stderr: str = "fine"
) -> Iterator[tuple[subprocess.Popen[str], str]]:
    """A child process running a stoppable coroutine that runs `sh` with a long sleep under it, as gh runs
    git; yielded once the sleep itself is up, and killed at the end if it still runs."""
    sleeper = f"sleep 30.{random.randrange(10**8, 10**9)}"  # no other process has this command line
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, f"{sleeper} & wait", hangup, stderr],
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


@needs_pgrep
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


@needs_pgrep
def test_a_hangup_that_was_ignored_when_airlie_started_stays_ignored(tmp_path: Path) -> None:
    # `nohup task story ... &`: the story outlives its terminal
    with running_story(tmp_path, "nohup") as (child, sleeper):
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


@needs_pgrep
@pytest.mark.parametrize("stderr", ["closed-pipe", "hung-up"])
def test_a_stop_still_exits_with_its_signal_when_nobody_reads_stderr(tmp_path: Path, stderr: str) -> None:
    # closed-pipe: whatever read stderr has closed it. hung-up: the terminal is gone and every write fails.
    # The notice cannot be written either way; the exit status must still be 128 + the signal (what stderr
    # refused would otherwise fail again when Python flushes it at exit, and the status would be 120).
    with running_story(tmp_path, "default", "hung-up" if stderr == "hung-up" else "fine") as (child, sleeper):
        if stderr == "closed-pipe":
            assert child.stderr is not None
            child.stderr.close()
        child.send_signal(signal.SIGTERM)
        out, _ = child.communicate(timeout=10)
    assert child.returncode == 143 and "cleaned up" in out
    assert eventually(lambda: not running(sleeper))


def test_a_stoppable_run_puts_back_the_handlers_it_found() -> None:
    def mine(signum: int, frame: object) -> None: ...

    async def ok() -> int:
        return 1

    async def boom() -> int:
        raise ValueError("engine fell over")

    found = {sig: signal.signal(sig, mine) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        assert cli._run_stoppable(ok()) == 1
        assert [signal.getsignal(sig) for sig in found] == [mine, mine]
        with pytest.raises(ValueError, match="engine fell over"):
            cli._run_stoppable(boom())
        assert [signal.getsignal(sig) for sig in found] == [mine, mine]  # after an error too
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        assert cli._run_stoppable(ok()) == 1
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN  # an ignored hangup is left ignored
    finally:
        for sig, handler in found.items():
            signal.signal(sig, handler)


def test_off_the_main_thread_a_stoppable_run_installs_nothing_and_still_runs() -> None:
    async def ok() -> int:
        return 42

    outcome: list[object] = []
    before = [signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)]

    def worker() -> None:
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            try:
                outcome.append(cli._run_stoppable(ok()))
            except BaseException as e:  # noqa: BLE001 - the test says what it was, not a traceback in a thread
                outcome.append(f"{type(e).__name__}: {e}")
            gc.collect()  # a coroutine that was never awaited warns when it is collected
        outcome.append([str(w.message) for w in seen if "never awaited" in str(w.message)])

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert outcome == [42, []]  # it ran, and no coroutine was dropped on the way
    assert [signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)] == before


# ---- text from a model, an issue or Jira is printed as it was written ----


def literal(output: str, *codes: str) -> None:
    """Each emoji code is in `output` as typed, and Rich has not made an emoji of any of them."""
    assert all(code in output for code in codes), output
    assert not any(emoji in output for emoji in "⚠❌🚀🎉✨"), output


def test_a_stop_reason_and_the_pull_requests_print_as_typed_not_as_emoji(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    p = story_cfg(tmp_path, test_cmd="./check.sh", repo=str(repo))
    prs = [{"number": 11, "branch": "b:rocket:", "url": "https://github.com/o/r/pull/:tada:"}]

    async def blocked(cfg, *, board, card, repo, test_cmd):  # type: ignore[no-untyped-def]
        why = "tests fail :warning: on :x:"
        return StoryState(
            path=tmp_path / ":sparkles:",
            card=card.to_dict(),
            repo=str(repo),
            stage="layers",
            blocked=why,
            prs=prs,
        )

    monkeypatch.setattr(factory, "run_one", blocked)
    r = runner.invoke(cli.app, ["story", "run", "--text", "x", "--config", str(p)])
    assert r.exit_code == 1
    literal(r.output, ":warning:", ":x:", ":rocket:", ":tada:", ":sparkles:/state.json")


def test_errors_and_problems_print_as_typed_not_as_emoji(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    p = story_cfg(tmp_path, test_cmd="./check.sh", repo=str(repo))

    async def jira_says_no(cfg, *, board, card, repo, test_cmd):  # type: ignore[no-untyped-def]
        raise BoardError("Jira GET issue/AIR-7: 404 :warning: no such issue")

    async def cannot_tick(cfg):  # type: ignore[no-untyped-def]
        raise factory.StorySetupError(":warning: no repository: set story.repo")

    monkeypatch.setattr(factory, "run_one", jira_says_no)
    monkeypatch.setattr(factory, "tick", cannot_tick)
    for args, code in (
        (["run", "--text", "x"], ":warning: no such issue"),  # a board error
        (["tick"], ":warning: no repository"),  # a setup error
    ):
        r = runner.invoke(cli.app, ["story", *args, "--config", str(p)])
        assert r.exit_code == 1
        literal(r.output, code)
    # a role that is not one, a topology that is not one: both are config text
    (tmp_path / "roles").mkdir()
    named = story_cfg(tmp_path / "roles", test_cmd="./check.sh", repo=str(repo), product_role=":warning:")
    r = runner.invoke(cli.app, ["story", "run", "--text", "x", "--config", str(named)])
    assert r.exit_code == 1
    literal(r.output, "story.product_role names ':warning:'")
    (tmp_path / "topology").mkdir()
    r = runner.invoke(
        cli.app,
        ["story", "run", "--text", "x", "--config", str(story_cfg(tmp_path / "topology", topology=":x:"))],
    )
    assert r.exit_code == 1
    literal(r.output, "unknown topology ':x:'")


def test_the_tick_line_and_the_check_problems_print_as_typed_not_as_emoji(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_tick(cfg):  # type: ignore[no-untyped-def]
        return "AIR-7 :warning: Avatar upload: blocked (:x: tests fail)"

    async def fake_check(cfg, repo, test_cmd, ref=None):  # type: ignore[no-untyped-def]
        return Checked([":warning: project AIR has no status ':x:'"], [":x: a note"])

    monkeypatch.setattr(factory, "tick", fake_tick)
    monkeypatch.setattr(factory, "check", fake_check)
    p = str(story_cfg(tmp_path))
    r = runner.invoke(cli.app, ["story", "tick", "--config", p])
    assert r.exit_code == 0
    literal(r.output, ":warning: Avatar upload", "(:x: tests fail)")
    r = runner.invoke(cli.app, ["story", "check", "--config", p])
    assert r.exit_code == 1
    literal(r.output, ":warning: project AIR", "no status ':x:'", ":x: a note")


# ---- a crash never prints the locals of the frames it passed through ----

CRASH = """
import sys

import typer

# make Typer's own default what it would be in a release that showed locals: the setting in cli.py must hold
typer.Typer.__init__.__kwdefaults__["pretty_exceptions_show_locals"] = True

from airlie import cli
from airlie.story import factory


async def tick(cfg):
    email, token = "me@example.org", "t0ken-SENTINEL"  # what jira_board holds while it builds a board
    raise RuntimeError("the board fell over")


factory.tick = tick
sys.argv = ["airlie", "story", "tick", "--config", sys.argv[1]]
cli.main()
"""


def test_a_traceback_never_shows_the_locals_of_the_frames_it_passes(tmp_path: Path) -> None:
    r = subprocess.run(
        [sys.executable, "-c", CRASH, str(story_cfg(tmp_path))],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    crashed = r.returncode != 0 and "RuntimeError" in r.stderr and "the board fell over" in r.stderr
    assert crashed, r.stderr  # a traceback was printed, so its absence of locals means something
    assert "t0ken-SENTINEL" not in r.stderr and "me@example.org" not in r.stderr
