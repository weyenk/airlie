"""Story wiring: which board, what a story needs, where flags win, the one-story lock."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from airlie.board import BoardStore
from airlie.engine import EngineHealth, FakeEngine
from airlie.story import factory
from airlie.story import github_app as github_app_module
from airlie.story.board import NOTE_PREFIX, BoardError, Card, Column, NullBoard, text_card
from airlie.story.factory import (
    Place,
    StoryBusy,
    card_for,
    check,
    describe_board,
    github_app,
    make_board,
    make_pipeline,
    place,
    run_one,
    stories_dir,
    story_job,
    story_problems,
    tick,
)
from airlie.story.gate import missing_gate
from airlie.story.github_app import GitHubApp
from airlie.story.jira import JiraBoard
from airlie.story.local_board import LocalBoard
from airlie.story.model import RepositoryError, StorySetupError
from airlie.story.publish import Checked, Publisher
from airlie.story.state import StoryState, story_lock
from airlie.topology.studio import StudioOrchestrator
from tests.unit.git_helpers import git
from tests.unit.github_helpers import FakeGitHub, app_spec, fake_app, key_file
from tests.unit.test_fork_orchestrator import repo_with_marker_tests
from tests.unit.test_story_board import FakeJira
from tests.unit.test_story_board import board as jira_board_for
from tests.unit.test_story_pipeline import (
    STORY_ROLES,
    RecordedGh,
    agents_md,
    checkout_of,
    commit_file,
    fenced,
    happy_engine,
)
from tests.unit.test_studio_orchestrator import cfg_for as studio_cfg_for


def cfg_for(tmp_path: Path, *, extra: dict[str, Any] | None = None, **kw: Any) -> Any:
    """The studio tests' config with the story settings that keep these tests' scripted replies in order:
    planning in one go and no survey (the rounds and the survey have tests of their own)."""
    extra = dict(extra or {})
    # no tester: it is a subtask per layer with scripted replies of its own (one test runs with it)
    extra["story"] = {"plan_round_steps": 0, "survey": False, "tester_role": None, **extra.get("story", {})}
    kw.setdefault("done_check", False)  # it costs the coder one scripted reply per layer
    return studio_cfg_for(tmp_path, extra=extra, **kw)


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
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"board": "jira"}})
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


def test_describe_board_names_the_board_in_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`task story:check` says which board stories come from before anything else."""
    local = cfg_for(tmp_path, roles=STORY_ROLES, extra={"board": {"prefix": "HDS"}})
    assert describe_board(local) == (
        f"board: local, {tmp_path / 'state' / 'board.sqlite'} (keys HDS-n; story.board: local)"
    )
    jira = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"board": "jira"}})
    assert describe_board(jira) == (
        "board: Jira (story.board: jira), but no site or project is set: ATLASSIAN_BASE_URL and "
        "ATLASSIAN_PROJECT_KEY"
    )
    monkeypatch.setenv("ATLASSIAN_BASE_URL", "https://acme.atlassian.net")
    monkeypatch.setenv("ATLASSIAN_PROJECT_KEY", "AIR")
    assert describe_board(jira) == "board: Jira project AIR at https://acme.atlassian.net (story.board: jira)"


def test_story_problems_name_missing_roles(tmp_path: Path) -> None:
    problems = story_problems(cfg_for(tmp_path))  # the studio roles, but no product or architect
    assert any("story.product_role" in p for p in problems)
    assert any("story.architect_role" in p for p in problems)
    assert story_problems(cfg_for(tmp_path, roles=STORY_ROLES)) == []
    on = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"tester_role": "qa"}})
    assert story_problems(on) == []  # qa is a specialist in these fixtures
    missing = story_problems(cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"tester_role": "nobody"}}))
    assert len(missing) == 1 and "story.tester_role names 'nobody'" in missing[0]


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


# ---- where a story is built: a written request's repository, or the checkout an issue's label names ----

LABELLED = Card("AIR-7", "10007", "AIR-7", "Avatar upload", "", labels=("repo:o/repo",))


def test_a_written_request_is_built_in_repo_else_story_repo_resolved_once(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(repo)
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"repo": str(link)}})
    request = text_card("Let users upload an avatar")
    # StoryState.open compares the repository's spelling verbatim: one spelling goes to every caller
    assert place(cfg, request) == Place(repo.resolve(), None)
    assert place(cfg, request, link) == Place(repo.resolve(), None)
    with pytest.raises(StorySetupError, match="is not a git repository$"):
        place(cfg, request, tmp_path)
    cfg.story.repo = None
    with pytest.raises(StorySetupError, match="^no repository: pass --repo or set story.repo"):
        place(cfg, request)


def test_an_issue_is_built_in_the_checkout_its_label_names_never_in_story_repo(tmp_path: Path) -> None:
    repo = checkout_of(tmp_path / "git", "o/Repo")
    elsewhere = repo_with_marker_tests(tmp_path)  # story.repo is a written request's, not an issue's
    story = {"repos_root": str(tmp_path / "git"), "repo": str(elsewhere)}
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": story})
    # the label's spelling, the directory's own
    assert place(cfg, LABELLED) == Place(repo.resolve(), "o/repo")


def test_repo_replaces_the_lookup_but_the_remote_must_still_name_the_labels_repository(
    tmp_path: Path,
) -> None:
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"repos_root": str(tmp_path / "git")}})
    mine = checkout_of(tmp_path / "work", "o/repo")  # a clone of its own, outside story.repos_root
    assert place(cfg, LABELLED, mine) == Place(mine.resolve(), "o/repo")
    other = checkout_of(tmp_path / "other", "o/other")
    with pytest.raises(RepositoryError, match="is not a checkout of o/repo: its remote 'origin'"):
        place(cfg, LABELLED, other)
    # the person's error, in the command: not the issue's
    with pytest.raises(StorySetupError, match="is not a git repository$") as e:
        place(cfg, LABELLED, tmp_path / "nowhere")
    assert not isinstance(e.value, RepositoryError)


def test_a_label_changed_since_the_story_started_says_from_what_to_what(tmp_path: Path) -> None:
    repo = checkout_of(tmp_path / "git", "o/repo")
    checkout_of(tmp_path / "git", "o/other")
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"repos_root": str(tmp_path / "git")}})
    StoryState.open(stories_dir(cfg) / "AIR-7", LABELLED, repo.resolve(), github_repo="o/repo")
    state = stories_dir(cfg) / "AIR-7" / "state.json"
    for labels, now in (
        (("repo:o/other",), "repo:o/other"),
        ((), "none"),
        (("repo:o", "repo:o/x/y"), "'repo:o', 'repo:o/x/y'"),
    ):
        with pytest.raises(RepositoryError) as e:
            place(cfg, replace(LABELLED, labels=labels))
        assert str(e.value) == (
            f"the issue's repo: label changed after its story started, from repo:o/repo to {now}: put "
            f"repo:o/repo back, or delete {state} to start the story over"
        )
    # the same repository in another case is the same repository
    assert place(cfg, replace(LABELLED, labels=("REPO:O/Repo",))) == Place(repo.resolve(), "O/Repo")


async def test_a_story_whose_repository_cannot_be_used_tells_the_issue_and_raises(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"repos_root": str(tmp_path / "git")}})
    unlabelled = replace(LABELLED, labels=())
    board = QueueBoard(unlabelled)
    with pytest.raises(RepositoryError, match="the issue names no repository"):
        await run_one(cfg, board=board, card=unlabelled)
    assert board.column == {"AIR-7": "blocked"}  # out of intake
    [text] = board.notes["AIR-7"]
    assert text.startswith(
        f"{NOTE_PREFIX}cannot be picked up: the issue names no repository: add the label repo:<owner>/<name>"
    )
    assert not (stories_dir(cfg) / "AIR-7").exists()  # no story was started
    with story_lock(stories_dir(cfg)) as held:
        assert held


