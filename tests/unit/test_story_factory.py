"""Story wiring: which board, what a story needs, where flags win, the one-story lock."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from airlie.engine import EngineHealth
from airlie.story import factory
from airlie.story.board import BoardError, Card, Column, NullBoard, text_card
from airlie.story.factory import (
    StoryBusy,
    card_for,
    check,
    make_board,
    make_pipeline,
    resolve,
    run_one,
    stories_dir,
    story_job,
    story_problems,
    tick,
)
from airlie.story.jira import JiraBoard
from airlie.story.model import StorySetupError
from airlie.story.publish import Publisher
from airlie.story.state import StoryState, story_lock
from airlie.topology.studio import StudioOrchestrator
from tests.unit.git_helpers import git
from tests.unit.test_fork_orchestrator import repo_with_marker_tests
from tests.unit.test_story_board import FakeJira
from tests.unit.test_story_board import board as jira_board_for
from tests.unit.test_story_pipeline import STORY_ROLES, RecordedGh, happy_engine
from tests.unit.test_studio_orchestrator import cfg_for


@pytest.fixture(autouse=True)
def _no_real_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """A JiraBoard that make_board builds has no mock transport. Should a test reach it, the request stops
    here instead of reaching a Jira site."""

    async def refuse(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a unit test sent a real request: {request.method} {request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


def test_the_board_is_jira_once_the_site_and_project_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = cfg_for(tmp_path, roles=STORY_ROLES)
    assert isinstance(make_board(cfg), NullBoard)
    monkeypatch.setenv("ATLASSIAN_BASE_URL", "https://acme.atlassian.net")
    monkeypatch.setenv("ATLASSIAN_EMAIL", "me@example.org")
    monkeypatch.setenv("ATLASSIAN_API_KEY", "t0ken")
    with pytest.raises(BoardError, match="ATLASSIAN_PROJECT_KEY"):
        make_board(cfg)  # half configured: a site but no project
    monkeypatch.setenv("ATLASSIAN_PROJECT_KEY", "AIR")
    board = make_board(cfg)
    assert isinstance(board, JiraBoard)
    assert (board.site, board.project) == ("https://acme.atlassian.net", "AIR")
    monkeypatch.delenv("ATLASSIAN_API_KEY")
    with pytest.raises(BoardError, match="ATLASSIAN_API_KEY"):
        make_board(cfg)


def test_story_problems_name_missing_roles(tmp_path: Path) -> None:
    problems = story_problems(cfg_for(tmp_path))  # the studio roles, but no product or architect
    assert any("story.product_role" in p for p in problems)
    assert any("story.architect_role" in p for p in problems)
    assert story_problems(cfg_for(tmp_path, roles=STORY_ROLES)) == []


@pytest.mark.parametrize("setting", ["trunk", "remote"])
@pytest.mark.parametrize("value", ["", "   ", "-x", "--upload-pack=true"])
def test_a_trunk_or_remote_git_would_read_as_an_option_is_a_problem(
    tmp_path: Path, setting: str, value: str
) -> None:
    # gh-stack passes --base on to git as a positional argument, and the pipeline passes both to `git fetch`
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {setting: value}})
    problems = story_problems(cfg)
    assert len(problems) == 1 and f"story.{setting}" in problems[0]


def test_an_ordinary_trunk_and_remote_are_not_a_problem(tmp_path: Path) -> None:
    story = {"trunk": "release/1.2", "remote": "upstream"}
    assert story_problems(cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": story})) == []


def test_resolve_prefers_flags_and_names_what_is_missing(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"test_cmd": "pytest -q"}})
    with pytest.raises(StorySetupError, match="no repository"):
        resolve(cfg, None, None)
    assert resolve(cfg, repo, None) == (repo.resolve(), "pytest -q")
    assert resolve(cfg, repo, "./check.sh")[1] == "./check.sh"
    with pytest.raises(StorySetupError, match="not a git repository"):
        resolve(cfg, tmp_path, None)
    cfg.story.test_cmd = None
    with pytest.raises(StorySetupError, match="no test command"):
        resolve(cfg, repo, None)


def test_a_blank_test_command_is_no_test_command(tmp_path: Path) -> None:
    # a blank gate would pass every layer
    repo = repo_with_marker_tests(tmp_path)
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"test_cmd": "pytest -q"}})
    assert resolve(cfg, repo, "  ")[1] == "pytest -q"  # a blank flag does not replace the configured command
    cfg.story.test_cmd = "  "
    for flag in (None, "", " \t"):
        with pytest.raises(StorySetupError, match="no test command"):
            resolve(cfg, repo, flag)


def test_resolve_uses_the_configured_repository_resolved_once(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(repo)
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"repo": str(link), "test_cmd": "true"}})
    # StoryState.open compares the repository's spelling verbatim: one spelling goes to every caller
    assert resolve(cfg, None, None) == (repo.resolve(), "true")
    assert resolve(cfg, link, None)[0] == repo.resolve()


async def test_run_one_refuses_while_another_story_holds_the_lock(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, roles=STORY_ROLES)
    with story_lock(stories_dir(cfg)) as held:
        assert held
        with pytest.raises(StoryBusy):
            await run_one(
                cfg, board=NullBoard(stories_dir(cfg)), card=text_card("x"), repo=tmp_path, test_cmd="true"
            )


async def test_run_one_builds_the_story_with_the_pipeline_and_lets_go_of_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    cfg = cfg_for(
        tmp_path, roles=STORY_ROLES, extra={"story": {"trunk": git(repo, "branch", "--show-current")}}
    )
    gh = RecordedGh()
    monkeypatch.setattr(factory, "_engine", lambda cfg: happy_engine())
    monkeypatch.setattr(factory, "Publisher", lambda *, remote: Publisher(remote=remote, run=gh))
    card = text_card("Let users upload an avatar")
    st = await run_one(cfg, board=NullBoard(stories_dir(cfg)), card=card, repo=repo, test_cmd="./check.sh")
    assert st.stage == "review" and st.blocked is None and [p["number"] for p in st.prs] == [11, 12]
    assert st.path == stories_dir(cfg) / card.key and (st.path / "state.json").is_file()
    assert ["gh", "stack", "submit", "--auto", "--remote", "origin"] in gh.calls
    with story_lock(stories_dir(cfg)) as held:
        assert held  # the lock was let go when the story ended


def test_the_pipeline_is_wired_to_the_story_remote_and_the_engine_of_the_build_role(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"remote": "upstream"}})
    pipeline = make_pipeline(cfg, board=NullBoard(stories_dir(cfg)), repo=tmp_path, test_cmd="true")
    assert pipeline.publisher.remote == "upstream" and pipeline.test_cmd == "true"
    assert pipeline.repo == tmp_path.resolve()
    studio = pipeline.studio()
    assert isinstance(studio, StudioOrchestrator)
    assert getattr(studio.engine, "base_url", None) == "http://x/v1"


# ---- card_for: the issue is fetched now, so a bad reference fails before any job starts ----


async def test_a_written_request_is_a_text_card_on_a_null_board(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, roles=STORY_ROLES)
    board, card = await card_for(cfg, None, "Let users upload an avatar")
    assert isinstance(board, NullBoard) and board.stories_dir == stories_dir(cfg)
    assert card == text_card("Let users upload an avatar")


async def test_an_empty_request_is_refused_and_an_empty_reference_is_not_a_request(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, roles=STORY_ROLES)
    with pytest.raises(StorySetupError, match="empty"):
        await card_for(cfg, None, " \n")
    with pytest.raises(BoardError, match="--text"):  # no Jira here, and "" is not a written request
        await card_for(cfg, "", None)


async def test_an_issue_is_fetched_from_the_board_when_the_card_is_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = cfg_for(tmp_path, roles=STORY_ROLES)
    jira = FakeJira()
    jira_board = jira_board_for(jira)
    monkeypatch.setattr(factory, "make_board", lambda cfg: jira_board)
    board, card = await card_for(cfg, "https://acme.atlassian.net/browse/AIR-7", None)
    assert board is jira_board and (card.key, card.ref, card.title) == ("AIR-7", "AIR-7", "Avatar upload")
    assert [r.url.path for r in jira.requests] == ["/rest/api/3/issue/AIR-7"]
    jira.requests.clear()
    with pytest.raises(BoardError, match="not a Jira issue key"):
        await card_for(cfg, "banana", None)
    assert jira.requests == []  # a reference that is no issue never reaches Jira


# ---- tick: one scheduler beat, one line for the log ----


class Intake:
    """A board whose intake query returns `top`; it counts how often it was asked."""

    def __init__(self, top: Card | None) -> None:
        self.top = top
        self.asked = 0

    async def card(self, ref: str) -> Card:
        raise BoardError("not needed here")

    async def next_card(self) -> Card | None:
        self.asked += 1
        return self.top

    async def move(self, card: Card, column: Column) -> None: ...

    async def comment(self, card: Card, text: str) -> None: ...


class Engine:
    """Answers the health probe of a tick."""

    def __init__(self, ok: bool | Exception) -> None:
        self.ok = ok
        self.probed = 0

    async def health(self) -> EngineHealth:
        self.probed += 1
        if isinstance(self.ok, Exception):
            raise self.ok
        return EngineHealth(ok=self.ok, models={})


TOP = Card("AIR-7", "10007", "AIR-7", "Avatar upload", "")


def tick_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, board: Any, engine: Engine
) -> tuple[Any, Path]:
    repo = repo_with_marker_tests(tmp_path)
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"repo": str(repo), "test_cmd": "./check.sh"}})
    monkeypatch.setattr(factory, "make_board", lambda cfg: board)
    monkeypatch.setattr(factory, "_engine", lambda cfg: engine)
    return cfg, repo


async def test_a_tick_without_a_board_has_nothing_to_pick_up(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, roles=STORY_ROLES)  # no Jira, and no repository either: neither is needed
    assert "no board configured" in await tick(cfg)


async def test_a_tick_builds_the_top_issue_and_reports_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = Intake(TOP)
    cfg, repo = tick_setup(tmp_path, monkeypatch, board, Engine(True))
    built: list[tuple[Any, Card, Path, str]] = []

    class Built:
        def __init__(self, b: Any, r: Path, t: str) -> None:
            self.args = (b, r, t)

        async def run(self, card: Card) -> StoryState:
            built.append((self.args[0], card, self.args[1], self.args[2]))
            return StoryState(path=tmp_path / "s", card=card.to_dict(), repo=str(repo), stage="review")

    monkeypatch.setattr(
        factory, "make_pipeline", lambda cfg, *, board, repo, test_cmd: Built(board, repo, test_cmd)
    )
    assert await tick(cfg) == "AIR-7 Avatar upload: review"
    assert built == [(board, TOP, repo.resolve(), "./check.sh")]
    with story_lock(stories_dir(cfg)) as held:
        assert held  # the beat let go of the lock


async def test_a_blocked_story_is_still_one_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, repo = tick_setup(tmp_path, monkeypatch, Intake(TOP), Engine(True))

    class Blocked:
        async def run(self, card: Card) -> StoryState:
            why = "layer 01: `./check.sh` fails\n  on b1;\tits worktree is kept"
            return StoryState(
                path=tmp_path / "s", card=card.to_dict(), repo=str(repo), stage="layers", blocked=why
            )

    monkeypatch.setattr(factory, "make_pipeline", lambda cfg, *, board, repo, test_cmd: Blocked())
    line = await tick(cfg)
    assert line == "AIR-7 Avatar upload: blocked (layer 01: `./check.sh` fails on b1; its worktree is kept)"
    assert "\n" not in line


async def test_a_tick_with_nothing_in_the_intake_query_is_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _ = tick_setup(tmp_path, monkeypatch, Intake(None), Engine(True))
    assert await tick(cfg) == "idle: no issue matches the intake query"


@pytest.mark.parametrize("health", [False, ConnectionError("refused")])
async def test_a_tick_does_not_start_a_story_while_the_engine_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, health: bool | Exception
) -> None:
    board = Intake(TOP)
    cfg, _ = tick_setup(tmp_path, monkeypatch, board, Engine(health))
    assert (await tick(cfg)).startswith("engine down: start it with")
    assert board.asked == 0  # the issue stays where it is


async def test_a_tick_does_nothing_while_a_story_is_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board, engine = Intake(TOP), Engine(True)
    cfg, _ = tick_setup(tmp_path, monkeypatch, board, engine)
    with story_lock(stories_dir(cfg)) as held:
        assert held
        assert await tick(cfg) == "busy: a story is in flight"
    assert (board.asked, engine.probed) == (0, 0)


async def test_a_tick_with_a_board_needs_a_repository_and_a_test_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = cfg_for(tmp_path, roles=STORY_ROLES)
    monkeypatch.setattr(factory, "make_board", lambda cfg: Intake(TOP))
    with pytest.raises(StorySetupError, match="no repository"):
        await tick(cfg)


# ---- story_job: what POST /v1/airlie/stories starts ----


async def test_a_story_job_is_named_after_its_card_and_follows_its_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"test_cmd": "./check.sh"}})
    ran: list[tuple[Card, Path, str]] = []
    prs = [{"number": 11, "branch": "b1", "url": "https://github.com/o/r/pull/11"}]

    async def fake_run_one(c: Any, *, board: Any, card: Card, repo: Path, test_cmd: str) -> StoryState:
        ran.append((card, repo, test_cmd))
        return StoryState(
            path=stories_dir(c) / card.key, card=card.to_dict(), repo=str(repo), stage="review", prs=prs
        )

    monkeypatch.setattr(factory, "run_one", fake_run_one)
    request = "Let users upload an avatar"
    job_id, events, work = await story_job(cfg, card=None, text=request, repo=str(repo), test_cmd=None)
    key = text_card(request).key
    assert (job_id, events) == (f"story-{key}", stories_dir(cfg) / key / "events.jsonl")
    assert ran == []  # nothing is built until the job runs
    result = await work
    assert result == {"stage": "review", "blocked": None, "prs": prs, "dir": str(stories_dir(cfg) / key)}
    assert ran == [(text_card(request), repo.resolve(), "./check.sh")]


async def test_a_story_job_with_an_issue_fetches_it_before_any_job_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"test_cmd": "./check.sh"}})
    jira = FakeJira()
    monkeypatch.setattr(factory, "make_board", lambda cfg: jira_board_for(jira))
    job_id, events, work = await story_job(cfg, card="AIR-7", text=None, repo=str(repo), test_cmd=None)
    work.close()  # not started here
    assert (job_id, events.parent.name) == ("story-AIR-7", "AIR-7")
    with pytest.raises(BoardError, match="not a Jira issue key"):
        await story_job(cfg, card="banana", text=None, repo=str(repo), test_cmd=None)


async def test_a_story_job_that_cannot_be_set_up_says_what_is_missing(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    with pytest.raises(StorySetupError, match="story.product_role.*; .*story.architect_role"):
        await story_job(cfg_for(tmp_path), card=None, text="x", repo=str(repo), test_cmd="true")
    cfg = cfg_for(tmp_path, roles=STORY_ROLES)
    with pytest.raises(StorySetupError, match="no test command"):
        await story_job(cfg, card=None, text="x", repo=str(repo), test_cmd=None)
    with pytest.raises(StorySetupError, match="no repository"):
        await story_job(cfg, card=None, text="x", repo=None, test_cmd="true")


async def test_a_story_topology_that_does_not_load_is_a_setup_problem_not_a_crash(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"topology": "nope"}})
    with pytest.raises(StorySetupError, match="unknown topology 'nope'"):
        await story_job(cfg, card=None, text="x", repo=str(tmp_path), test_cmd="true")


# ---- check: everything `task story:check` reports ----


def fake_publisher(problems: list[str], seen: list[tuple[str, Path]]) -> Any:
    class Fake:
        def __init__(self, *, remote: str = "origin") -> None:
            self.remote = remote

        async def check(self, repo: Path) -> list[str]:
            seen.append((self.remote, repo))
            return problems

    return Fake


async def test_check_without_a_repository_does_not_ask_gh_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, Path]] = []
    monkeypatch.setattr(factory, "Publisher", fake_publisher(["gh asked"], seen))
    cfg = cfg_for(tmp_path)  # no product or architect, no repository, no test command
    problems = await check(cfg, None, None)
    assert [p for p in problems if "story.product_role" in p] and [
        p for p in problems if "no repository" in p
    ]
    assert seen == [] and "gh asked" not in problems


async def test_check_adds_what_publishing_and_jira_need(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    seen: list[tuple[str, Path]] = []
    monkeypatch.setattr(factory, "Publisher", fake_publisher(["gh is not logged in"], seen))
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"remote": "upstream"}})
    assert await check(cfg, repo, "./check.sh") == ["gh is not logged in"]
    assert seen == [("upstream", repo.resolve())]

    async def jira_check(self: JiraBoard) -> list[str]:
        return ["project AIR has no status 'Blocked' (story.jira.statuses.blocked)"]

    monkeypatch.setattr(JiraBoard, "check", jira_check)  # nothing reaches a Jira site
    monkeypatch.setenv("ATLASSIAN_BASE_URL", "https://acme.atlassian.net")
    monkeypatch.setenv("ATLASSIAN_PROJECT_KEY", "AIR")
    monkeypatch.setenv("ATLASSIAN_EMAIL", "me@example.org")
    monkeypatch.setenv("ATLASSIAN_API_KEY", "t0ken")
    problems = await check(cfg, repo, "./check.sh")
    assert problems == [
        "gh is not logged in",
        "project AIR has no status 'Blocked' (story.jira.statuses.blocked)",
    ]


async def test_check_does_not_hand_git_a_remote_it_would_read_as_an_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    seen: list[tuple[str, Path]] = []
    monkeypatch.setattr(factory, "Publisher", fake_publisher(["asked"], seen))
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"remote": "--upload-pack=x"}})
    problems = await check(cfg, repo, "./check.sh")
    assert len(problems) == 1 and "story.remote" in problems[0] and seen == []


async def test_check_reports_a_half_configured_jira_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    monkeypatch.setattr(factory, "Publisher", fake_publisher([], []))
    monkeypatch.setenv("ATLASSIAN_BASE_URL", "https://acme.atlassian.net")
    monkeypatch.setenv("ATLASSIAN_EMAIL", "me@example.org")
    monkeypatch.setenv("ATLASSIAN_API_KEY", "t0ken")
    cfg = cfg_for(tmp_path, roles=STORY_ROLES)
    problems = await check(cfg, repo, "./check.sh")
    assert len(problems) == 1 and "ATLASSIAN_PROJECT_KEY" in problems[0]
