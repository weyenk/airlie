"""Story wiring: which board, what a story needs, where flags win, the one-story lock."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from airlie.engine import EngineHealth
from airlie.story import factory
from airlie.story.board import NOTE_PREFIX, BoardError, Card, Column, NullBoard, text_card
from airlie.story.factory import (
    StoryBusy,
    card_for,
    check,
    github_app,
    make_board,
    make_pipeline,
    resolve,
    run_one,
    stories_dir,
    story_job,
    story_problems,
    tick,
)
from airlie.story.github_app import GitHubApp
from airlie.story.jira import JiraBoard
from airlie.story.model import StorySetupError
from airlie.story.publish import Checked, Publisher
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
    monkeypatch.setattr(
        factory, "Publisher", lambda *, remote, app: Publisher(remote=remote, run=gh, app=app)
    )
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


# ---- a poison issue: a story that cannot be opened must not hold up the issues behind it ----


class QueueBoard:
    """A board with an intake query: its issues in rank order, each in a column. Moves and notes are kept; a
    call named in `refuse` raises BoardError, as Jira does when it is down."""

    def __init__(
        self, *cards: Card, refuse: tuple[str, ...] = (), says: str = "500 Internal Server Error"
    ) -> None:
        self.cards = {c.key: c for c in cards}
        self.column: dict[str, str] = {c.key: "intake" for c in cards}
        self.notes: dict[str, list[str]] = {c.key: [] for c in cards}
        self.refuse = refuse
        self.says = says  # what a refused call is told
        self.searches = 0

    async def card(self, ref: str) -> Card:
        return self.cards[ref]

    async def next_card(self) -> Card | None:
        self.searches += 1
        return next((c for c in self.cards.values() if self.column[c.key] == "intake"), None)

    async def move(self, card: Card, column: Column) -> None:
        if "move" in self.refuse:
            raise BoardError(f"Jira POST issue/{card.key}/transitions: {self.says}")
        self.column[card.key] = column

    async def comment(self, card: Card, text: str) -> None:
        if "comment" in self.refuse:
            raise BoardError(f"Jira POST issue/{card.key}/comment: {self.says}")
        self.notes[card.key].append(text)


POISON = Card("AIR-7", "10007", "AIR-7", "Avatar upload", "")
NEXT = Card("AIR-8", "10008", "AIR-8", "Avatar upload", "Users want avatars")


def queue_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, board: QueueBoard, *, blocked: str | None = "Blocked"
) -> tuple[Any, RecordedGh]:
    """A tick over `board`, in which every story builds on a fresh FakeEngine with a recorded gh."""
    repo = repo_with_marker_tests(tmp_path)
    story = {
        "repo": str(repo),
        "test_cmd": "./check.sh",
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
    state.json, one that cannot be read, an event log that cannot be appended to, or a story started in
    another repository)."""
    cfg, gh = queue_setup(tmp_path, monkeypatch, board, blocked=blocked)
    state = stories_dir(cfg) / POISON.key
    if how == "damaged":
        state.mkdir(parents=True)
        (state / "state.json").write_text("{ not json")
    elif how == "unreadable":  # a good state file that airlie may not read
        StoryState.open(state, POISON, Path(cfg.story.repo).resolve())
        (state / "state.json").chmod(0)
    elif how == "read-only events":  # a good event log that no event can be appended to
        StoryState.open(state, POISON, Path(cfg.story.repo).resolve()).finish("intake")
        (state / "events.jsonl").chmod(0o444)
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

    monkeypatch.setattr(factory, "make_pipeline", lambda cfg, *, board, repo, test_cmd: Broken())
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
        p for p in problems if "no repository" in p
    ]
    assert seen == [] and "gh asked" not in problems and notes == []


async def test_check_adds_what_publishing_and_jira_need(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    seen: list[tuple[str, Path]] = []
    monkeypatch.setattr(factory, "Publisher", fake_publisher(["gh is not logged in"], seen))
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"remote": "upstream"}})
    assert await check(cfg, repo, "./check.sh") == (["gh is not logged in"], [])
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
    cfg = cfg_for(tmp_path, roles=STORY_ROLES)
    problems, _ = await check(cfg, repo, "./check.sh")
    assert len(problems) == 1 and "ATLASSIAN_PROJECT_KEY" in problems[0]


# ---- who publishes ----


def test_the_publisher_acts_as_the_app_only_once_its_app_id_is_set(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    cfg = cfg_for(tmp_path, roles=STORY_ROLES)
    assert (
        github_app(cfg) is None
        and make_pipeline(cfg, board=NullBoard(tmp_path), repo=repo, test_cmd="x").publisher.app is None
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
    publisher = make_pipeline(cfg, board=NullBoard(tmp_path), repo=repo, test_cmd="x").publisher
    assert isinstance(publisher.app, GitHubApp) and publisher.app.app_id == 7 and publisher.remote == "up"


async def test_check_passes_on_what_the_publisher_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    seen: list[tuple[str, Path]] = []
    notes = ["publishing to o/r as app[bot]"]
    monkeypatch.setattr(factory, "Publisher", fake_publisher([], seen, notes))
    assert await check(cfg_for(tmp_path, roles=STORY_ROLES), repo, "./check.sh") == ([], notes)