async def test_run_one_refuses_while_another_story_holds_the_lock(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, roles=STORY_ROLES)
    with story_lock(stories_dir(cfg)) as held:
        assert held
        with pytest.raises(StoryBusy):
            await run_one(cfg, board=NullBoard(stories_dir(cfg)), card=text_card("x"), repo=tmp_path)


async def test_run_one_builds_the_story_with_the_pipeline_and_lets_go_of_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    cfg = cfg_for(
        tmp_path, roles=STORY_ROLES, extra={"story": {"trunk": git(repo, "branch", "--show-current")}}
    )
    gh = RecordedGh()
    monkeypatch.setattr(factory, "_engine", lambda cfg: happy_engine())
    monkeypatch.setattr(
        factory, "Publisher", lambda *, remote, app: Publisher(remote=remote, run=gh, app=app)
    )
    card = text_card("Let users upload an avatar")
    st = await run_one(cfg, board=NullBoard(stories_dir(cfg)), card=card, repo=repo, test_cmd="./check.sh")
    assert st.gate is None  # a person's command: AGENTS.md is not read
    assert st.stage == "review" and st.blocked is None and [p["number"] for p in st.prs] == [11, 12]
    assert st.path == stories_dir(cfg) / card.key and (st.path / "state.json").is_file()
    assert ["gh", "stack", "submit", "--auto", "--remote", "origin"] in gh.calls
    with story_lock(stories_dir(cfg)) as held:
        assert held  # the lock was let go when the story ended


def test_the_pipeline_is_wired_to_the_story_remote_and_the_engine_of_the_build_role(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"remote": "upstream"}})
    pipeline = make_pipeline(
        cfg, board=NullBoard(stories_dir(cfg)), repo=tmp_path, test_cmd="true", github_repo="o/r"
    )
    assert pipeline.publisher.remote == "upstream" and pipeline.test_cmd == "true"
    assert pipeline.repo == tmp_path.resolve() and pipeline.github_repo == "o/r"
    assert make_pipeline(cfg, board=NullBoard(stories_dir(cfg)), repo=tmp_path).person_cmd is None
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
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"board": "jira"}})
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

    async def comment(self, card: Card, text: str, role: str | None = None) -> None: ...


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


TOP = Card("AIR-7", "10007", "AIR-7", "Avatar upload", "", labels=("repo:o/repo",))


def github_answers(
    monkeypatch: pytest.MonkeyPatch,
    answer: Callable[[httpx.Request], httpx.Response] = lambda request: httpx.Response(200, json={}),
) -> list[httpx.Request]:
    """GitHub's API as a tick that publishes as you pings it (github_app.ping), answering with `answer` (200
    by default); returns the requests it got. A tick that publishes as the bot asks its app instead."""
    asked: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return answer(request)

    async def ping(api_url: str) -> None:
        await github_app_module.ping(api_url, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(factory, "ping", ping)
    return asked


def tick_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, board: Any, engine: Engine
) -> tuple[Any, Path]:
    """A tick over `board`, whose issues are for o/repo: its checkout is in story.repos_root, and GitHub
    answers. Neither story.repo nor a test command is set: an issue's repository is its label, and its gate
    is AGENTS.md's."""
    repo = checkout_of(tmp_path / "git", "o/repo")
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"repos_root": str(tmp_path / "git")}})
    monkeypatch.setattr(factory, "make_board", lambda cfg: board)
    monkeypatch.setattr(factory, "_engine", lambda cfg: engine)
    github_answers(monkeypatch)
    return cfg, repo


async def test_a_tick_without_a_board_has_nothing_to_pick_up(tmp_path: Path) -> None:
    # story.board: jira without Jira settings, and no repository either: neither is needed
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"board": "jira"}})
    assert "no board configured" in await tick(cfg)


async def test_a_tick_builds_the_top_issue_and_reports_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = Intake(TOP)
    cfg, repo = tick_setup(tmp_path, monkeypatch, board, Engine(True))
    built: list[tuple[Any, ...]] = []

    class Built:
        def __init__(self, *args: Any) -> None:
            self.args = args

        async def run(self, card: Card) -> StoryState:
            built.append((card, *self.args))
            return StoryState(path=tmp_path / "s", card=card.to_dict(), repo=str(repo), stage="review")

    def made(cfg: Any, *, board: Any, repo: Path, test_cmd: str | None, github_repo: str | None) -> Built:
        return Built(board, repo, test_cmd, github_repo)

    monkeypatch.setattr(factory, "make_pipeline", made)
    assert await tick(cfg) == "AIR-7 Avatar upload: review"
    assert built == [(TOP, board, repo.resolve(), None, "o/repo")]  # the label's repository, AGENTS.md's gate
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

    monkeypatch.setattr(factory, "make_pipeline", lambda cfg, **kw: Blocked())
    line = await tick(cfg)
    assert line == "AIR-7 Avatar upload: blocked (layer 01: `./check.sh` fails on b1; its worktree is kept)"
    assert "\n" not in line


class Rows:
    """A LocalBoard over a real store, whose next_card is `pick` (a story's key) whatever its column."""

    def __init__(self, tmp_path: Path, pick: str) -> None:
        self.store = BoardStore(tmp_path / "board.sqlite")
        self.store.create(level="story", title="todo one", labels=["repo:o/repo"])
        self.store.create(level="story", title="approved two", labels=["repo:o/repo"])
        self.store.move("AIR-2", "approved")
        self.pick = pick
        self.board = LocalBoard(self.store)

        async def chosen() -> Card:
            return await self.board.card(self.pick)

        self.board.next_card = chosen  # type: ignore[method-assign]


def started_log(monkeypatch: pytest.MonkeyPatch) -> list[Card]:
    started: list[Card] = []

    class Built:
        async def run(self, card: Card) -> StoryState:
            started.append(card)
            return StoryState(path=Path("s"), card=card.to_dict(), repo="r", stage="review")

    monkeypatch.setattr(factory, "make_pipeline", lambda cfg, **kw: Built())
    return started


async def test_a_tick_over_the_local_board_says_the_column_it_took_the_story_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = Rows(tmp_path, "AIR-2")
    cfg, _ = tick_setup(tmp_path, monkeypatch, rows.board, Engine(True))
    started = started_log(monkeypatch)
    assert await tick(cfg) == "AIR-2 approved two: review (taken from approved)"
    assert [c.key for c in started] == ["AIR-2"]


async def test_a_tick_refuses_a_story_that_is_not_approved_and_moves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = Rows(tmp_path, "AIR-1")  # a board that offers a story waiting for approval
    cfg, _ = tick_setup(tmp_path, monkeypatch, rows.board, Engine(True))
    started = started_log(monkeypatch)
    assert await tick(cfg) == "refused: AIR-1 is todo, not approved (only approved stories are built)"
    assert started == [] and rows.store.get("AIR-1").status == "todo"


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
    asked = github_answers(monkeypatch)
    assert (await tick(cfg)).startswith("engine down: start it with")
    assert board.asked == 0  # the issue stays where it is
    assert asked == []  # the engine is named first; GitHub is not asked


async def test_a_tick_does_nothing_while_a_story_is_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board, engine = Intake(TOP), Engine(True)
    cfg, _ = tick_setup(tmp_path, monkeypatch, board, engine)
    asked = github_answers(monkeypatch)
    with story_lock(stories_dir(cfg)) as held:
        assert held
        assert await tick(cfg) == "busy: a story is in flight"
    assert (board.asked, engine.probed, asked) == (0, 0, [])


def unavailable(request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, text="unicorn")


def no_reply(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def no_story(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tick that takes an issue fails the test right there, before its story (and any git or gh, which
    would go to the checkout's github.com remote) can run."""

    def made(cfg: Any, **kw: Any) -> Any:
        raise AssertionError("the tick took an issue and started its story")

    monkeypatch.setattr(factory, "make_pipeline", made)


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        (unavailable, "GitHub GET rate_limit: 503 Service Unavailable"),
        (no_reply, "GitHub GET rate_limit: ConnectError, no reply"),
    ],
    ids=["503", "no-reply"],
)
async def test_as_you_a_tick_takes_no_issue_while_github_does_not_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: Callable[[httpx.Request], httpx.Response],
    said: str,
) -> None:
    """Every issue a tick took now would stop at the trunk fetch and go to blocked: the queue waits instead,
    as it does for the engine, and the next beat asks again."""
    board = Intake(TOP)
    cfg, _ = tick_setup(tmp_path, monkeypatch, board, Engine(True))
    no_story(monkeypatch)
    asked = github_answers(monkeypatch, answer)
    assert await tick(cfg) == f"github down: {said}"
    assert board.asked == 0 and len(asked) == 1  # the issue stays in To Do, untouched
    asked = github_answers(monkeypatch)  # GitHub is back: the next beat asks the board again
    board.top = None
    assert await tick(cfg) == "idle: no issue matches the intake query"
    assert board.asked == 1 and len(asked) == 1


def app_down(github: FakeGitHub, says: str) -> httpx.MockTransport:
    """`github`, but GET /app (the app asking who it is) gets a 503 with `says`."""

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/app":
            github.requests.append(request)
            return httpx.Response(503, json={"message": says})
        return github(request)

    return httpx.MockTransport(answer)


@pytest.mark.parametrize("why", ["github 503", "no key", "another key"])
async def test_as_the_bot_a_tick_takes_no_issue_while_the_app_cannot_reach_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, why: str
) -> None:
    """GitHubApp.slug is the check: GitHub must answer the app's JWT, so its key must be there and be the
    app's. Each would stop every issue at the bot's identity (`stopped after 'new'`), so none is taken."""
    board, github = Intake(TOP), FakeGitHub()
    cfg, _ = tick_setup(tmp_path, monkeypatch, board, Engine(True))
    no_story(monkeypatch)
    pinged = github_answers(monkeypatch)
    if why == "github 503":
        app = GitHubApp(app_spec(tmp_path), transport=app_down(github, "down\nfor maintenance"))
        said = "GitHub GET app: 503 down for maintenance"  # one line in the log, whatever GitHub says
    elif why == "no key":
        app = fake_app(tmp_path, github, key_file=tmp_path / "nowhere.pem")
        said = (
            f"story.github_app.key_file {tmp_path / 'nowhere.pem'} cannot be read (No such file or "
            "directory): put the app's private key there"
        )
    else:
        other = key_file(tmp_path, "other")
        app = fake_app(tmp_path, github, key_file=other)
        said = "GitHub refused the app's JWT (401 A JSON web token could not be decoded): check that "
    monkeypatch.setattr(factory, "github_app", lambda cfg: app)
    line = await tick(cfg)
    assert line.startswith(f"github down: {said}") and "\n" not in line
    assert board.asked == 0 and pinged == []  # as the bot, the app is the check
    assert github.tokens == []  # nothing was minted


async def test_as_the_bot_a_tick_whose_app_github_answers_takes_the_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board, github = Intake(None), FakeGitHub()
    cfg, _ = tick_setup(tmp_path, monkeypatch, board, Engine(True))
    pinged = github_answers(monkeypatch)
    monkeypatch.setattr(factory, "github_app", lambda cfg: fake_app(tmp_path, github))
    assert await tick(cfg) == "idle: no issue matches the intake query"
    assert board.asked == 1 and pinged == [] and github.asked("GET") == ["/api/app"]


# ---- a poison issue: a story that cannot be opened must not hold up the issues behind it ----


class QueueBoard:
    """A board with an intake query: its issues in rank order, each in a column. Moves and notes are kept; a
    call named in `refuse` raises BoardError, as Jira does when it is down."""

    def __init__(
        self,
        *cards: Card,
        refuse: tuple[str, ...] = (),
        says: str = "500 Internal Server Error",
        off: tuple[str, ...] = (),
    ) -> None:
        self.cards = {c.key: c for c in cards}
        self.column: dict[str, str] = {c.key: "intake" for c in cards}
        self.notes: dict[str, list[str]] = {c.key: [] for c in cards}
        self.refuse = refuse
        self.says = says  # what a refused call is told
        # switched-off columns (statuses.blocked: null): a move there leaves the issue where it is, as Jira's
        self.off = off
        self.searches = 0

    async def card(self, ref: str) -> Card:
        return self.cards[ref]

    async def next_card(self) -> Card | None:
        self.searches += 1
        return next((c for c in self.cards.values() if self.column[c.key] == "intake"), None)

    async def move(self, card: Card, column: Column) -> None:
        if "move" in self.refuse:
            raise BoardError(f"Jira POST issue/{card.key}/transitions: {self.says}")
        if column not in self.off:
            self.column[card.key] = column

    async def comment(self, card: Card, text: str, role: str | None = None) -> None:
        if "comment" in self.refuse:
            raise BoardError(f"Jira POST issue/{card.key}/comment: {self.says}")
        self.notes[card.key].append(text)


POISON = Card("AIR-7", "10007", "AIR-7", "Avatar upload", "", labels=("repo:o/poison",))
NEXT = Card("AIR-8", "10008", "AIR-8", "Avatar upload", "Users want avatars", labels=("repo:o/repo",))


def queue_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, board: QueueBoard, *, blocked: str | None = "Blocked"
) -> tuple[Any, RecordedGh]:
    """A tick over `board`, in which every story builds on a fresh FakeEngine with a recorded gh, and GitHub
    answers. AIR-7 is for o/poison and AIR-8 for o/repo, each with its checkout in story.repos_root (tmp/git)
    and an AGENTS.md whose gate is ./check.sh. Their remotes are GitHub's, so the recorded gh fetches
    nothing."""
    root = tmp_path / "git"
    repo = checkout_of(root, "o/repo")
    checkout_of(root, "o/poison")
    story = {
        "repos_root": str(root),
        "trunk": git(repo, "branch", "--show-current"),
        "jira": {"statuses": {"blocked": blocked}},
    }
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": story})
    gh = RecordedGh()
    monkeypatch.setattr(factory, "make_board", lambda cfg: board)
    monkeypatch.setattr(factory, "_engine", lambda cfg: happy_engine())
    monkeypatch.setattr(
        factory, "Publisher", lambda *, remote, app: Publisher(remote=remote, run=gh, app=app)
    )
    github_answers(monkeypatch)
    return cfg, gh


def poison_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    board: QueueBoard,
    *,
    how: str = "damaged",
    blocked: str | None = "Blocked",
) -> tuple[Any, RecordedGh]:
    """queue_setup, where the top issue, AIR-7, has a story state that cannot be opened (`how`: a damaged
    state.json, one that cannot be read, an event log that cannot be appended to, one whose pinned repository
    is a number, a checkout whose remote no longer names the issue's repository, or a story started in another
    repository)."""
    cfg, gh = queue_setup(tmp_path, monkeypatch, board, blocked=blocked)
    state = stories_dir(cfg) / POISON.key
    poison = (cfg.story.repos_root / "poison").resolve()
    if how == "damaged":
        state.mkdir(parents=True)
        (state / "state.json").write_text("{ not json")
    elif how == "unreadable":  # a good state file that airlie may not read
        StoryState.open(state, POISON, poison)
        (state / "state.json").chmod(0)
    elif how == "read-only events":  # a good event log that no event can be appended to
        StoryState.open(state, POISON, poison).finish("intake")
        (state / "events.jsonl").chmod(0o444)
    elif how == "pin not text":  # the repository it is pinned to is a number: damaged, not a crash
        StoryState.open(state, POISON, poison, github_repo="o/poison").finish("intake")
        saved = json.loads((state / "state.json").read_text())
        (state / "state.json").write_text(json.dumps({**saved, "github_repo": 7}))
    elif how == "moved remote":  # started for o/poison; its checkout's remote names another repository now
        StoryState.open(state, POISON, poison, github_repo="o/poison").finish("intake")
        git(poison, "remote", "set-url", "origin", "git@github.com:evil/other.git")
    else:  # a story that was started in another repository, then stopped
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        StoryState.open(state, POISON, elsewhere)
    return cfg, gh


@pytest.mark.parametrize(
    ("how", "blocked", "column", "says"),
    [
        ("damaged", "Blocked", "blocked", "/AIR-7/state.json is damaged"),
        ("unreadable", "Blocked", "blocked", "/AIR-7/state.json cannot be read (Permission denied)"),
        ("read-only events", "Blocked", "blocked", "/AIR-7/events.jsonl cannot be read and appended to"),
        ("pin not text", "Blocked", "blocked", "/AIR-7/state.json is damaged (its github_repo is not text)"),
        ("moved remote", "Blocked", "blocked", "/poison is not a checkout of o/poison: its remote 'origin'"),
        ("elsewhere", None, "building", "started in"),
    ],
)
async def test_a_poison_issue_is_set_aside_and_the_next_tick_builds_the_issue_behind_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str, blocked: str | None, column: str, says: str
) -> None:
    board = QueueBoard(POISON, NEXT)
    cfg, gh = poison_setup(tmp_path, monkeypatch, board, how=how, blocked=blocked)

    line = await tick(cfg)  # the top issue's story cannot be opened
    assert line.startswith("AIR-7 Avatar upload: cannot start (") and says in line and "\n" not in line
    assert board.column == {"AIR-7": column, "AIR-8": "intake"}  # out of the intake query; AIR-8 untouched
    [text] = board.notes["AIR-7"]
    assert text.startswith(NOTE_PREFIX) and says in text
    assert "Needs a person" in text and "`task story -- AIR-7`" in text
    assert ("story.jira.statuses.blocked is off" in text) == (blocked is None)
    assert gh.calls == []
    if how == "damaged":
        assert (stories_dir(cfg) / "AIR-7" / "state.json").read_text() == "{ not json"  # left for the person

    assert await tick(cfg) == "AIR-8 Avatar upload: review"  # the next tick takes the issue behind it
    assert board.column == {"AIR-7": column, "AIR-8": "review"}
    assert await tick(cfg) == "idle: no issue matches the intake query"
    assert board.searches == 3 and board.notes["AIR-7"] == [text]  # AIR-7 was not looked at again


@pytest.mark.parametrize(
    ("labels", "says"),
    [
        ((), "the issue names no repository: add the label repo:<owner>/<name> (repo:weyenk/OpenHand, say)"),
        (
            ("repo:o/poison", "Repo:o/repo"),
            "the issue has 2 repo: labels ('repo:o/poison', 'Repo:o/repo'); keep",
        ),
        (("repo:o",), "the label 'repo:o' is not repo:<owner>/<name>"),
        (("repo:o/missing",), "no checkout of o/missing in "),
        (("repo:o/twin",), "(story.repos_root) is named twin in some case: "),
        (("repo:o/plain",), "/plain is not a git repository: clone o/plain there"),
        (("repo:o/bare",), "/bare has no remote 'origin' (story.remote): add it with git -C "),
        (
            ("repo:o/stray",),
            "/stray is not a checkout of o/stray: its remote 'origin' (story.remote) is 'git@",
        ),
    ],
)
async def test_an_issue_whose_repository_cannot_be_used_is_told_why_and_moved_out_of_intake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, labels: tuple[str, ...], says: str
) -> None:
    board = QueueBoard(replace(POISON, labels=labels), NEXT)
    cfg, gh = queue_setup(tmp_path, monkeypatch, board)
    root = cfg.story.repos_root
    (root / "plain").mkdir()  # a directory, not a clone
    git(root, "init", "-q", "bare")  # a clone with no remote
    stray = checkout_of(root, "o/stray")
    git(stray, "remote", "set-url", "origin", "git@github.com:o/repo.git")  # a clone of another repository
    for name in ("twin", "TWIN"):  # one directory where names are case-insensitive (APFS), else two
        (root / name).mkdir(exist_ok=True)
    real = Path.iterdir

    def listing(self: Path) -> Iterator[Path]:  # story.repos_root as a case-sensitive volume lists it
        found = {p.name: p for p in real(self)}
        if self == root:
            found.setdefault("TWIN", self / "TWIN")
        yield from found.values()

    monkeypatch.setattr(Path, "iterdir", listing)

    line = await tick(cfg)
    assert line.startswith("AIR-7 Avatar upload: cannot start (") and says in line and "\n" not in line
    assert board.column == {"AIR-7": "blocked", "AIR-8": "intake"}
    [text] = board.notes["AIR-7"]
    assert text.startswith(f"{NOTE_PREFIX}cannot be picked up: ") and says in text
    assert "Needs a person: fix that, then resume with `task story -- AIR-7`." in text
    assert not (stories_dir(cfg) / "AIR-7").exists() and gh.calls == []  # nothing was started
    assert await tick(cfg) == "AIR-8 Avatar upload: review"  # the queue moves on


async def test_an_issue_whose_checkout_cannot_be_read_is_set_aside_not_a_beat_that_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = QueueBoard(POISON, NEXT)
    cfg, _ = queue_setup(tmp_path, monkeypatch, board)
    poison = cfg.story.repos_root / "poison"
    poison.chmod(0)
    try:
        line = await tick(cfg)  # no PermissionError out of the beat: every later beat would stop on it
    finally:
        poison.chmod(0o755)
    assert line.startswith("AIR-7 Avatar upload: cannot start (") and "/poison cannot be read (" in line
    assert board.column == {"AIR-7": "blocked", "AIR-8": "intake"}
    assert await tick(cfg) == "AIR-8 Avatar upload: review"


async def test_with_blocked_off_an_issue_without_a_repository_still_leaves_intake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = QueueBoard(replace(POISON, labels=()), NEXT, off=("blocked",))
    cfg, _ = queue_setup(tmp_path, monkeypatch, board, blocked=None)
    line = await tick(cfg)
    assert "cannot start (the issue names no repository" in line and line.endswith("moved to building")
    assert board.column == {"AIR-7": "building", "AIR-8": "intake"}  # out of the intake query all the same
    assert "story.jira.statuses.blocked is off" in board.notes["AIR-7"][0]
    assert await tick(cfg) == "AIR-8 Avatar upload: review"


@pytest.mark.parametrize("blocked", ["Blocked", None])
async def test_an_issue_whose_repository_has_no_test_gate_stops_out_of_intake_before_any_model_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked: str | None
) -> None:
    board = QueueBoard(POISON, NEXT, off=() if blocked else ("blocked",))
    cfg, _ = queue_setup(tmp_path, monkeypatch, board, blocked=blocked)
    engine = happy_engine()
    monkeypatch.setattr(factory, "_engine", lambda cfg: engine)
    poison = cfg.story.repos_root / "poison"
    commit_file(poison, "AGENTS.md", "# Agents\n\nNo gate yet.\n", "the gate goes")
    trunk = git(poison, "branch", "--show-current")

    line = await tick(cfg)
    assert line.startswith(f"AIR-7 Avatar upload: blocked (o/poison has no test gate on origin/{trunk} (")
    assert "its AGENTS.md has no Test gate section" in line and engine.requests == []
    assert board.column == {"AIR-7": "blocked" if blocked else "building", "AIR-8": "intake"}
    stop = board.notes["AIR-7"][-1]
    on = f"o/poison has no test gate on origin/{trunk} ("
    assert stop.startswith(f"{NOTE_PREFIX}stopped after 'intake': {on}")
    assert "Add a `## Test gate` section to AGENTS.md at the repository root" in stop
    assert await tick(cfg) == "AIR-8 Avatar upload: review"


async def test_the_label_pins_the_bots_repository_and_a_label_changed_since_is_set_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = QueueBoard(NEXT)  # AIR-8, for o/repo
    cfg, _ = queue_setup(tmp_path, monkeypatch, board)
    github = FakeGitHub(installed=("o/repo", "o/poison"))  # the app could publish to either
    monkeypatch.setattr(factory, "github_app", lambda cfg: fake_app(tmp_path, github))
    stops = FakeEngine({"product": [fenced({"type": "blocked", "reason": "which sizes?"})]})
    monkeypatch.setattr(factory, "_engine", lambda cfg: stops)

    line = await tick(cfg)  # the story starts for o/repo, and stops at refine
    assert (
        line.startswith("AIR-8 Avatar upload: blocked (product says") and board.column["AIR-8"] == "blocked"
    )
    state = stories_dir(cfg) / "AIR-8" / "state.json"
    assert json.loads(state.read_text())["github_repo"] == "o/repo"
    assert github.tokens and github.minted_for() == [["repo"]] * len(github.tokens)

    # a person relabels the issue for another repository and puts it back in To Do
    board.cards["AIR-8"] = replace(NEXT, labels=("repo:o/poison",))
    board.column["AIR-8"] = "intake"
    minted = len(github.tokens)
    line = await tick(cfg)
    says = "the issue's repo: label changed after its story started, from repo:o/repo to repo:o/poison"
    assert line.startswith(f"AIR-8 Avatar upload: cannot start ({says}: put repo:o/repo back, or delete ")
    assert board.column["AIR-8"] == "blocked" and says in board.notes["AIR-8"][-1]
    assert len(github.tokens) == minted  # nothing was minted for the other repository


@pytest.mark.parametrize("refuse", [("comment",), ("move",), ("comment", "move")])
async def test_a_board_that_refuses_the_note_or_the_move_is_named_in_the_line_and_does_not_fail_the_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refuse: tuple[str, ...]
) -> None:
    board = QueueBoard(POISON, NEXT, refuse=refuse)
    cfg, _ = poison_setup(tmp_path, monkeypatch, board)
    line = await tick(cfg)  # no exception: a failing board is no second failure
    assert line.startswith("AIR-7 Avatar upload: cannot start (")
    note_failed = "the note failed (BoardError: Jira POST issue/AIR-7/comment: 500"
    move_failed = "the move to blocked failed (BoardError: Jira POST issue/AIR-7/transitions: 500"
    assert (note_failed in line) == ("comment" in refuse) and (move_failed in line) == ("move" in refuse)
    # each call is made whatever the other did
    assert (board.notes["AIR-7"] != []) == ("comment" not in refuse)
    assert (board.column["AIR-7"] == "blocked") == ("move" not in refuse)


async def test_what_a_refusing_board_said_is_masked_and_cut_in_the_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a cut keeps the beginning of a text and its end: the password is in the end
    said = "500 " + "y" * 300 + " https://me:s3cret@acme.atlassian.net/x"
    board = QueueBoard(POISON, NEXT, refuse=("comment",), says=said)
    cfg, _ = poison_setup(tmp_path, monkeypatch, board)
    line = await tick(cfg)
    assert "s3cret" not in line and "https://***@acme.atlassian.net" in line
    assert "…" in line and len(line) < 1000


async def test_the_reason_posted_for_a_poison_issue_is_masked_and_cut_like_a_stop_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = QueueBoard(POISON, NEXT)
    cfg, _ = poison_setup(tmp_path, monkeypatch, board, how="elsewhere")
    state_file = stories_dir(cfg) / POISON.key / "state.json"
    data = json.loads(state_file.read_text())
    data["repo"] = "https://me:s3cret@git.example.org/r.git/" + "x" * 3000  # where it was started
    state_file.write_text(json.dumps(data))
    line = await tick(cfg)
    [text] = board.notes["AIR-7"]
    for shown in (line, text):
        assert "s3cret" not in shown and "https://***@git.example.org" in shown
        assert "…" in shown and len(shown) < 1500  # cut: the reason alone could be 3,000 characters


async def test_only_a_story_that_cannot_be_opened_is_set_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = QueueBoard(POISON, NEXT)
    cfg, _ = poison_setup(tmp_path, monkeypatch, board)

    class Broken:
        async def run(self, card: Card) -> StoryState:
            raise RuntimeError("a bug in the pipeline")

    monkeypatch.setattr(factory, "make_pipeline", lambda cfg, **kw: Broken())
    with pytest.raises(RuntimeError, match="a bug in the pipeline"):  # a bug is not hidden as a poison issue
        await tick(cfg)
    assert board.column["AIR-7"] == "intake" and board.notes["AIR-7"] == []


# ---- a finished story whose issue is back in To Do must not hold up the issues behind it either ----


async def test_an_issue_put_back_in_to_do_after_its_story_finished_goes_to_review_and_the_next_is_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = QueueBoard(POISON, NEXT)  # AIR-7 is the issue whose story is built first
    cfg, gh = queue_setup(tmp_path, monkeypatch, board)

    assert await tick(cfg) == "AIR-7 Avatar upload: review"  # built; the pipeline moved the issue to review
    assert board.column == {"AIR-7": "review", "AIR-8": "intake"}
    told = list(board.notes["AIR-7"])
    assert not any("already in review" in text for text in told)  # a story that has just finished is not
    pushed = list(gh.calls)  # told it had finished before

    board.column["AIR-7"] = "intake"  # a person puts the issue back in To Do
    line = await tick(cfg)  # nothing is left to do for it, and nothing would move it
    assert line.startswith("AIR-7 Avatar upload: already in review (nothing to do); noted on the issue, ")
    assert line.endswith("moved to review") and "\n" not in line
    assert board.column == {"AIR-7": "review", "AIR-8": "intake"}
    assert len(board.notes["AIR-7"]) == len(told) + 1
    text = board.notes["AIR-7"][-1]
    pulls = "https://github.com/o/r/pull/11 https://github.com/o/r/pull/12"
    assert text.startswith(NOTE_PREFIX) and f"this story is already in review: {pulls}; airlie has" in text
    assert "nothing more to do" in text and "is not built again" in text
    assert str(stories_dir(cfg) / "AIR-7") in text  # where its state is
    assert gh.calls == pushed  # nothing was pushed or opened again

    assert await tick(cfg) == "AIR-8 Avatar upload: review"  # the next tick takes the issue behind it
    assert board.column == {"AIR-7": "review", "AIR-8": "review"}
    assert await tick(cfg) == "idle: no issue matches the intake query"
    assert board.searches == 4


@pytest.mark.parametrize("how", ["label removed", "label changed", "checkout moved"])
@pytest.mark.parametrize("stage", ["publish", "review"])
async def test_a_story_that_has_published_needs_neither_its_label_nor_its_checkout_any_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, how: str
) -> None:
    """Its PRs are open and nothing is built or published again: a person may tidy the label or move the
    clone, and the story still gets its final note (stage publish: the PR links never reached the issue) and
    In Review (stage review: the issue was put back in To Do). It runs in the checkout it recorded,
    unchecked."""
    board = QueueBoard(NEXT)  # AIR-8, for o/repo
    cfg, gh = queue_setup(tmp_path, monkeypatch, board)
    assert await tick(cfg) == "AIR-8 Avatar upload: review"
    state = stories_dir(cfg) / "AIR-8" / "state.json"
    if stage == "publish":
        saved = json.loads(state.read_text())
        state.write_text(json.dumps({**saved, "stage": "publish"}))
    if how == "checkout moved":
        (cfg.story.repos_root / "repo").rename(cfg.story.repos_root / "repo-moved")
    else:
        board.cards["AIR-8"] = replace(NEXT, labels=() if how == "label removed" else ("repo:o/poison",))
    board.column["AIR-8"] = "intake"
    calls = len(gh.calls)
    line = await tick(cfg)
    if stage == "publish":
        assert line == "AIR-8 Avatar upload: review"
        assert "stacked draft PRs, bottom layer first" in board.notes["AIR-8"][-1]
    else:
        assert line.startswith("AIR-8 Avatar upload: already in review (nothing to do)")
    assert board.column["AIR-8"] == "review" and len(gh.calls) == calls  # nothing fetched or published


@pytest.mark.parametrize("contents", ["[]", "{}", '"review"', "12"])
async def test_a_state_file_of_the_wrong_shape_is_a_poison_issue_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: str
) -> None:
    board = QueueBoard(POISON, NEXT)
    cfg, _ = queue_setup(tmp_path, monkeypatch, board)
    (stories_dir(cfg) / "AIR-7").mkdir(parents=True)
    (stories_dir(cfg) / "AIR-7" / "state.json").write_text(contents)  # JSON, but no story state
    line = await tick(cfg)  # the stage is read before the run: that read must not be what crashes the tick
    assert "cannot start" in line and "is damaged" in line
    assert board.column == {"AIR-7": "blocked", "AIR-8": "intake"}


@pytest.mark.parametrize("refuse", [("comment",), ("move",), ("comment", "move")])
async def test_a_board_that_refuses_the_note_or_the_move_does_not_fail_a_tick_on_a_finished_story(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refuse: tuple[str, ...]
) -> None:
    board = QueueBoard(POISON, NEXT)
    cfg, _ = queue_setup(tmp_path, monkeypatch, board)
    await tick(cfg)
    told = len(board.notes["AIR-7"])
    board.column["AIR-7"] = "intake"
    board.refuse = refuse  # the board goes down

    line = await tick(cfg)  # no exception
    assert line.startswith("AIR-7 Avatar upload: already in review (nothing to do); ")
    note_failed = "the note failed (BoardError: Jira POST issue/AIR-7/comment: 500"
    move_failed = "the move to review failed (BoardError: Jira POST issue/AIR-7/transitions: 500"
    assert (note_failed in line) == ("comment" in refuse) and (move_failed in line) == ("move" in refuse)
    # each call is made whatever the other did
    assert (len(board.notes["AIR-7"]) == told + 1) == ("comment" not in refuse)
    assert (board.column["AIR-7"] == "review") == ("move" not in refuse)


# ---- story_job: what POST /v1/airlie/stories starts ----


async def test_a_story_job_is_named_after_its_card_and_follows_its_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    cfg = cfg_for(tmp_path, roles=STORY_ROLES)
    ran: list[tuple[Card, Path | None, str | None]] = []
    prs = [{"number": 11, "branch": "b1", "url": "https://github.com/o/r/pull/11"}]

    async def fake_run_one(
        c: Any, *, board: Any, card: Card, repo: Path | None = None, test_cmd: str | None = None
    ) -> StoryState:
        ran.append((card, repo, test_cmd))
        return StoryState(
            path=stories_dir(c) / card.key, card=card.to_dict(), repo=str(repo), stage="review", prs=prs
        )

    monkeypatch.setattr(factory, "run_one", fake_run_one)
    request = "Let users upload an avatar"
    job_id, events, work = await story_job(cfg, card=None, text=request, repo=str(repo))
    key = text_card(request).key
    assert (job_id, events) == (f"story-{key}", stories_dir(cfg) / key / "events.jsonl")
    assert ran == []  # nothing is built until the job runs
    result = await work
    assert result == {"stage": "review", "blocked": None, "prs": prs, "dir": str(stories_dir(cfg) / key)}
    assert ran == [(text_card(request), repo, None)]  # no test command: the gate is AGENTS.md's


def test_a_story_job_takes_no_test_command() -> None:
    assert list(inspect.signature(story_job).parameters) == ["cfg", "card", "text", "repo", "running"]


async def test_a_story_job_with_an_issue_fetches_it_and_finds_its_checkout_before_any_job_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout_of(tmp_path / "git", "o/repo")  # what FakeJira's AIR-7 is labelled for
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"repos_root": str(tmp_path / "git")}})
    jira = FakeJira()
    monkeypatch.setattr(factory, "make_board", lambda cfg: jira_board_for(jira))
    job_id, events, work = await story_job(cfg, card="AIR-7", text=None, repo=None)
    work.close()  # not started here
    assert (job_id, events.parent.name) == ("story-AIR-7", "AIR-7")
    assert [r.method for r in jira.requests] == ["GET"]  # the issue, read; nothing told
    with pytest.raises(BoardError, match="not a Jira issue key"):
        await story_job(cfg, card="banana", text=None, repo=None)


async def test_a_story_job_for_an_issue_whose_repository_cannot_be_used_tells_the_issue_and_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"repos_root": str(tmp_path / "git")}})
    jira = FakeJira()  # its AIR-7 is labelled repo:o/repo, which has no checkout here
    monkeypatch.setattr(factory, "make_board", lambda cfg: jira_board_for(jira))
    with pytest.raises(RepositoryError, match="^no checkout of o/repo in "):
        await story_job(cfg, card="AIR-7", text=None, repo=None)
    posted = [r.url.path for r in jira.requests if r.method == "POST"]
    assert posted == ["/rest/api/3/issue/AIR-7/comment", "/rest/api/3/issue/AIR-7/transitions"]
    told = json.loads([r for r in jira.requests if r.url.path.endswith("/comment")][0].content)
    assert "cannot be picked up: no checkout of o/repo in" in json.dumps(told)


async def test_a_story_job_that_cannot_be_set_up_says_what_is_missing(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    with pytest.raises(StorySetupError, match="story.product_role.*; .*story.architect_role"):
        await story_job(cfg_for(tmp_path), card=None, text="x", repo=str(repo))
    cfg = cfg_for(tmp_path, roles=STORY_ROLES)
    with pytest.raises(StorySetupError, match="no repository"):
        await story_job(cfg, card=None, text="x", repo=None)


async def test_a_story_topology_that_does_not_load_is_a_setup_problem_not_a_crash(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"topology": "nope"}})
    with pytest.raises(StorySetupError, match="unknown topology 'nope'"):
        await story_job(cfg, card=None, text="x", repo=str(tmp_path))


# ---- check: everything `task story:check` reports ----


def fake_publisher(problems: list[str], seen: list[tuple[str, Path]], notes: list[str] | None = None) -> Any:
    class Fake:
        def __init__(self, *, remote: str = "origin", app: GitHubApp | None = None) -> None:
            self.remote = remote

        async def check(self, repo: Path) -> Checked:
            seen.append((self.remote, repo))
            return Checked(problems, notes or [])

    return Fake


async def test_check_without_a_repository_does_not_ask_gh_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, Path]] = []
    monkeypatch.setattr(factory, "Publisher", fake_publisher(["gh asked"], seen))
    cfg = cfg_for(tmp_path)  # no product or architect, no repository, no test command
    problems, notes = await check(cfg, None, None)
    assert [p for p in problems if "story.product_role" in p] and [
        p
        for p in problems
        if p.startswith("no repository to check: name an issue (task story:check -- AIR-7)")
    ]
    assert seen == [] and "gh asked" not in problems and notes == []


async def test_check_adds_what_publishing_and_jira_need(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    trunk = git(repo, "branch", "--show-current")
    seen: list[tuple[str, Path]] = []
    monkeypatch.setattr(factory, "Publisher", fake_publisher(["gh is not logged in"], seen))
    story = {"remote": "upstream", "trunk": trunk, "board": "jira"}
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": story})
    problems, notes = await check(cfg, repo, "./check.sh")
    assert problems == ["gh is not logged in"] and notes[-1] == "test gate: ./check.sh (--test-cmd)"
    assert seen == [("upstream", repo.resolve())]

    async def jira_check(self: JiraBoard) -> list[str]:
        return ["project AIR has no status 'Blocked' (story.jira.statuses.blocked)"]

    monkeypatch.setattr(JiraBoard, "check", jira_check)  # nothing reaches a Jira site
    monkeypatch.setenv("ATLASSIAN_BASE_URL", "https://acme.atlassian.net")
    monkeypatch.setenv("ATLASSIAN_PROJECT_KEY", "AIR")
    monkeypatch.setenv("ATLASSIAN_EMAIL", "me@example.org")
    monkeypatch.setenv("ATLASSIAN_API_KEY", "t0ken")
    problems, _ = await check(cfg, repo, "./check.sh")
    assert problems == [
        "gh is not logged in",
        "project AIR has no status 'Blocked' (story.jira.statuses.blocked)",
    ]


async def no_jira_problems(self: JiraBoard) -> list[str]:
    return []


async def test_check_with_an_issue_shows_its_repository_its_trunk_as_last_fetched_and_its_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = checkout_of(tmp_path / "git", "o/repo", "uv sync", "uv run pytest -q")
    trunk = git(repo, "branch", "--show-current")
    fetched = git(repo, "rev-parse", trunk)
    git(repo, "update-ref", f"refs/remotes/origin/{trunk}", fetched)  # what the last fetch brought
    commit_file(repo, "AGENTS.md", agents_md("make test"), "a local commit the remote has not seen")
    story = {"repos_root": str(tmp_path / "git"), "trunk": trunk}
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": story})
    jira = FakeJira()
    monkeypatch.setattr(factory, "make_board", lambda cfg: jira_board_for(jira))
    monkeypatch.setattr(JiraBoard, "check", no_jira_problems)
    seen: list[tuple[str, Path]] = []
    monkeypatch.setattr(factory, "Publisher", fake_publisher([], seen, ["publishing to o/repo as app[bot]"]))
    head = git(repo, "rev-parse", "HEAD")

    problems, notes = await check(cfg, None, None, "https://acme.atlassian.net/browse/AIR-7")
    assert problems == []
    assert notes == [
        f"AIR-7 is for o/repo (its repo: label): {repo.resolve()}, whose remote 'origin' is "
        "git@github.com:o/repo.git",
        f"trunk: origin/{trunk} at {fetched[:12]}, as last fetched (a story fetches it first)",
        f"test gate (AGENTS.md at origin/{trunk}): uv sync && uv run pytest -q",
        "publishing to o/repo as app[bot]",
    ]
    assert seen == [("origin", repo.resolve())]
    # nothing is written: the issue read, no note or move; no fetch; no story state
    assert [r.method for r in jira.requests] == ["GET"]
    assert git(repo, "rev-parse", f"origin/{trunk}") == fetched and git(repo, "rev-parse", "HEAD") == head
    assert not stories_dir(cfg).exists()
    # a person's test command wins, and --repo replaces the lookup (the remote must still be the label's)
    mine = checkout_of(tmp_path / "work", "o/repo")
    problems, notes = await check(cfg, mine, " make check ", "AIR-7")
    assert problems == [] and notes[0].startswith(f"AIR-7 is for o/repo (its repo: label): {mine.resolve()},")
    assert "test gate: make check (--test-cmd)" in notes


@pytest.mark.parametrize("kind", ["tag", "branch"])
async def test_check_reads_the_remote_trunk_by_its_full_name_whatever_was_made_under_its_short_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """A tag or a local branch named origin/<trunk>, which a model's shell can make from a layer's worktree,
    is what git reads first for that short name: check shows the remote-tracking branch, and its gate."""
    repo = checkout_of(tmp_path / "git", "o/repo", "make test")
    trunk = git(repo, "branch", "--show-current")
    fetched = git(repo, "rev-parse", f"refs/remotes/origin/{trunk}")
    layer = tmp_path / "layer"
    git(repo, "worktree", "add", "-q", "--detach", str(layer))
    commit_file(layer, "AGENTS.md", agents_md("true"), "a weaker gate")
    git(layer, kind, f"origin/{trunk}")  # at the weaker commit
    story = {"repos_root": str(tmp_path / "git"), "trunk": trunk}
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": story})
    monkeypatch.setattr(factory, "make_board", lambda cfg: jira_board_for(FakeJira()))
    monkeypatch.setattr(JiraBoard, "check", no_jira_problems)
    monkeypatch.setattr(factory, "Publisher", fake_publisher([], []))
    problems, notes = await check(cfg, None, None, "AIR-7")
    assert problems == []
    assert notes[1:] == [
        f"trunk: origin/{trunk} at {fetched[:12]}, as last fetched (a story fetches it first)",
        f"test gate (AGENTS.md at origin/{trunk}): make test",
    ]


async def test_check_with_an_issue_that_would_be_blocked_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "git"
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"repos_root": str(root)}})
    jira = FakeJira()
    monkeypatch.setattr(factory, "make_board", lambda cfg: jira_board_for(jira))
    monkeypatch.setattr(JiraBoard, "check", no_jira_problems)
    seen: list[tuple[str, Path]] = []
    monkeypatch.setattr(factory, "Publisher", fake_publisher([], seen))
    problems, notes = await check(cfg, None, None, "AIR-7")  # no checkout of o/repo yet
    assert problems == [
        f"AIR-7 would be blocked: no checkout of o/repo in {root} (story.repos_root): clone it there: "
        f"git clone git@github.com:o/repo.git {root / 'repo'}"
    ]
    assert notes == [] and seen == [] and [r.method for r in jira.requests] == ["GET"]

    repo = checkout_of(root, "o/repo")
    commit_file(repo, "AGENTS.md", "# Agents\n", "no gate")
    trunk = git(repo, "branch", "--show-current")
    git(repo, "update-ref", f"refs/remotes/origin/{trunk}", "HEAD")  # pushed, and fetched since
    cfg.story.trunk = trunk
    sha = git(repo, "rev-parse", trunk)
    problems, notes = await check(cfg, None, None, "AIR-7")
    no_section = missing_gate("o/repo", f"origin/{trunk}", sha, "its AGENTS.md has no Test gate section")
    assert problems == [f"AIR-7 would be blocked: {no_section}"]
    assert notes[1] == f"trunk: origin/{trunk} at {sha[:12]}, as last fetched (a story fetches it first)"
    assert not stories_dir(cfg).exists() and all(r.method == "GET" for r in jira.requests)


async def test_check_of_an_issue_whose_story_has_published_checks_no_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is built or published again: its repository, trunk and gate are not looked at (the checkout it
    recorded may have moved since), and check says so."""
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"repos_root": str(tmp_path / "git")}})
    gone = tmp_path / "git" / "repo"  # recorded, and moved away since
    st = StoryState(
        path=stories_dir(cfg) / "AIR-7", card={}, repo=str(gone), stage="review", github_repo="o/repo"
    )
    st.path.mkdir(parents=True)
    st.save()
    jira = FakeJira()
    monkeypatch.setattr(factory, "make_board", lambda cfg: jira_board_for(jira))
    monkeypatch.setattr(JiraBoard, "check", no_jira_problems)
    seen: list[tuple[str, Path]] = []
    monkeypatch.setattr(factory, "Publisher", fake_publisher([], seen))
    problems, notes = await check(cfg, None, None, "AIR-7")
    assert problems == [] and seen == []
    assert notes == [
        f"AIR-7's story has published (stage review, in {gone}): nothing is built or published again, so its "
        "repository and test gate are not checked"
    ]


async def test_check_with_an_issue_but_no_jira_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(factory, "Publisher", fake_publisher([], []))
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"board": "jira"}})
    problems, _ = await check(cfg, None, None, "AIR-7")
    assert len(problems) == 1 and "no Jira project is configured" in problems[0]


async def test_check_of_a_written_requests_repository_shows_its_trunk_and_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = checkout_of(tmp_path / "work", "o/repo", "make test")
    trunk = git(repo, "branch", "--show-current")
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"repo": str(repo), "trunk": trunk}})
    monkeypatch.setattr(factory, "Publisher", fake_publisher([], []))
    problems, notes = await check(cfg, None, None)
    assert problems == []
    sha = git(repo, "rev-parse", trunk)
    assert notes == [
        f"trunk: origin/{trunk} at {sha[:12]}, as last fetched (a story fetches it first)",
        f"test gate (AGENTS.md at origin/{trunk}): make test",
    ]
    # never fetched: the local trunk is no stand-in
    git(repo, "update-ref", "-d", f"refs/remotes/origin/{trunk}")
    problems, notes = await check(cfg, None, None)
    assert problems == [] and notes == [
        f"trunk: origin/{trunk} has not been fetched into {repo.resolve()}: a story fetches it first, and "
        "reads its test gate there"
    ]
    problems, notes = await check(cfg, None, " make check ")  # a person's command is shown all the same
    assert notes[-1] == "test gate: make check (--test-cmd)"


async def test_check_does_not_hand_git_a_remote_it_would_read_as_an_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    seen: list[tuple[str, Path]] = []
    monkeypatch.setattr(factory, "Publisher", fake_publisher(["asked"], seen))
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"remote": "--upload-pack=x"}})
    problems, _ = await check(cfg, repo, "./check.sh")
    assert len(problems) == 1 and "story.remote" in problems[0] and seen == []


async def test_check_reports_a_half_configured_jira_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    monkeypatch.setattr(factory, "Publisher", fake_publisher([], []))
    monkeypatch.setenv("ATLASSIAN_BASE_URL", "https://acme.atlassian.net")
    monkeypatch.setenv("ATLASSIAN_EMAIL", "me@example.org")
    monkeypatch.setenv("ATLASSIAN_API_KEY", "t0ken")
    story = {"trunk": git(repo, "branch", "--show-current"), "board": "jira"}
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": story})
    problems, _ = await check(cfg, repo, "./check.sh")
    assert len(problems) == 1 and "ATLASSIAN_PROJECT_KEY" in problems[0]


# ---- who publishes ----


def test_the_publisher_acts_as_the_app_only_once_its_app_id_is_set(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    cfg = cfg_for(tmp_path, roles=STORY_ROLES)
    assert (
        github_app(cfg) is None
        and make_pipeline(cfg, board=NullBoard(tmp_path), repo=repo).publisher.app is None
    )
    key = tmp_path / "app.pem"
    cfg = cfg_for(
        tmp_path,
        roles=STORY_ROLES,
        extra={"story": {"remote": "up", "github_app": {"app_id": 7, "key_file": str(key)}}},
    )
    app = github_app(cfg)
    assert isinstance(app, GitHubApp) and (app.app_id, app.key_file, app.api_url) == (
        7,
        key,
        "https://api.github.com",
    )
    publisher = make_pipeline(cfg, board=NullBoard(tmp_path), repo=repo).publisher
    assert isinstance(publisher.app, GitHubApp) and publisher.app.app_id == 7 and publisher.remote == "up"


async def test_check_passes_on_what_the_publisher_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    seen: list[tuple[str, Path]] = []
    notes = ["publishing to o/r as app[bot]"]
    monkeypatch.setattr(factory, "Publisher", fake_publisher([], seen, notes))
    cfg = cfg_for(
        tmp_path, roles=STORY_ROLES, extra={"story": {"trunk": git(repo, "branch", "--show-current")}}
    )
    problems, said = await check(cfg, repo, "./check.sh")
    assert problems == [] and said[-1:] == notes


async def test_a_written_request_may_name_its_repository_as_owner_slash_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "git"
    clone = checkout_of(root, "weyenk/airlie")
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"repos_root": str(root)}})
    ran: list[Path | None] = []

    async def fake_run_one(c: Any, *, board: Any, card: Card, repo: Path | None = None) -> StoryState:
        ran.append(repo)
        return StoryState(path=stories_dir(c) / card.key, card=card.to_dict(), repo=str(repo), stage="review")

    monkeypatch.setattr(factory, "run_one", fake_run_one)
    _, _, work = await story_job(cfg, card=None, text="avatars", repo="weyenk/airlie")
    await work
    assert ran == [clone]  # the checkout in story.repos_root, found by name as a repo: label's is


async def test_a_written_request_still_takes_a_checkout_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"repos_root": str(tmp_path / "git")}})
    assert factory.given_repo(cfg, str(repo)) == repo
    assert factory.given_repo(cfg, "~/code/app") == Path.home() / "code" / "app"
    assert factory.given_repo(cfg, None) is None


async def test_a_written_request_naming_a_repository_with_no_checkout_says_to_clone_it(
    tmp_path: Path,
) -> None:
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"repos_root": str(tmp_path / "git")}})
    with pytest.raises(RepositoryError, match="^no checkout of weyenk/airlie in .*clone it there"):
        await story_job(cfg, card=None, text="avatars", repo="weyenk/airlie")


async def test_an_owner_slash_name_never_means_a_directory_under_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "weyenk" / "airlie" / ".git").mkdir(parents=True)  # a decoy where the server runs
    monkeypatch.chdir(tmp_path)
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"repos_root": str(tmp_path / "git")}})
    with pytest.raises(RepositoryError, match="^no checkout of weyenk/airlie in "):
        factory.given_repo(cfg, "weyenk/airlie")
