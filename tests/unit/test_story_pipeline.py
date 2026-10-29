"""The story pipeline end to end on FakeEngine, a NullBoard and a recorded gh: refine → layers → each layer
test-first on its own branch → stack → review; stops, notes and resume."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from autogen_ext.models.replay import ReplayChatCompletionClient

from airlie.config import RoleSpec
from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.report import RunReport
from airlie.router import ChiefRouter
from airlie.story import pipeline as pipeline_module
from airlie.story.amigos import Agreement, Meeting, ProofTest
from airlie.story.board import NOTE_PREFIX, Board, BoardError, Card, Column, NullBoard, note, text_card
from airlie.story.briefs import pr_body
from airlie.story.epic_plan import PlannedStory, ScopedPath, render_story_card
from airlie.story.github_app import GitHubApp
from airlie.story.model import Criterion, EpicHeader, Layer, Story, StorySetupError
from airlie.story.pipeline import (
    StoryBlocked,
    StoryPipeline,
    _ending,
    _gate_detail,
    _layers_note,
    _reason,
    _running,
    build_commit,
)
from airlie.story.publish import Publisher, PublishError, run_cmd
from airlie.story.state import STAGES, StoryState
from airlie.teams.protocol import TeamResult
from airlie.teams.worktree import add_worktree
from airlie.topology.studio import StudioOrchestrator
from tests.unit.git_helpers import git
from tests.unit.github_helpers import FakeGitHub, fake_app
from tests.unit.test_fork_orchestrator import repo_with_marker_tests
from tests.unit.test_story_board import FakeJira
from tests.unit.test_story_board import board as jira_board_for
from tests.unit.test_studio_orchestrator import APPROVE, DONE, ROLES, RUN, WRITE_PASS, cfg_for

STORY_ROLES = {
    **ROLES,
    "product": ("---\nsummary: Stories and acceptance criteria\n---\n# product", ["read_file", "list_dir"]),
    "architect": ("---\nsummary: Layered plans for a change\n---\n# architect", ["read_file", "list_dir"]),
}


def fenced(obj: dict[str, Any]) -> FakeReply:
    return FakeReply(text="I read the README.\n```json\n" + json.dumps(obj) + "\n```")


STORY = fenced(
    {
        "type": "story",
        "title": "Add avatar upload",
        "summary": "Users upload an avatar and see it.",
        "acceptance": [
            {"id": "AC1", "given": "a user", "when": "they upload a PNG", "then": "it is stored"},
            {"id": "AC2", "given": "a stored avatar", "when": "the profile loads", "then": "it is shown"},
        ],
    }
)


def layer(slug: str, title: str, scope: str, ac: str, brief: str) -> dict[str, Any]:
    return {"slug": slug, "title": title, "scope": scope, "acceptance": [ac], "brief": brief}


LAYERS = fenced(
    {
        "layers": [
            layer("storage", "Storage", "store.py", "AC1", "Store."),
            layer("profile", "Profile", "profile.py", "AC2", "Show."),
        ]
    }
)
NOTE = FakeReply(
    tool_calls=[ToolCall("n", "write_file", json.dumps({"path": "profile.txt", "content": "shown\n"}))]
)
B1, B2 = "add-avatar-upload-01-storage", "add-avatar-upload-02-profile"


def package_reply(
    tests: dict[str, str],
    covers: dict[str, list[str]],
    *,
    files: list[dict[str, str]] | None = None,
    interfaces: list[dict[str, str]] | None = None,
    steps: list[str] | None = None,
    prose: str = "I read the code.\n",
) -> FakeReply:
    """The architect's reply for a layer's package (P1, 2026-10-24): each test file under its heading in a
    fenced block, then the package object in a json fence."""
    parts = [prose]
    for path, text in tests.items():
        parts.append(f"### test file: {path}\n```python\n{text}```\n")
    obj = {
        "type": "package",
        "files": files if files is not None else [{"path": "feature.py", "action": "create"}],
        "interfaces": interfaces or [],
        "steps": steps or ["write the feature"],
        "covers": covers,
        "done_when": "run_tests passes",
    }
    parts.append("```json\n" + json.dumps(obj) + "\n```")
    return FakeReply(text="\n".join(parts))


PKG1 = package_reply(
    {"tests/unit/test_storage.py": "def test_stores_an_avatar():\n    assert True\n"},
    {"AC1": ["tests/unit/test_storage.py::test_stores_an_avatar"]},
)
PKG2 = package_reply(
    {"tests/unit/test_profile.py": "def test_shows_the_avatar():\n    assert True\n"},
    {"AC2": ["tests/unit/test_profile.py::test_shows_the_avatar"]},
)
PKGS = [PKG1, PKG2]
"""The packages of LAYERS' two layers. The gate of these tests (./check.sh) runs no pytest, so they are read
and parsed and not run on the base."""


class RecordedGh:
    """Stands in for gh: records every command and its environment and answers `gh pr view` with numbered PRs.
    The trunk fetch (the one git command) is `fetch`: "mirror" (the default) acts as a remote whose trunk is
    the local one, setting the ref the fetch's refspec names to that branch, with no remote and no network;
    "real" runs git (a local bare remote, with local_remotes); "down" fails, as a remote that does not
    answer."""

    def __init__(self, *, fetch: str = "mirror") -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.numbers: dict[str, int] = {}
        self.fetch = fetch

    async def __call__(self, args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
        self.calls.append(args)
        self.envs.append(dict(env))
        if args[0] == "git":
            if self.fetch == "real":
                return await run_cmd(args, cwd, env)
            if self.fetch == "down":
                raise PublishError(f"`{' '.join(args)}` exited 128: no network in a unit test")
            source, _, target = args[-1].removeprefix("+").partition(":")
            found = subprocess.run(
                ["git", "rev-parse", "--verify", "-q", source], cwd=cwd, capture_output=True, text=True
            ).stdout.strip()
            if not found:
                raise PublishError(f"`{' '.join(args)}` exited 128: fatal: couldn't find remote ref {source}")
            git(cwd, "update-ref", target, found)
            return ""
        if args[:3] == ["gh", "stack", "view"]:
            raise PublishError("not a stack yet")
        if args[:3] == ["gh", "pr", "view"]:
            n = self.numbers.setdefault(args[3], 11 + len(self.numbers))
            return json.dumps({"number": n, "url": f"https://github.com/o/r/pull/{n}"})
        return ""


def pipeline(
    tmp_path: Path,
    repo: Path,
    engine: FakeEngine,
    gh: RecordedGh | None = None,
    *,
    story: dict[str, object] | None = None,
    board: Board | None = None,
    studio: type[StudioOrchestrator] = StudioOrchestrator,
    test_cmd: str | None = "./check.sh",
    app: GitHubApp | None = None,
    roles: dict[str, tuple[str, list[str] | None]] = STORY_ROLES,
    github_repo: str | None = None,
    done_check: bool = False,
    meeting: Any = None,
) -> StoryPipeline:
    """A pipeline over `repo`. `test_cmd` is a person's (None: the gate of the repository's AGENTS.md), and
    `github_repo` the repository an issue's label names (None: a written request's, the remote as written)."""
    settings = {
        "trunk": git(repo, "branch", "--show-current"),
        "plan_round_steps": 0,  # one go, no survey: the rounds and the survey have tests of their own
        "survey": False,
        "layer_attempts": 1,  # a layer that fails stops the story; re-planning has tests of its own
        "amigos": {"enabled": False},  # the meeting has tests of its own (below)
        **(story or {}),
    }
    # the done-check is off unless a test asks for it: it costs the coder one scripted reply per layer
    cfg = cfg_for(tmp_path, roles=roles, extra={"story": settings}, done_check=done_check)
    return StoryPipeline(
        cfg,
        board=board or NullBoard(cfg.observability.state_dir / "stories"),
        studio=lambda: studio(cfg, engine, router=ChiefRouter()),
        publisher=Publisher(run=gh or RecordedGh(), app=app),
        repo=repo,
        test_cmd=test_cmd,
        github_repo=github_repo,
        meeting=meeting,
    )


def kinds_all(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text().splitlines()]


def kinds(path: Path, kind: str) -> list[dict[str, Any]]:
    return [e for e in (json.loads(x) for x in path.read_text().splitlines()) if e["kind"] == kind]


def happy_engine(**scripts: list[FakeReply]) -> FakeEngine:
    """The replies of a story whose two layers build first time; a keyword replaces one role's script."""
    base = {
        "product": [STORY],
        "architect": [LAYERS, *PKGS],
        "coder": [WRITE_PASS, RUN, DONE, NOTE, DONE],
        "reviewer": [APPROVE, APPROVE],
    }
    return FakeEngine({**base, **scripts})


async def test_a_layer_built_with_the_done_check_on_ticks_its_criteria_before_review(tmp_path: Path) -> None:
    """Lever 3 (Noah, 2026-10-19): with studio.done_check on (the default), each layer's coder answers the
    done-check between its build and the review, so a story costs one more coder reply per layer."""
    repo = repo_with_marker_tests(tmp_path)
    tick = FakeReply(text="AC1 met: see the diff")
    engine = happy_engine(coder=[WRITE_PASS, RUN, DONE, tick, NOTE, DONE, tick])
    st = await pipeline(tmp_path, repo, engine, done_check=True).run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review"
    built = [e["studio_run"] for e in kinds(st.path / "events.jsonl", "layer_start")]
    runs = [kinds(st.path.parents[1] / "runs" / r / "events.jsonl", "done_check") for r in built]
    assert [len(r) for r in runs] == [1, 1]  # once per built layer, none in refine or the packages


async def test_each_layer_is_built_from_its_package_drafted_with_the_others_up_front(tmp_path: Path) -> None:
    """P1 (2026-10-24), amended 2026-10-26: the architect drafts every layer's package in one conversation
    before the first layer is built, each checked just before its layer; the pipeline places its tests in the
    worktree and commits them with the layer, and the coder's goal is the package."""
    repo = repo_with_marker_tests(tmp_path)
    engine = happy_engine()
    st = await pipeline(tmp_path, repo, engine).run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review", st.blocked
    assert [row["id"] for row in st.built["01"]["subtasks"]] == ["build"]
    assert st.built["01"]["tests"] == ["tests/unit/test_storage.py::test_stores_an_avatar"]
    assert st.built["01"]["package"]["files"] == [{"path": "feature.py", "action": "create", "why": ""}]
    build_goal = [r for r in engine.requests if r.role == "coder"][0].messages[-1].content
    assert "## The tests you make pass" in build_goal
    assert "tests/unit/test_storage.py::test_stores_an_avatar" in build_goal and "## Steps" in build_goal
    assert "tests/unit/test_storage.py" in git(repo, "show", "--name-only", "--format=", B1)
    assert "tests/unit/test_profile.py" in git(repo, "show", "--name-only", "--format=", B2)
    events = st.path / "events.jsonl"
    names = [e["kind"] for e in kinds_all(events)]
    assert names.index("package_drafted") < names.index("layer_start")  # both drafted before any build
    assert [e["layer"] for e in kinds(events, "package_drafted")] == ["01", "02"]
    assert [e["layer"] for e in kinds(events, "package_start")] == ["01", "02"]
    assert [e["layer"] for e in kinds(events, "package_ready")] == ["01", "02"]
    assert [e["paths"] for e in kinds(events, "package_placed")] == [
        ["tests/unit/test_storage.py"],
        ["tests/unit/test_profile.py"],
    ]
    assert st.drafted and st.drafts == {}  # each layer took its draft
    # one conversation: layer 02 is asked as a follow-up to layer 01's reply, before the coder starts
    roles = [r.role for r in engine.requests]
    architect = [r for r in engine.requests if r.role == "architect"]
    assert roles.index("coder") > [n for n, r in enumerate(roles) if r == "architect"][2]
    first, second = architect[1], architect[2]
    assert "packages of all 2 layers" in first.messages[1].content
    assert "This is layer 1 of 2: Storage" in first.messages[1].content
    asked = second.messages[-1].content
    assert "This is layer 2 of 2: Profile" in asked and "Now write the next layer's package" in asked
    assert len(second.messages) > len(first.messages)  # the same conversation, layer 01's reply in it


async def test_a_package_the_parser_refuses_is_asked_again_with_the_reason_then_stops_the_story(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    nonsense = FakeReply(text="Here is my plan, no package.")
    engine = FakeEngine({"product": [STORY], "architect": [LAYERS, nonsense, nonsense]})
    st = await pipeline(tmp_path, repo, engine).run(text_card("avatars"))
    assert st.blocked is not None
    assert st.blocked.startswith(
        "layer 01 (Storage): its package still fails the readiness check after 2 tries"
    )
    assert "package_unusable" in st.blocked and "01" not in st.built
    asked = [r for r in engine.requests if r.role == "architect"]
    # the retry is a turn of the drafting conversation (P9): its last message, with the drafting brief before
    assert len(asked) == 3 and "could not be used: no JSON object" in asked[2].messages[-1].content
    assert "You write the packages of all" in asked[2].messages[1].content
    assert [e["layer"] for e in kinds(st.path / "events.jsonl", "package_resumed")] == ["01"]
    assert [e["kinds"] for e in kinds(st.path / "events.jsonl", "package_returned")] == [
        ["package_unusable"],
        ["package_unusable"],
    ]
    assert not [r for r in engine.requests if r.role == "coder"]  # the coder never started
    assert worktrees(repo) == 1  # a rejected package costs no branch


async def test_a_rejected_package_records_a_lesson_and_the_next_layers_brief_carries_it(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    nonsense = FakeReply(text="no package")
    pipe = pipeline(
        tmp_path, repo, FakeEngine({"product": [STORY], "architect": [LAYERS, nonsense, nonsense]})
    )
    st = await pipe.run(text_card("avatars"))
    assert st.blocked is not None
    [lesson] = pipe.lessons.recent()
    assert (lesson.title, lesson.ending, lesson.detail) == ("Storage", "package_rejected", "package_unusable")
    assert lesson.acceptance == ["AC1"]
    assert [e["ending"] for e in kinds(st.path / "events.jsonl", "lesson_recorded")] == ["package_rejected"]
    # a later story in the repository: the architect's package brief carries the lesson
    engine = happy_engine()
    again = pipeline(tmp_path, repo, engine)
    await again.run(text_card("avatars again"))
    asked = [r.messages[1].content for r in engine.requests if r.role == "architect"]
    assert "Layers like these blocked" in asked[0]  # the layers brief
    assert "Storage: 0 source files, 0 test files, 0 steps, 0 tokens, ended package_rejected" in asked[1]


def worktrees(repo: Path) -> int:
    return git(repo, "worktree", "list", "--porcelain").count("worktree ")


def push_news(tmp_path: Path, trunk: str, name: str) -> str:
    """A commit on the origin's trunk that the repository has not fetched (it is made in another clone);
    returns it."""
    other = tmp_path / "other"
    (other / name).write_text("news\n")
    git(other, "add", name)
    git(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", name)
    git(other, "push", "-q", "origin", f"HEAD:{trunk}")
    return git(other, "rev-parse", "HEAD")


def advance_origin(tmp_path: Path, repo: Path) -> str:
    """Give `repo` a bare `origin` (a local directory) whose trunk is one commit ahead of anything `repo` has
    fetched; returns that commit. `push_news` moves it on again."""
    trunk = git(repo, "branch", "--show-current")
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", trunk, str(origin))
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", trunk)
    git(tmp_path, "clone", "-q", str(origin), str(tmp_path / "other"))
    return push_news(tmp_path, trunk, "NEWS")


class FailingGh(RecordedGh):
    """A gh whose `stack submit` fails with `failure`."""

    def __init__(self, failure: str) -> None:
        super().__init__()
        self.failure = failure

    async def __call__(self, args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
        if args[:3] == ["gh", "stack", "submit"]:
            self.calls.append(args)
            raise PublishError(self.failure)
        return await super().__call__(args, cwd, env)


class ReadingGh(RecordedGh):
    """A gh that reads each `--body-file` the way gh does: as UTF-8 bytes."""

    def __init__(self) -> None:
        super().__init__()
        self.bodies: dict[str, str] = {}

    async def __call__(self, args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
        if args[:3] == ["gh", "pr", "edit"]:
            self.bodies[args[3]] = Path(args[args.index("--body-file") + 1]).read_bytes().decode("utf-8")
        return await super().__call__(args, cwd, env)


class RecordingBoard:
    """A board that keeps its moves and notes in memory (NullBoard writes them to a file)."""

    def __init__(self) -> None:
        self.moves: list[str] = []
        self.notes: list[str] = []
        self.calls: list[tuple[str, str]] = []  # both, in order: ("move", column) and ("comment", text)

    async def card(self, ref: str) -> Card:
        raise BoardError("not needed here")

    async def next_card(self) -> Card | None:
        return None

    async def move(self, card: Card, column: Column) -> None:
        self.moves.append(column)
        self.calls.append(("move", column))

    async def comment(self, card: Card, text: str, role: str | None = None) -> None:
        self.notes.append(text)
        self.calls.append(("comment", text))


class FlakyBoard(RecordingBoard):
    """A recording board that refuses some calls: `refuses(kind, what)` is asked about ("move", column) and
    ("comment", text), and a True answer is a BoardError."""

    def __init__(self, refuses: Callable[[str, str], bool], message: str = "jira refused it") -> None:
        super().__init__()
        self.refuses, self.message = refuses, message

    async def move(self, card: Card, column: Column) -> None:
        if self.refuses("move", column):
            raise BoardError(self.message)
        await super().move(card, column)

    async def comment(self, card: Card, text: str, role: str | None = None) -> None:
        if self.refuses("comment", text):
            raise BoardError(self.message)
        await super().comment(card, text, role=role)


class DownBoard(NullBoard):
    """A board that cannot be reached."""

    async def move(self, card: Card, column: Column) -> None:
        raise BoardError("jira is unreachable")

    async def comment(self, card: Card, text: str, role: str | None = None) -> None:
        raise BoardError("jira is unreachable")


class GitFails(StudioOrchestrator):
    """A studio whose git fails the way a command against a remote with credentials in its URL does."""

    async def execute_task(self, query_text: str, **kwargs: Any) -> RunReport:
        url = "https://user:secret@example.com/o/r.git"
        raise subprocess.CalledProcessError(
            128, ["git", "ls-remote", url], stderr=f"fatal: unable to access '{url}/': Could not resolve host"
        )


class Crashes(StudioOrchestrator):
    """A studio that dies after it made its worktree."""

    async def execute_task(self, query_text: str, *, repo: Path, **kwargs: Any) -> RunReport:
        add_worktree(repo, self.run_dir.path / "studio", branch=None)
        raise RuntimeError("the engine fell over")


def studio_failing_from(
    n: int, act: Callable[[StudioOrchestrator, dict[str, Any]], Awaitable[None]]
) -> type[StudioOrchestrator]:
    """A studio that runs its first `n` tasks for real and, from the next, does `act` instead (after it made
    the worktree and branch the task would have made, as a task that dies half way has)."""
    calls = 0

    class FailsFrom(StudioOrchestrator):
        async def execute_task(self, query_text: str, **kwargs: Any) -> RunReport:
            nonlocal calls
            calls += 1
            if calls <= n:
                return await super().execute_task(query_text, **kwargs)
            add_worktree(
                kwargs["repo"],
                self.run_dir.path / "studio",
                branch=kwargs.get("branch"),
                start_point=kwargs.get("base"),
            )
            await act(self, kwargs)
            raise AssertionError("act must raise or wait")

    return FailsFrom


async def crash(orch: StudioOrchestrator, kwargs: dict[str, Any]) -> None:
    raise RuntimeError("the engine fell over")


async def stopped_with(tmp_path: Path, reason: str) -> tuple[StoryState, RecordingBoard]:
    """A story stopped with `reason`: what it records, and what its board is told."""
    tmp_path.mkdir(exist_ok=True)
    repo = repo_with_marker_tests(tmp_path)
    board = RecordingBoard()
    pipe = pipeline(tmp_path, repo, FakeEngine(), board=board)
    card = text_card("avatars")
    st = StoryState.open(tmp_path / "s", card, pipe.repo)
    await pipe._block(st, card, reason)
    return st, board


async def test_a_pack_that_cannot_be_gathered_is_an_event_and_the_plain_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(pipeline_module, "gather", broken)
    st, board = await stopped_with(tmp_path, "layer 01 (B) changed nothing")
    [posted] = board.notes
    resume = "Resume: the same `task story -- --text ...` command."
    assert posted == note(f"stopped after 'new': layer 01 (B) changed nothing\n{resume}")
    [failed] = kinds(st.path / "events.jsonl", "diagnosis_failed")
    assert failed["error"] == "OSError: disk gone"
    [blocked] = kinds(st.path / "events.jsonl", "blocked")
    assert blocked["reason"] == st.blocked and "class" not in blocked  # today's event, nothing more
    assert board.moves[-1] == "blocked"  # a diagnosis that fails never keeps the issue out of Blocked


async def test_a_cancel_while_muse_reads_the_block_puts_the_issue_back_in_intake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    board = RecordingBoard()
    pipe = pipeline(tmp_path, repo, FakeEngine(), board=board)

    async def cancelled(*args: object, **kwargs: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(pipe, "_muse_at_block", cancelled)
    card = text_card("avatars")
    st = StoryState.open(pipe.stories_dir / card.key, card, pipe.repo)
    with pytest.raises(asyncio.CancelledError):
        await pipe._block(st, card, "layer 01 (B) changed nothing")
    assert board.moves == ["intake"] and "back in To Do" in board.notes[-1]
    assert kinds(st.path / "events.jsonl", "cancelled") and not kinds(st.path / "events.jsonl", "blocked")


async def test_a_request_becomes_two_stacked_layers_and_the_card_reaches_review(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    trunk = git(repo, "branch", "--show-current")
    engine = FakeEngine(
        {
            "product": [STORY],
            "architect": [LAYERS, *PKGS],
            "coder": [WRITE_PASS, RUN, DONE, NOTE, DONE],
            "reviewer": [APPROVE, APPROVE],
        }
    )
    gh = RecordedGh()
    st = await pipeline(tmp_path, repo, engine, gh).run(text_card("Let users upload an avatar"))
    assert st.blocked is None and st.stage == "review"
    # one commit per layer, stacked, by airlie
    assert git(repo, "log", "--format=%s", f"{trunk}..{B1}") == "Add avatar upload: Storage (01/02)"
    assert git(repo, "log", "--format=%s", f"{B1}..{B2}") == "Add avatar upload: Profile (02/02)"
    assert git(repo, "show", "--name-only", "--format=", B1).splitlines() == [
        "PASS",
        "tests/unit/test_storage.py",
    ]
    assert git(repo, "log", "-1", "--format=%an", B2) == "airlie"
    # gh: the stack bottom first on the trunk, drafts, never a merge; the person's checkout untouched
    assert ["gh", "stack", "init", "--base", trunk, B1, B2] in gh.calls
    assert ["gh", "stack", "submit", "--auto", "--remote", "origin"] in gh.calls
    assert not any("merge" in a for call in gh.calls for a in call)
    assert git(repo, "branch", "--show-current") == trunk and git(repo, "status", "--porcelain") == ""
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    # PR bodies, the board's notes, the event trail
    assert [p["number"] for p in st.prs] == [11, 12]
    body = (st.path / "pr-02.md").read_text()
    assert "AC2" in body and "AC1" not in body and "2. #12 Profile (this PR)" in body
    notes = (st.path / "notes.md").read_text()
    assert "moved to building" in notes and "layer 02/02 built" in notes and "moved to review" in notes
    stages = [e["stage"] for e in kinds(st.path / "events.jsonl", "stage")]
    assert stages == ["intake", "refine", "layers", "build", "publish", "review"]


async def test_product_can_stop_a_request_it_cannot_build(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine({"product": [fenced({"type": "blocked", "reason": "which image sizes?"})]})
    st = await pipeline(tmp_path, repo, engine).run(text_card("avatars"))
    assert st.stage == "intake" and st.blocked is not None and "which image sizes?" in st.blocked
    notes = (st.path / "notes.md").read_text()
    assert "moved to blocked" in notes and "which image sizes?" in notes
    assert notes.index("stopped after") < notes.index(
        "moved to blocked"
    )  # the reason is posted, then the move
    assert [r.role for r in engine.requests] == ["product"]


async def test_unusable_replies_are_retried_with_feedback_then_stop_the_story(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    half = fenced({"type": "story", "title": "x", "summary": "y"})
    engine = FakeEngine({"product": [FakeReply(text="Sounds good to me."), half]})
    st = await pipeline(tmp_path, repo, engine).run(text_card("avatars"))
    assert st.blocked is not None and "usable story" in st.blocked and "acceptance" in st.blocked
    second = [r for r in engine.requests if r.role == "product"][1].messages[1].content
    assert "could not be used" in second and "JSON" in second
    assert len(kinds(st.path / "events.jsonl", "retry")) == 2


CUT_THINKING = FakeReply(reasoning="Let me read the files once more. ", finish_reason="length")
CUT_IN_JSON = FakeReply(
    text='Here it is.\n```json\n{"layers": [{"slug": "storage", "ti', finish_reason="length"
)


def cut_off_why(role: str) -> str:
    """What the story says of a reply of `role` that was cut off: the stage roles in these tests have the
    schema's default max_tokens. It does not say "still thinking": the result of the turn does not tell it."""
    room = RoleSpec.model_fields["max_tokens"].default
    return f"the reply was cut off at max_tokens ({room}) before it ended; raise roles.{role}.max_tokens"


@pytest.mark.parametrize("cut", [CUT_THINKING, CUT_IN_JSON], ids=["still thinking", "half a json block"])
async def test_an_architect_cut_off_at_max_tokens_stops_the_story_at_once_and_is_named(
    tmp_path: Path, cut: FakeReply
) -> None:
    """HDS-3 (2026-10-15): the architect thought until max_tokens, the loop took the empty text for its answer
    and the story stopped with "no JSON object in the reply". Now the cut-off is named. The same config meets
    the same cap, so the architect is not asked again though attempts are left."""
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine({"product": [STORY], "architect": [cut, cut, cut]})
    st = await pipeline(tmp_path, repo, engine, story={"stage_attempts": 3}).run(text_card("avatars"))
    assert st.stage == "refine" and st.blocked == f"the architect did not finish: {cut_off_why('architect')}"
    assert [r.role for r in engine.requests].count("architect") == 1
    assert not kinds(st.path / "events.jsonl", "retry")
    assert "cut off at max_tokens" in (st.path / "notes.md").read_text()  # the person is told on the board
    assert "no JSON object" not in (st.path / "events.jsonl").read_text()


async def test_a_reply_cut_off_in_a_loop_of_repeats_is_asked_again_with_the_reason(tmp_path: Path) -> None:
    """Run 9 on airlie-sandbox (2026-10-26): product's refine wrote `from_roman("IIII…` until max_tokens. A
    runaway repeat is not a reply too long for its room, so another try is worth it: it is a StoryError."""
    repo = repo_with_marker_tests(tmp_path)
    looped = FakeReply(text='{"acceptance": [{"when": "from_roman(\\"' + "I" * 600, finish_reason="length")
    engine = FakeEngine({"product": [looped, STORY], "architect": [LAYERS, *PKGS]})
    st = await pipeline(tmp_path, repo, engine, story={"stage_attempts": 3}).run(text_card("avatars"))
    assert st.blocked is None or "repeated" not in st.blocked, st.blocked
    asked = [r.messages[1].content for r in engine.requests if r.role == "product"]
    assert len(asked) == 2 and "repeated 'I'" in asked[1] and "max_tokens" not in asked[1]
    [retry] = kinds(st.path / "events.jsonl", "retry")
    assert retry["stage"] == "refine"


async def test_a_retry_prompt_has_no_operator_advice_and_a_cut_off_after_one_stops_the_story(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    replies = [FakeReply(text="Sounds good to me."), CUT_THINKING, LAYERS]
    engine = FakeEngine({"product": [STORY], "architect": replies})
    st = await pipeline(tmp_path, repo, engine, story={"stage_attempts": 3}).run(text_card("avatars"))
    asked = [r.messages[1].content for r in engine.requests if r.role == "architect"]
    assert len(asked) == 2 and st.blocked == f"the architect did not finish: {cut_off_why('architect')}"
    assert (
        "could not be used" in asked[1] and "no JSON object" in asked[1]
    )  # the model is told what was wrong
    assert "roles." not in asked[1] and "max_tokens" not in asked[1]  # in its own terms, not the operator's


async def test_a_failing_layer_stops_and_a_resume_rebuilds_only_that_layer(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    card = text_card("Let users upload an avatar")
    first = FakeEngine(
        {
            "product": [STORY],
            "architect": [LAYERS, PKG1],
            "coder": [NOTE, DONE],  # a change, but not the one the tests need
            "reviewer": [APPROVE],
        }
    )
    st = await pipeline(tmp_path, repo, first).run(card)
    assert st.stage == "layers" and st.blocked is not None and "./check.sh` fails" in st.blocked
    assert st.attempt is not None and git(repo, "rev-parse", "--verify", B1)  # kept for inspection
    again = FakeEngine(
        {
            "architect": [PKG1, PKG2],  # the failed layer is planned again, as a resume rebuilds it
            "coder": [WRITE_PASS, RUN, DONE, NOTE, DONE],
            "reviewer": [APPROVE, APPROVE],
        }
    )
    st = await pipeline(tmp_path, repo, again).run(card)
    assert st.blocked is None and st.stage == "review"
    assert not [r for r in again.requests if r.role == "product"]  # finished stages skipped
    asked = [r.messages[1].content for r in again.requests if r.role == "architect"]
    assert len(asked) == 2 and all("Write this layer's package" in a for a in asked)  # no layer plan
    assert git(repo, "show", "--name-only", "--format=", B1).splitlines() == [
        "PASS",
        "tests/unit/test_storage.py",
    ]  # rebuilt from the trunk
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    notes = (st.path / "notes.md").read_text()  # out of blocked while it builds again
    assert "resumed after 'layers'" in notes
    assert notes.rindex("moved to building") > notes.index("moved to blocked")


async def test_a_layer_that_changes_nothing_stops_the_story(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine(
        {"product": [STORY], "architect": [LAYERS, PKG1], "coder": [DONE], "reviewer": [APPROVE]}
    )
    st = await pipeline(tmp_path, repo, engine).run(text_card("avatars"))
    assert st.blocked is not None and "changed nothing" in st.blocked


async def test_a_layer_that_changed_nothing_names_the_subtasks_whose_reply_was_cut_off(
    tmp_path: Path,
) -> None:
    """A subtask cut off at max_tokens is a likely reason a layer changed nothing, or fails its tests."""
    repo = repo_with_marker_tests(tmp_path)
    engine = happy_engine(qa=[DONE], coder=[CUT_THINKING], reviewer=[APPROVE])
    st = await pipeline(tmp_path, repo, engine).run(text_card("avatars"))
    assert st.blocked is not None and st.blocked.startswith(
        "layer 01 (Storage) changed nothing; "
        f"subtask 'build' (coder): {cut_off_why('coder')}; its worktree is kept at "
    )


async def test_a_layer_is_one_coder_subtask_and_its_cut_off_is_named_once(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = happy_engine(coder=[CUT_THINKING], reviewer=[APPROVE])
    st = await pipeline(tmp_path, repo, engine).run(text_card("avatars"))
    assert st.blocked is not None and st.blocked.startswith(
        f"layer 01 (Storage) changed nothing; subtask 'build' (coder): {cut_off_why('coder')}; its worktree "
        "is kept at "
    )
    assert not [r for r in engine.requests if r.role == "qa"]


async def test_a_layer_whose_tests_fail_names_the_subtask_whose_reply_was_cut_off(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = happy_engine(qa=[DONE], coder=[NOTE, CUT_THINKING], reviewer=[APPROVE])
    st = await pipeline(tmp_path, repo, engine).run(text_card("avatars"))
    assert st.blocked is not None and st.blocked.startswith(
        f"layer 01 (Storage): `./check.sh` fails on {B1}; "
        f"subtask 'build' (coder): {cut_off_why('coder')}; its worktree is kept at "
    )


async def test_a_layer_stop_with_no_cut_off_subtask_says_nothing_of_one(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = happy_engine(qa=[DONE], coder=[DONE], reviewer=[APPROVE])
    st = await pipeline(tmp_path, repo, engine).run(text_card("avatars"))
    assert st.blocked is not None
    assert st.blocked.startswith("layer 01 (Storage) changed nothing; its worktree is kept at ")


async def test_a_git_failure_stops_the_story_with_gits_own_words(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    git(repo, "branch", B1)  # a leftover branch holds the first layer's name
    engine = FakeEngine({"product": [STORY], "architect": [LAYERS, PKG1]})
    st = await pipeline(tmp_path, repo, engine).run(text_card("avatars"))
    assert st.blocked is not None and f"a branch named '{B1}' already exists" in st.blocked
    assert "CalledProcessError" not in st.blocked


ISSUE = Card(
    "AIR-7",
    "10007",
    "AIR-7",
    "Let users upload an avatar",
    "Users want avatars.",
    url="https://example.atlassian.net/browse/AIR-7",
)
NIT = FakeReply(
    text=json.dumps(
        {
            "verdict": "approve",
            "findings": [
                {"severity": "minor", "file": "PASS", "line": 1, "claim": "name it better", "evidence": "x"}
            ],
        }
    )
)
NIT_WITHOUT_A_LINE = FakeReply(
    text=json.dumps(
        {
            "verdict": "approve",
            "findings": [{"severity": "minor", "file": "profile.txt", "claim": "say more", "evidence": "x"}],
        }
    )
)


async def test_an_issue_keeps_its_key_in_branch_names_and_its_trail_and_findings_reach_the_prs(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    trunk = git(repo, "branch", "--show-current")
    a1, a2 = f"AIR-7-{B1}", f"AIR-7-{B2}"
    gh = RecordedGh()
    engine = happy_engine(reviewer=[NIT, NIT_WITHOUT_A_LINE])
    st = await pipeline(tmp_path, repo, engine, gh).run(ISSUE)
    assert st.blocked is None and st.stage == "review"
    assert git(repo, "branch", "--list", "AIR-7-*", "--format=%(refname:short)").splitlines() == [a1, a2]
    assert ["gh", "stack", "init", "--base", trunk, a1, a2] in gh.calls
    assert [p["branch"] for p in st.prs] == [a1, a2]
    first = (st.path / "pr-01.md").read_text(encoding="utf-8")
    second = (st.path / "pr-02.md").read_text(encoding="utf-8")
    assert "[AIR-7](https://example.atlassian.net/browse/AIR-7)" in first
    assert "minor PASS:1: name it better" in first and "minor profile.txt: say more" in second
    assert "| build | coder | done | approve | 0 |" in first
    # every run is on record, and the events follow the stages
    assert len(st.runs) == 5  # refine, layers, the packages drafted together, then a build per layer
    log = [json.loads(x) for x in (st.path / "events.jsonl").read_text().splitlines()]
    assert [e["kind"] for e in log] == [
        *("story_start", "stage"),  # intake; the trunk is fetched, quietly
        *("run", "stage", "run", "stage"),  # refine, layers
        # build: every package drafted in one conversation, then each checked just before its layer;
        # ./check.sh runs no pytest, so the check on a layer's package says it ran no red check
        *("packages_drafting", "run", "package_drafted", "package_drafted"),
        *("test_checks_skipped", "package_start", "package_ready"),
        *("layer_start", "package_placed", "layer_built"),
        *("test_checks_skipped", "package_start", "package_ready"),
        *("layer_start", "package_placed", "layer_built"),
        "stage",  # mutation testing is CI's, on the PRs: no stage of its own here
        *("published", "stage", "stage", "story_done"),  # publish, review
    ]
    assert {e["run_id"] for e in log} == {
        "AIR-7"
    }  # the sink's run_id is the story; a studio run is studio_run
    assert [e["studio_run"] for e in log if e["kind"] in ("run", "layer_start")] == st.runs
    notes = (st.path / "notes.md").read_text()
    assert "layer 01/02 built: Storage (`./check.sh` passes)" in notes
    assert notes.index("pull/11") < notes.index("pull/12") and "only a human merges" in notes


async def test_a_pr_body_is_written_as_utf_8_whatever_the_locale(tmp_path: Path, ascii_locale: None) -> None:
    repo = repo_with_marker_tests(tmp_path)
    claim = "x" * 600  # cut to one line of 500, which ends in "…"
    finding = {"severity": "minor", "file": "PASS", "line": 1, "claim": claim, "evidence": "x"}
    wordy = FakeReply(text=json.dumps({"verdict": "approve", "findings": [finding]}))
    gh = ReadingGh()
    st = await pipeline(tmp_path, repo, happy_engine(reviewer=[wordy, APPROVE]), gh).run(ISSUE)
    assert st.blocked is None and st.stage == "review"  # a body written in ASCII would have stopped it here
    notes = (st.path / "notes.md").read_bytes().decode("utf-8")  # the NullBoard's notes hold airlie's "·"
    assert "\u00b7" in notes and "moved to building" in notes and "moved to review" in notes
    story, layers = Story.from_dict(st.story), tuple(Layer.from_dict(d) for d in st.layers)
    prs = [(p["number"], p["url"]) for p in st.prs]
    for i, pr in enumerate(st.prs):
        nn = f"{i + 1:02d}"
        body = pr_body(ISSUE, story, layers, i, st.built[nn]["subtasks"], prs, "./check.sh")
        assert (st.path / f"pr-{nn}.md").read_bytes().decode("utf-8") == body  # the file, read back as UTF-8
        assert gh.bodies[pr["branch"]] == body  # and what gh reads from --body-file
    first = gh.bodies[st.prs[0]["branch"]]
    assert "\u200b" in first and "…" in first  # the characters that make the encoding matter


async def test_the_stop_note_tells_a_person_how_to_resume(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    for card, how in (
        (ISSUE, "Resume: `task story -- AIR-7`."),
        (text_card("avatars"), "Resume: the same `task story -- --text ...` command."),
    ):
        engine = FakeEngine({"product": [fenced({"type": "blocked", "reason": "which image sizes?"})]})
        st = await pipeline(tmp_path, repo, engine).run(card)
        notes = (st.path / "notes.md").read_text()
        assert (
            "stopped after 'intake': product says this cannot be built as written: which image sizes?"
            in notes
        )
        assert how in notes
        [blocked] = kinds(st.path / "events.jsonl", "blocked")
        assert blocked["stage"] == "intake" and "which image sizes?" in blocked["reason"]
        assert kinds(st.path / "events.jsonl", "story_done")[-1]["blocked"] == st.blocked


async def test_a_layer_plan_that_leaves_a_criterion_unowned_is_sent_back_once(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    leaves_ac2 = fenced({"layers": [layer("storage", "Storage", "store.py", "AC1", "Store.")]})
    engine = happy_engine(architect=[leaves_ac2, LAYERS, *PKGS])
    st = await pipeline(tmp_path, repo, engine).run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review"
    [retry] = kinds(st.path / "events.jsonl", "retry")
    assert retry["stage"] == "layers" and "no layer covers acceptance ['AC2']" in retry["error"]
    asked = [r for r in engine.requests if r.role == "architect"]
    assert len(asked) == 4  # the layer plan twice, then a package per layer
    assert "could not be used" in asked[1].messages[1].content
    assert "no layer covers acceptance ['AC2']" in asked[1].messages[1].content


async def test_layer_one_builds_on_the_fetched_remote_trunk(tmp_path: Path, local_remotes: None) -> None:
    repo = repo_with_marker_tests(tmp_path)
    trunk = git(repo, "branch", "--show-current")
    news = advance_origin(tmp_path, repo)
    mine = git(repo, "rev-parse", trunk)
    st = await pipeline(tmp_path, repo, happy_engine(), RecordedGh(fetch="real")).run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review"
    git(repo, "merge-base", "--is-ancestor", news, B1)  # fails unless layer 01 sits on the fetched trunk
    assert git(repo, "show", "--name-only", "--format=", B1).splitlines() == [
        "PASS",
        "tests/unit/test_storage.py",
    ]  # the layer's own change, not NEWS
    assert git(repo, "rev-parse", trunk) == mine  # the person's own trunk is not moved
    assert kinds(st.path / "events.jsonl", "fetch_failed") == []
    assert st.base == {"ref": f"origin/{trunk}", "sha": news}  # resolved once, and kept
    [first, _] = kinds(st.path / "events.jsonl", "layer_start")
    assert (first["base"], first["base_sha"]) == (f"origin/{trunk}", news)


async def test_the_trunk_is_the_remote_branch_just_fetched_and_a_fetch_that_fails_is_a_stop(
    tmp_path: Path,
    local_remotes: None,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    trunk = git(repo, "branch", "--show-current")
    pipe = pipeline(tmp_path, repo, FakeEngine(), RecordedGh(fetch="real"))
    st = StoryState.open(tmp_path / "s", text_card("x"), pipe.repo)
    with pytest.raises(StoryBlocked, match=f"^the trunk {trunk} could not be fetched from origin"):
        await pipe._trunk_ref(st)  # no remote: no trunk to build on, and the local one is no stand-in
    [failed] = kinds(st.path / "events.jsonl", "fetch_failed")
    assert failed["remote"] == "origin" and failed["trunk"] == trunk and "origin" in failed["error"]
    news = advance_origin(tmp_path, repo)
    assert await pipe._trunk_ref(st) == f"origin/{trunk}"
    assert git(repo, "rev-parse", f"origin/{trunk}") == news


async def test_a_trunk_that_cannot_be_fetched_stops_the_story_before_any_model_and_says_to_run_it_again(
    tmp_path: Path,
) -> None:
    repo = gated(tmp_path)
    trunk = git(repo, "branch", "--show-current")
    board, engine = RecordingBoard(), happy_engine()
    pipe = pipeline(tmp_path, repo, engine, RecordedGh(fetch="down"), board=board, test_cmd=None)
    st = await pipe.run(ISSUE)
    assert st.stage == "intake" and st.base is None and st.gate is None and engine.requests == []
    assert st.blocked is not None
    assert st.blocked.startswith(f"the trunk {trunk} could not be fetched from origin")
    assert "no network in a unit test" in st.blocked and "run the story again" in st.blocked
    assert board.moves == ["building", "blocked"]
    stop = f"stopped after 'intake': the trunk {trunk} could not be fetched"
    assert board.notes[-1].startswith(f"{NOTE_PREFIX}transient: fetch_failed")  # the class leads
    assert stop in board.notes[-1]


async def test_a_story_builds_on_the_fetched_remote_trunk_never_on_a_local_one_a_model_moved(
    tmp_path: Path, local_remotes: None
) -> None:
    """A model's shell can move the local trunk (a weaker gate, say) and plant a fetch refspec that maps
    nothing, so a plain `git fetch origin main` leaves origin/main where it was, or nowhere. The fetch names
    the one ref it updates, and the story builds on it and reads its gate there, or stops."""
    repo = gated(tmp_path)
    trunk = git(repo, "branch", "--show-current")
    news = advance_origin(tmp_path, repo)  # the remote's trunk: the gate is ./check.sh
    commit_file(repo, "AGENTS.md", agents_md("true"), "the local trunk, moved to a weaker gate")
    git(repo, "update-ref", "-d", f"refs/remotes/origin/{trunk}")
    planted = "+refs/heads/nothing:refs/remotes/origin/nothing"
    git(repo, "config", "--replace-all", "remote.origin.fetch", planted)
    gh = RecordedGh(fetch="real")
    st = await pipeline(tmp_path, repo, happy_engine(), gh, test_cmd=None).run(text_card("avatars"))
    assert st.base == {"ref": f"origin/{trunk}", "sha": news}
    assert st.gate is not None and st.gate["commands"] == ["./check.sh"]
    assert st.blocked is None and st.stage == "review"


async def test_a_failed_fetch_is_masked_in_its_event_and_in_the_stop_reason(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)

    class Refuses(RecordedGh):
        async def __call__(self, args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
            url = "https://user:secret@example.com/o/r.git"
            raise PublishError(f"`git fetch -q origin main` exited 128: fatal: repository '{url}/' not found")

    st = await pipeline(tmp_path, repo, FakeEngine(), Refuses()).run(text_card("x"))
    [failed] = kinds(st.path / "events.jsonl", "fetch_failed")
    for said in (failed["error"], st.blocked or ""):
        assert "https://***@example.com/o/r.git/" in said and "secret" not in said


def test_a_stop_reason_is_what_git_said() -> None:
    said = subprocess.CalledProcessError(128, ["git", "worktree", "add", "x"], stderr="fatal: 'x' exists\n")
    assert _reason(said) == "`git worktree add x` exited 128: fatal: 'x' exists"
    on_stdout = subprocess.CalledProcessError(1, ["git", "x"], output="from stdout\n", stderr="")
    assert _reason(on_stdout) == "`git x` exited 1: from stdout"
    assert _reason(RuntimeError("boom")) == "RuntimeError: boom"


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        ("https://user:secret@host.example/o/r.git", "https://***@host.example/o/r.git"),
        ("https://ghp_0123456789@github.com/o/r", "https://***@github.com/o/r"),
        ("ssh://git@example.com:2222/o/r.git", "ssh://***@example.com:2222/o/r.git"),
        ("http://user:p@ss@host.example/x", "http://***@host.example/x"),  # a password that holds an @
        ("https://user:secret@[::1]:8443/x", "https://***@[::1]:8443/x"),
        (
            "from https://a:b@one.example/x to https://c:d@two.example/y",
            "from https://***@one.example/x to https://***@two.example/y",
        ),
        (
            "see https://host.example/o/r.git and git@github.com:o/r.git",
            "see https://host.example/o/r.git and git@github.com:o/r.git",
        ),
        ("https://host.example/search?by=bob@example.com", "https://host.example/search?by=bob@example.com"),
    ],
)
async def test_a_stop_reason_of_any_kind_is_masked_in_block_once(
    tmp_path: Path, raw: str, masked: str
) -> None:
    # a StoryBlocked message, and a git failure as _reason words it, both reach the board through _block
    st, board = await stopped_with(tmp_path, raw)
    assert st.blocked == masked and f"stopped after 'new': {masked}\n" in board.notes[0]
    git_said = subprocess.CalledProcessError(128, ["git", "ls-remote", raw], stderr=f"fatal: {raw}")
    st, board = await stopped_with(tmp_path / "again", _reason(git_said))
    assert st.blocked == f"`git ls-remote {masked}` exited 128: fatal: {masked}"


async def test_masking_a_long_unbroken_run_of_letters_takes_no_time_at_all(tmp_path: Path) -> None:
    started = time.perf_counter()
    st, _ = await stopped_with(tmp_path, "a" * 200_000)
    assert (
        time.perf_counter() - started < 5.0
    )  # a pattern before the "://" made the masking alone take minutes
    assert st.blocked is not None and len(st.blocked) == 800


async def test_a_stop_reason_is_capped_at_800_characters_keeping_its_start_and_its_end(
    tmp_path: Path,
) -> None:
    long = "`git x` exited 128: " + "a" * 3000 + " the end of it"
    st, board = await stopped_with(tmp_path, long)
    assert st.blocked is not None and len(st.blocked) == 800
    assert st.blocked.startswith("`git x` exited 128: aaa") and st.blocked.endswith("aaa the end of it")
    assert st.blocked[200] == "\u2026"
    stopped = f"stopped after 'new': {st.blocked}\n"
    [posted] = board.notes
    assert stopped in posted and posted.endswith("Resume: the same `task story -- --text ...` command.")
    # the board is told what the state and the event hold
    [event] = kinds(st.path / "events.jsonl", "blocked")
    assert event["reason"] == st.blocked
    short, _ = await stopped_with(tmp_path / "short", "x" * 800)
    assert short.blocked == "x" * 800  # at the limit, nothing is cut


async def test_a_password_is_masked_before_the_reason_is_cut(tmp_path: Path) -> None:
    password = "pw-" + "0123456789" * 6
    # cut first, the 200 characters kept from the front would end inside the password
    reason = "x" * 149 + f" https://user:{password}@example.com/o/r.git " + "y" * 900
    st, _ = await stopped_with(tmp_path, reason)
    assert st.blocked is not None and len(st.blocked) == 800
    assert st.blocked.startswith("x" * 149 + " https://***@example.com/o/r.git ")
    assert "pw-0" not in st.blocked and st.blocked.endswith("y" * 599)


async def test_url_credentials_in_a_git_failure_reach_neither_the_board_nor_the_logs(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    st = await pipeline(tmp_path, repo, FakeEngine(), studio=GitFails).run(text_card("avatars"))
    assert st.blocked is not None and "https://***@example.com/o/r.git" in st.blocked
    for name in ("state.json", "events.jsonl", "notes.md"):
        assert "secret" not in (st.path / name).read_text(), name
    assert "CalledProcessError" in kinds(st.path / "events.jsonl", "error")[0]["traceback"]  # kept, masked


async def test_a_resume_through_another_spelling_of_the_repository_path_is_not_refused(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(repo)
    card = text_card("avatars")
    unbuildable = fenced({"type": "blocked", "reason": "which image sizes?"})
    st = await pipeline(tmp_path, link, FakeEngine({"product": [unbuildable]})).run(card)
    assert st.blocked is not None and st.repo == str(repo.resolve())  # one spelling, the resolved one
    unbuildable = fenced({"type": "blocked", "reason": "which formats?"})
    st = await pipeline(tmp_path, repo, FakeEngine({"product": [unbuildable]})).run(card)
    # it ran again: a second spelling of the path did not raise StorySetupError
    assert st.blocked is not None and "which formats?" in st.blocked


async def test_a_failed_publish_stops_after_the_build_and_a_resume_publishes_without_rebuilding(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    card = text_card("Let users upload an avatar")
    failure = (
        "`gh stack submit --auto --remote origin` exited 1: remote: "
        "https://user:secret@github.com/o/r.git rejected the push"
    )
    st = await pipeline(tmp_path, repo, happy_engine(), FailingGh(failure)).run(card)
    assert st.stage == "build" and st.prs == []
    assert st.blocked == (
        "PublishError: `gh stack submit --auto --remote origin` exited 1: remote: "
        "https://***@github.com/o/r.git rejected the push"
    )
    assert "secret" not in (st.path / "notes.md").read_text()
    # both layers are kept, and the publish worktree is gone
    assert set(st.built) == {"01", "02"} and worktrees(repo) == 1
    # the same command again: nothing is asked of the models or rebuilt, the stack is published
    idle, gh = FakeEngine(), RecordedGh()
    st = await pipeline(tmp_path, repo, idle, gh).run(card)
    assert st.blocked is None and st.stage == "review" and idle.requests == []
    assert [p["number"] for p in st.prs] == [11, 12] and worktrees(repo) == 1
    assert ["gh", "stack", "submit", "--auto", "--remote", "origin"] in gh.calls
    # a story that reached review is left alone by a third run
    notes, calls = (st.path / "notes.md").read_text(), len(gh.calls)
    st = await pipeline(tmp_path, repo, idle, gh).run(card)
    assert st.blocked is None and st.stage == "review" and idle.requests == []
    assert len(gh.calls) == calls and (st.path / "notes.md").read_text() == notes
    started = kinds(st.path / "events.jsonl", "story_start")
    assert [e["resumed"] for e in started] == [False, True, True]
    assert {e["build"] for e in started} == {build_commit()}  # which airlie code ran this story


async def test_a_branch_that_was_already_there_is_never_deleted_by_a_resume(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    trunk = git(repo, "branch", "--show-current")
    git(repo, "checkout", "-q", "-b", B1)  # someone's own branch, with a name airlie would pick
    (repo / "mine.txt").write_text("my own work\n")
    git(repo, "add", "mine.txt")
    git(repo, "-c", "user.name=me", "-c", "user.email=me@x", "commit", "-q", "-m", "my own work")
    git(repo, "checkout", "-q", trunk)
    mine = git(repo, "rev-parse", B1)
    card = text_card("avatars")
    scripts = ({"product": [STORY], "architect": [LAYERS, PKG1]}, {"architect": [PKG1]})
    for engine in (FakeEngine(scripts[0]), FakeEngine(scripts[1])):  # then a resume
        st = await pipeline(tmp_path, repo, engine).run(card)
        assert st.blocked is not None and f"a branch named '{B1}' already exists" in st.blocked
        assert st.attempt is None  # the branch was not made by this story, so it is not recorded as its own
        assert git(repo, "rev-parse", B1) == mine and worktrees(repo) == 1


async def test_a_board_that_is_down_does_not_hide_the_reason(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    board = DownBoard(tmp_path / "state" / "stories")
    st = await pipeline(tmp_path, repo, FakeEngine(), board=board).run(text_card("avatars"))
    assert st.blocked == "BoardError: jira is unreachable"
    failed = kinds(st.path / "events.jsonl", "board_error")
    assert [(e["call"], e["error"]) for e in failed] == [  # the fix card's note, the stop's note, the move
        ("comment", "BoardError: jira is unreachable"),
        ("comment", "BoardError: jira is unreachable"),
        ("move", "BoardError: jira is unreachable"),
    ]
    assert json.loads((st.path / "state.json").read_text())["blocked"] == st.blocked  # on disk, whoever heard


async def test_a_role_that_cannot_run_a_stage_stops_the_story_with_a_readable_reason(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    pipe = pipeline(tmp_path, repo, FakeEngine(), story={"product_role": "nonesuch"})
    st = await pipe.run(text_card("avatars"))
    assert st.blocked is not None
    assert st.blocked.startswith("PlanError: subtask 'refine' names role 'nonesuch'; use one of: ")
    notes = (st.path / "notes.md").read_text()
    stop = notes[
        notes.index("stopped after") :
    ]  # the fix card's note before it holds the traceback as evidence
    assert "nonesuch" in notes and "Traceback" not in stop  # the stop's note is the reason, not the traceback
    assert "Traceback" in kinds(st.path / "events.jsonl", "error")[0]["traceback"]
    assert worktrees(repo) == 1


async def test_a_studio_that_crashes_leaves_no_read_only_worktree_behind(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    st = await pipeline(tmp_path, repo, FakeEngine(), studio=Crashes).run(text_card("avatars"))
    assert st.blocked == "RuntimeError: the engine fell over"
    assert worktrees(repo) == 1
    [crashed] = st.runs  # the run that died is on record, with the event that names it
    assert (tmp_path / "state" / "runs" / crashed).is_dir()
    assert [e["studio_run"] for e in kinds(st.path / "events.jsonl", "run")] == [crashed]
    assert json.loads((st.path / "state.json").read_text())["runs"] == [crashed]


# ---- the issue hears each stage; refine and layers read what the layers build on ----


async def test_the_issue_hears_the_story_and_then_the_layers_before_any_layer_is_built(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    board = RecordingBoard()
    st = await pipeline(tmp_path, repo, happy_engine(), board=board).run(ISSUE)
    assert st.blocked is None and st.stage == "review"
    heads = [n.removeprefix(NOTE_PREFIX).splitlines()[0] for n in board.notes]
    assert heads[0].startswith("picked this up") and heads[1:] == [
        "story: Add avatar upload",
        "layers, bottom first:",
        "layer 01/02 built: Storage (`./check.sh` passes)",
        "layer 02/02 built: Profile (`./check.sh` passes)",
        "stacked draft PRs, bottom layer first:",
    ]
    assert board.notes[1] == note(
        "story: Add avatar upload\n"
        "- **AC1** Given a user, when they upload a PNG, then it is stored.\n"
        "- **AC2** Given a stored avatar, when the profile loads, then it is shown."
    )
    assert board.notes[2] == note("layers, bottom first:\n1. Storage (AC1)\n2. Profile (AC2)")
    assert board.moves == ["building", "review"]


def test_a_layer_that_owns_no_criterion_is_listed_as_an_enabling_layer() -> None:
    layers = (Layer("model", "Model", "m.py", "b", ()), Layer("api", "Api", "a.py", "b", ("AC1", "AC2")))
    assert (
        _layers_note(layers) == "layers, bottom first:\n1. Model (enabling: no criterion)\n2. Api (AC1, AC2)"
    )


async def test_a_note_that_cannot_be_posted_is_not_a_reason_to_ask_the_models_again(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    card = text_card("avatars")
    story_note = FlakyBoard(lambda kind, what: kind == "comment" and what.startswith(note("story:")))
    st = await pipeline(tmp_path, repo, happy_engine(), board=story_note).run(card)
    assert st.stage == "refine" and st.story is not None  # finish came first, so the story is kept
    assert st.blocked == "BoardError: jira refused it"
    again = FakeEngine(
        {
            "architect": [LAYERS, *PKGS],
            "coder": [WRITE_PASS, RUN, DONE, NOTE, DONE],
            "reviewer": [APPROVE, APPROVE],
        }
    )
    board = RecordingBoard()
    st = await pipeline(tmp_path, repo, again, board=board).run(card)
    assert st.blocked is None and st.stage == "review"
    assert not [r for r in again.requests if r.role == "product"]  # product is not asked for the story twice
    assert not [n for n in board.notes if "story:" in n]  # and the lost note is not made up for
    layers_note = [n for n in board.notes if "layers, bottom first" in n]
    assert layers_note == [note("layers, bottom first:\n1. Storage (AC1)\n2. Profile (AC2)")]


async def test_refine_and_layers_read_the_trunk_not_the_persons_checkout(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    trunk = git(repo, "branch", "--show-current")
    git(repo, "checkout", "-q", "-b", "feature")  # the person is working on another branch
    (repo / "feature_only.txt").write_text("not on the trunk\n")
    git(repo, "add", "feature_only.txt")
    git(repo, "-c", "user.name=me", "-c", "user.email=me@x", "commit", "-q", "-m", "feature")
    look = FakeReply(tool_calls=[ToolCall("l", "list_dir", json.dumps({"path": "."}))])
    engine = happy_engine(product=[look, STORY], architect=[look, LAYERS, *PKGS])
    st = await pipeline(tmp_path, repo, engine, story={"trunk": trunk}).run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review"
    for role in ("product", "architect"):
        listing = (
            [r for r in engine.requests if r.role == role][1].messages[-1].content
        )  # what list_dir showed
        assert "README.md" in listing and "check.sh" in listing and "feature_only.txt" not in listing
    assert st.base is not None and st.base["sha"] == git(repo, "rev-parse", trunk)
    runs = kinds(st.path / "events.jsonl", "run")
    # refine, layers and the packages, drafted together, read the trunk (each is checked on its real base)
    assert len(runs) == 3 and {e["base_sha"] for e in runs} == {st.base["sha"]}
    assert "feature_only.txt" not in git(
        repo, "ls-tree", "-r", "--name-only", B2
    )  # nor do the layers build on it
    assert git(repo, "branch", "--show-current") == "feature" and git(repo, "status", "--porcelain") == ""


async def test_a_resume_builds_on_the_base_the_story_started_from_not_on_a_newer_one(
    tmp_path: Path, local_remotes: None
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    trunk = git(repo, "branch", "--show-current")
    news = advance_origin(tmp_path, repo)
    card = text_card("avatars")
    broken = happy_engine(
        architect=[LAYERS, PKG1], coder=[NOTE, DONE], reviewer=[APPROVE]
    )  # layer 01's tests stay red
    st = await pipeline(tmp_path, repo, broken, RecordedGh(fetch="real")).run(card)
    assert st.stage == "layers" and st.blocked is not None
    assert st.base == {"ref": f"origin/{trunk}", "sha": news}
    later = push_news(tmp_path, trunk, "LATER")  # the remote moves on while the story is stopped
    again = FakeEngine(
        {
            "architect": PKGS,
            "coder": [WRITE_PASS, RUN, DONE, NOTE, DONE],
            "reviewer": [APPROVE, APPROVE],
        }
    )
    st = await pipeline(tmp_path, repo, again, RecordedGh(fetch="real")).run(card)
    assert st.blocked is None and st.stage == "review"
    git(repo, "merge-base", "--is-ancestor", news, B1)  # layer 01 was rebuilt on the same base
    with pytest.raises(subprocess.CalledProcessError):  # the newer commit was never fetched
        git(repo, "cat-file", "-e", f"{later}^{{commit}}")
    assert [e["base_sha"] for e in kinds(st.path / "events.jsonl", "layer_start")][:2] == [news, news]


async def test_a_trunk_that_does_not_exist_stops_the_story_before_any_model_is_asked(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = happy_engine()
    st = await pipeline(tmp_path, repo, engine, story={"trunk": "nonesuch"}).run(text_card("avatars"))
    assert st.stage == "intake" and st.base is None
    assert st.blocked is not None
    assert st.blocked.startswith("the trunk nonesuch could not be fetched from origin")
    assert "couldn't find remote ref refs/heads/nonesuch" in st.blocked
    assert engine.requests == []


# ---- the stop path: the note and the move are separate calls, and say what a person needs ----


async def test_a_note_or_a_move_that_fails_does_not_keep_the_other_from_happening(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    unbuildable = {"product": [fenced({"type": "blocked", "reason": "which image sizes?"})]}
    secret_url = "https://user:secret@jira.example/rest"
    no_note = FlakyBoard(
        lambda kind, what: kind == "comment" and "stopped after" in what, f"{secret_url} said no"
    )
    st = await pipeline(tmp_path, repo, FakeEngine(unbuildable), board=no_note).run(text_card("avatars"))
    assert no_note.moves == ["building", "blocked"]  # the note failed, and the issue still moved
    [failed] = kinds(st.path / "events.jsonl", "board_error")
    assert (
        failed["call"] == "comment" and failed["error"] == "BoardError: https://***@jira.example/rest said no"
    )
    no_move = FlakyBoard(lambda kind, what: kind == "move" and what == "blocked")
    st = await pipeline(tmp_path, repo, FakeEngine(unbuildable), board=no_move).run(text_card("more avatars"))
    assert any("stopped after 'intake'" in n for n in no_move.notes)  # the move failed, and the note is there
    [failed] = kinds(st.path / "events.jsonl", "board_error")
    assert failed["call"] == "move" and st.blocked is not None


async def test_the_test_command_is_masked_wherever_it_is_shown(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    cmd = "./check.sh # https://user:secret@example.com/x"  # the shell ignores the comment
    gh, board = ReadingGh(), RecordingBoard()
    st = await pipeline(tmp_path, repo, happy_engine(), gh, board=board, test_cmd=cmd).run(ISSUE)
    assert st.blocked is None and st.stage == "review"
    assert len(gh.bodies) == 2 and all("secret" not in body for body in gh.bodies.values())
    assert all("Tests: `./check.sh # https://***@example.com/x` passes" in b for b in gh.bodies.values())
    assert not [n for n in board.notes if "secret" in n]
    built = [n for n in board.notes if "layer 01/02 built" in n]
    assert built == [note("layer 01/02 built: Storage (`./check.sh # https://***@example.com/x` passes)")]
    other = tmp_path / "other-story"
    other.mkdir()
    repo = repo_with_marker_tests(other)
    broken = happy_engine(qa=[DONE], coder=[NOTE, DONE], reviewer=[APPROVE])
    st = await pipeline(other, repo, broken, test_cmd=cmd).run(text_card("avatars"))
    assert st.blocked is not None and "`./check.sh # https://***@example.com/x` fails" in st.blocked
    assert "secret" not in (st.path / "notes.md").read_text() + (st.path / "events.jsonl").read_text()


async def test_the_build_stage_is_recorded_even_when_the_last_layers_note_could_not_be_posted(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    card = text_card("avatars")
    no_last_note = FlakyBoard(lambda kind, what: kind == "comment" and "layer 02/02 built" in what)
    st = await pipeline(tmp_path, repo, happy_engine(), board=no_last_note).run(card)
    assert st.blocked is not None and st.stage == "layers" and set(st.built) == {"01", "02"}
    st = await pipeline(tmp_path, repo, FakeEngine(), board=RecordingBoard()).run(card)
    assert st.blocked is None and st.stage == "review"
    stages = [e["stage"] for e in kinds(st.path / "events.jsonl", "stage")]
    assert stages == ["intake", "refine", "layers", "build", "publish", "review"]


async def test_a_stopped_story_stays_stopped_until_its_issue_is_moved_back(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    card = text_card("avatars")
    unbuildable = fenced({"type": "blocked", "reason": "which image sizes?"})
    st = await pipeline(tmp_path, repo, FakeEngine({"product": [unbuildable]})).run(card)
    reason = st.blocked
    assert reason is not None

    def on_disk() -> str | None:
        blocked: str | None = json.loads((st.path / "state.json").read_text())["blocked"]
        return blocked

    seen: list[str | None] = []

    class SignalledOnTheMove(RecordingBoard):
        async def move(self, card: Card, column: Column) -> None:
            if column == "building":
                seen.append(on_disk())  # what the next run would find if the airlie were killed right here
                raise asyncio.CancelledError  # a signal arrives while the issue is being moved back
            await super().move(card, column)

    signalled = SignalledOnTheMove()
    with pytest.raises(asyncio.CancelledError):
        await pipeline(tmp_path, repo, FakeEngine(), board=signalled).run(card)
    assert seen == [reason] and on_disk() == reason  # still a stopped story: the issue never left Blocked
    assert signalled.moves == ["intake"]  # and the cancel put the issue back where story:tick looks
    board = RecordingBoard()
    st = await pipeline(tmp_path, repo, happy_engine(), board=board).run(card)
    assert board.moves[0] == "building" and board.notes[0] == note("resumed after 'intake'")
    assert st.blocked is None and st.stage == "review" and on_disk() is None


async def test_a_failed_layer_keeps_its_worktree_and_branch_and_the_note_says_what_the_next_run_does(
    tmp_path: Path,
) -> None:
    for n, (engine, what) in enumerate(
        [
            (happy_engine(qa=[DONE], coder=[NOTE, DONE], reviewer=[APPROVE]), "`./check.sh` fails on"),
            (happy_engine(qa=[DONE], coder=[DONE], reviewer=[APPROVE]), "changed nothing"),
        ]
    ):
        root = tmp_path / str(n)
        root.mkdir()
        repo = repo_with_marker_tests(root)
        st = await pipeline(root, repo, engine).run(text_card("avatars"))
        assert st.attempt is not None and st.blocked is not None and what in st.blocked
        wt = Path(st.attempt["worktree"])
        assert wt.is_dir() and worktrees(repo) == 2 and git(repo, "rev-parse", "--verify", B1)
        assert f"its worktree is kept at {wt} (branch {B1}) for inspection. " in st.blocked
        assert st.blocked.endswith(
            "The next run removes that worktree and branch before it rebuilds the layer: "
            "copy anything you want to keep first."
        )


async def test_a_finding_that_holds_half_an_escape_reaches_neither_the_state_nor_the_pr_as_one(
    tmp_path: Path,
) -> None:
    """A lone surrogate (the reviewer's JSON had half of a \\ud83d\\ude00 pair) cannot be written as UTF-8:
    in the state it would stop every publish, this one and each resume."""
    repo = repo_with_marker_tests(tmp_path)
    gh = ReadingGh()
    odd = FakeReply(
        text='{"verdict": "approve", "findings": [{"severity": "minor", "file": "store.py", "line": 3, '
        '"claim": "half \\ud83d of a pair"}]}'
    )
    engine = happy_engine(reviewer=[odd, APPROVE])
    st = await asyncio.wait_for(pipeline(tmp_path, repo, engine, gh).run(text_card("avatars")), 120)
    assert st.blocked is None and st.stage == "review"  # published
    shown = "minor store.py:3: half \N{REPLACEMENT CHARACTER} of a pair"
    assert [f for row in st.built["01"]["subtasks"] for f in row["findings"]] == [shown]
    saved = json.loads((st.path / "state.json").read_text())
    assert [f for row in saved["built"]["01"]["subtasks"] for f in row["findings"]] == [shown]
    assert shown in gh.bodies[B1]


# ---- a signal or a cancel ----

CANCELLED = "stopped by a signal or a cancel during '{}'; it is back in To Do and the next run resumes it"


def hangs(started: asyncio.Event) -> Callable[[StudioOrchestrator, dict[str, Any]], Awaitable[None]]:
    async def hang(orch: StudioOrchestrator, kwargs: dict[str, Any]) -> None:
        started.set()
        await asyncio.sleep(3600)

    return hang


class Killed(BaseException):
    """What a hard stop looks like from inside: nothing in the pipeline catches it."""


async def test_a_story_cancelled_while_it_refines_goes_back_to_intake_and_the_next_run_resumes_it(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    card = text_card("avatars")
    started = asyncio.Event()
    board = RecordingBoard()
    studio = studio_failing_from(0, hangs(started))
    task = asyncio.create_task(pipeline(tmp_path, repo, FakeEngine(), board=board, studio=studio).run(card))
    await asyncio.wait_for(started.wait(), 30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 30)
    told = note(CANCELLED.format("refine"))
    assert board.calls[-2:] == [("comment", told), ("move", "intake")]  # told, then put back in the queue
    assert board.moves == ["building", "intake"]  # not to Blocked: it was stopped from outside, not broken
    where = tmp_path / "state" / "stories" / card.key
    [cancelled] = kinds(where / "events.jsonl", "cancelled")
    assert (cancelled["stage"], cancelled["during"]) == ("intake", "refine")
    assert (
        kinds(where / "events.jsonl", "story_done") == [] and kinds(where / "events.jsonl", "blocked") == []
    )
    assert worktrees(repo) == 1  # the read-only worktree of the ask went with it
    kept = StoryState.open(where, card, repo.resolve())
    assert kept.stage == "intake" and kept.blocked is None and len(kept.runs) == 1  # the state is as it was
    # the same command (or the next tick, which takes its issue from intake) resumes from the last stage
    again = RecordingBoard()
    st = await pipeline(tmp_path, repo, happy_engine(), board=again).run(card)
    assert again.calls[:2] == [("move", "building"), ("comment", note("resumed after 'intake'"))]
    assert st.blocked is None and st.stage == "review"
    assert len(st.runs) == 6  # the cancelled run is on record with the five that finished


async def test_a_story_cancelled_while_a_layer_builds_keeps_the_layer_and_resumes_after_the_last_stage(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    card = text_card("avatars")
    started = asyncio.Event()
    board = RecordingBoard()
    studio = studio_failing_from(
        3, hangs(started)
    )  # refine, layers and the package run, the first layer hangs
    task = asyncio.create_task(pipeline(tmp_path, repo, happy_engine(), board=board, studio=studio).run(card))
    await asyncio.wait_for(started.wait(), 60)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 30)
    assert board.calls[-2:] == [("comment", note(CANCELLED.format("build"))), ("move", "intake")]
    assert worktrees(repo) == 2 and git(
        repo, "rev-parse", "--verify", B1
    )  # the layer is kept, as in any stop
    again = RecordingBoard()
    st = await pipeline(tmp_path, repo, happy_engine(product=[], architect=PKGS), board=again).run(card)
    assert again.calls[:2] == [("move", "building"), ("comment", note("resumed after 'layers'"))]
    assert st.blocked is None and st.stage == "review" and worktrees(repo) == 1
    assert git(repo, "show", "--name-only", "--format=", B1).splitlines() == [
        "PASS",
        "tests/unit/test_storage.py",
    ]  # rebuilt from the base


@pytest.mark.parametrize("trouble", ["slow", "no note", "no move"])
async def test_a_board_in_trouble_neither_holds_up_nor_changes_a_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trouble: str
) -> None:
    monkeypatch.setattr("airlie.story.pipeline.CANCEL_NOTE_S", 0.05)
    release = asyncio.Event()  # a slow board answers when this is set, which the cancel does not wait for

    class SlowBoard(RecordingBoard):
        async def comment(self, card: Card, text: str, role: str | None = None) -> None:
            if "stopped by a signal" in text:
                await release.wait()
            await super().comment(card, text, role=role)

    board: RecordingBoard = {
        "slow": SlowBoard(),
        "no note": FlakyBoard(lambda kind, what: kind == "comment" and "stopped by a signal" in what),
        "no move": FlakyBoard(lambda kind, what: kind == "move" and what == "intake"),
    }[trouble]
    repo = repo_with_marker_tests(tmp_path)
    card = text_card("avatars")
    started = asyncio.Event()
    studio = studio_failing_from(0, hangs(started))
    task = asyncio.create_task(pipeline(tmp_path, repo, FakeEngine(), board=board, studio=studio).run(card))
    await asyncio.wait_for(started.wait(), 30)
    task.cancel()
    t0 = time.perf_counter()
    with pytest.raises(
        asyncio.CancelledError
    ):  # the cancel itself, not the board's trouble, is what surfaces
        await asyncio.wait_for(task, 5)
    assert time.perf_counter() - t0 < 5
    events = tmp_path / "state" / "stories" / card.key / "events.jsonl"
    assert kinds(events, "cancelled")
    release.set()  # the shielded note goes on after the cancel, and the loop closes with nothing pending
    await asyncio.sleep(0.1)
    told = note(CANCELLED.format("refine"))
    errors = [e["call"] for e in kinds(events, "board_error")]
    if trouble == "slow":
        assert board.calls[-2:] == [("comment", told), ("move", "intake")] and errors == []
    elif trouble == "no note":
        assert board.calls[-1] == ("move", "intake") and errors == ["comment"]  # the issue still goes back
    else:
        assert board.calls[-1] == ("comment", told) and errors == ["move"]


@pytest.mark.parametrize("hangs_on", ["the note", "the move"])
async def test_a_cancel_while_a_stop_is_told_to_the_board_still_puts_the_issue_back_in_intake(
    tmp_path: Path, hangs_on: str
) -> None:
    """The stop's own note and move are where a Ctrl-C lands when git was the one it reached first: the
    cancel is still told and the issue goes back to intake, or no tick would take it again."""
    repo = repo_with_marker_tests(tmp_path)
    card = text_card("avatars")
    telling = asyncio.Event()

    class HangsWhileTold(RecordingBoard):
        async def comment(self, card: Card, text: str, role: str | None = None) -> None:
            if hangs_on == "the note" and "stopped after" in text:
                telling.set()
                await asyncio.sleep(3600)
            await super().comment(card, text, role=role)

        async def move(self, card: Card, column: Column) -> None:
            if hangs_on == "the move" and column == "blocked":
                telling.set()
                await asyncio.sleep(3600)
            await super().move(card, column)

    board = HangsWhileTold()
    unbuildable = fenced({"type": "blocked", "reason": "which image sizes?"})
    task = asyncio.create_task(
        pipeline(tmp_path, repo, FakeEngine({"product": [unbuildable]}), board=board).run(card)
    )
    await asyncio.wait_for(telling.wait(), 30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 30)
    assert board.calls[-2:] == [("comment", note(CANCELLED.format("refine"))), ("move", "intake")]
    events = tmp_path / "state" / "stories" / card.key / "events.jsonl"
    [cancelled] = kinds(events, "cancelled")
    assert (cancelled["stage"], cancelled["during"]) == ("intake", "refine")
    assert len(kinds(events, "blocked")) == 1 and kinds(events, "story_done") == []
    again = RecordingBoard()  # the next run (the tick takes it from intake) resumes it
    st = await asyncio.wait_for(pipeline(tmp_path, repo, happy_engine(), board=again).run(card), 120)
    assert again.calls[:2] == [("move", "building"), ("comment", note("resumed after 'intake'"))]
    assert st.blocked is None and st.stage == "review"


async def test_a_story_that_was_killed_is_resumed_with_its_issue_moved_to_building_again(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    card = text_card("avatars")

    async def die(orch: StudioOrchestrator, kwargs: dict[str, Any]) -> None:
        raise Killed

    board = RecordingBoard()
    with pytest.raises(Killed):
        await pipeline(tmp_path, repo, FakeEngine(), board=board, studio=studio_failing_from(0, die)).run(
            card
        )
    assert board.moves == ["building"] and worktrees(repo) == 1  # nothing said, nothing moved back
    again = RecordingBoard()
    st = await pipeline(tmp_path, repo, happy_engine(), board=again).run(card)
    assert again.calls[:2] == [("move", "building"), ("comment", note("resumed after 'intake'"))]
    assert st.blocked is None and st.stage == "review"


def test_the_stage_a_story_is_in_is_the_one_after_the_last_that_finished() -> None:
    def at(stage: str) -> StoryState:
        return StoryState(path=Path("."), card={}, repo="", stage=stage)

    assert [_running(at(stage)) for stage in STAGES] == [*STAGES[1:], "review"]


# ---- runs on record ----


async def test_a_layer_run_that_crashes_is_on_record_with_the_runs_that_finished(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    st = await pipeline(tmp_path, repo, happy_engine(), studio=studio_failing_from(3, crash)).run(
        text_card("avatars")
    )
    assert st.blocked == "RuntimeError: the engine fell over" and st.stage == "layers"
    assert len(st.runs) == 4 and st.attempt is not None  # refine, layers, the package, the layer that died
    assert [e["studio_run"] for e in kinds(st.path / "events.jsonl", "layer_start")] == st.runs[3:]
    assert json.loads((st.path / "state.json").read_text())["runs"] == st.runs


# ---- publishing as a GitHub App's bot ----

PLANT = "git config remote.origin.url git@github.com:evil/other.git && ./check.sh"
"""A test command that redirects the remote from inside the build: model-written code runs in the test gate,
and the repository's git configuration is shared by every worktree."""


def on_github(tmp_path: Path) -> Path:
    repo = repo_with_marker_tests(tmp_path)
    git(repo, "remote", "add", "origin", "git@github.com:o/r.git")
    return repo


def agents_md(*commands: str) -> str:
    """An AGENTS.md whose Test gate lists `commands`, one per line."""
    gate = "".join(f"{c}\n" for c in commands)
    intro = "# Agents\n\nRead the README first.\n\n## Test gate\n\n"
    return f"{intro}```sh\n# every change passes these\n{gate}```\n"


def commit_file(repo: Path, name: str, text: str, message: str | None = None) -> str:
    """`name` with `text`, committed on the branch `repo` is on; returns the commit."""
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message or name)
    return git(repo, "rev-parse", "HEAD")


def checkout_of(root: Path, github_repo: str, *gate: str) -> Path:
    """A clone of github.com/<github_repo> in `root`, named after it, as repo_with_marker_tests makes one (its
    tests pass once a file named PASS exists), with an AGENTS.md whose Test gate runs `gate` (./check.sh by
    default), an SSH remote, origin, that names the repository, and origin/<trunk> where its trunk is."""
    repo = root / github_repo.split("/")[1]
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("hi\n")
    (repo / "check.sh").write_text(
        "#!/bin/sh\nif [ -f PASS ]; then echo '1 passed'; exit 0; fi\necho '1 failed'; exit 1\n"
    )
    (repo / "check.sh").chmod(0o755)
    (repo / "AGENTS.md").write_text(agents_md(*(gate or ("./check.sh",))))
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    git(repo, "remote", "add", "origin", f"git@github.com:{github_repo}.git")
    trunk = git(repo, "branch", "--show-current")
    git(repo, "update-ref", f"refs/remotes/origin/{trunk}", trunk)  # as a clone has it: fetched when cloned
    return repo


def gated(tmp_path: Path, *commands: str) -> Path:
    """repo_with_marker_tests with an AGENTS.md whose Test gate runs `commands` (./check.sh by default)."""
    repo = repo_with_marker_tests(tmp_path)
    commit_file(repo, "AGENTS.md", agents_md(*(commands or ("./check.sh",))))
    return repo


def files_under(*roots: Path) -> str:
    """Every file under `roots`, as text: where a token must never be."""
    return "".join(
        f.read_text(errors="replace") for root in roots for f in sorted(root.rglob("*")) if f.is_file()
    )


async def test_as_the_bot_a_story_mints_a_token_per_step_for_its_repository_and_the_bot_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_the_persons_own")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/the-persons-agent.sock")
    repo = on_github(tmp_path)
    trunk = git(repo, "branch", "--show-current")
    github, gh = FakeGitHub(), RecordedGh()  # no fetch reaches GitHub from a unit test
    st = await pipeline(tmp_path, repo, happy_engine(), gh, app=fake_app(tmp_path, github)).run(
        text_card("x")
    )
    assert st.blocked is None and st.stage == "review" and st.github_repo == "o/r"
    assert github.asked("GET").count("/api/repos/o/r/installation") == 1
    assert github.minted_for() == [["r"]] * 8  # the bot's identity, the fetch, then six publish steps
    assert github.revoked == github.tokens  # and each was revoked when its lookup or its step was done
    # each step's own token: the fetch, the stack (its view and its init), the submit, each PR's view and edit
    assert [c[:3] for c in gh.calls] == [
        ["git", "fetch", "-q"],
        ["gh", "stack", "view"],
        ["gh", "stack", "init"],
        ["gh", "stack", "submit"],
        ["gh", "pr", "view"],
        ["gh", "pr", "view"],
        ["gh", "pr", "edit"],
        ["gh", "pr", "edit"],
    ]
    assert [e["GH_TOKEN"] for e in gh.envs] == [github.tokens[i] for i in (1, 2, 2, 3, 4, 5, 6, 7)]
    assert all("SSH_AUTH_SOCK" not in e and e["GIT_SSH_COMMAND"] == "false" for e in gh.envs)
    # the layers are the bot's commits, author and committer
    bot = "airlie-bot-test[bot]|9919+airlie-bot-test[bot]@users.noreply.github.com"
    for branch in (B1, B2):
        assert git(repo, "log", "-1", "--format=%an|%ae%n%cn|%ce", branch).splitlines() == [bot, bot]
    assert git(repo, "log", "--format=%s", f"{trunk}..{B1}") == "Add avatar upload: Storage (01/02)"
    # and no token is kept: not in the state, an event, a PR body, a note, or the repository's config
    kept = files_under(st.path) + (repo / ".git" / "config").read_text()
    assert not [t for t in [*github.tokens, *github.jwts] if t in kept]
    fetch = ["git", "fetch", "-q", "--refmap=", "origin", f"+refs/heads/{trunk}:refs/remotes/origin/{trunk}"]
    assert gh.calls[0] == fetch and st.base is not None and st.base["ref"] == f"origin/{trunk}"


async def test_as_the_bot_a_remote_a_model_redirects_during_the_build_stops_the_story_at_publish(
    tmp_path: Path,
) -> None:
    """The test gate runs model-written code, which can rewrite the shared .git/config. Publishing then
    refuses before a token is minted for it, and the next run cannot open the story: tick sets it aside."""
    repo = on_github(tmp_path)
    github, gh = FakeGitHub(installed=("o/r", "evil/other")), RecordedGh()
    pipe = pipeline(tmp_path, repo, happy_engine(), gh, app=fake_app(tmp_path, github), test_cmd=PLANT)
    st = await pipe.run(text_card("avatars"))
    assert git(repo, "config", "remote.origin.url") == "git@github.com:evil/other.git"  # the redirect ran
    assert st.stage == "build" and st.blocked is not None
    assert "the remote 'origin' would fetch from https://github.com/evil/other.git, not" in st.blocked
    assert [c[:2] for c in gh.calls] == [["git", "fetch"]]  # nothing of gh's
    assert github.minted_for() == [["r"], ["r"]]  # the identity and the fetch, both before the build
    assert "/api/repos/evil/other/installation" not in github.asked("GET")
    with pytest.raises(
        StorySetupError, match="this story publishes to o/r, but the remote now names evil/other"
    ):
        await pipe.run(text_card("avatars"))


async def test_as_you_a_remote_a_model_redirects_during_the_build_stops_the_story_at_publish(
    tmp_path: Path,
) -> None:
    """As the bot's (above): the step refuses before gh runs, so the layers are not pushed to the other
    repository with your credentials, and the next run cannot open the story."""
    repo = on_github(tmp_path)
    gh = RecordedGh()
    pipe = pipeline(tmp_path, repo, happy_engine(), gh, test_cmd=PLANT)
    st = await pipe.run(text_card("avatars"))
    assert git(repo, "config", "remote.origin.url") == "git@github.com:evil/other.git"  # the redirect ran
    assert st.stage == "build" and st.blocked is not None
    assert "the remote 'origin' would fetch from " in st.blocked and "evil/other" in st.blocked
    assert ", not o/r on github.com: " in st.blocked
    assert [c[:2] for c in gh.calls] == [["git", "fetch"]]  # the trunk, before the build; nothing of gh's
    with pytest.raises(
        StorySetupError, match="this story publishes to o/r, but the remote now names evil/other"
    ):
        await pipe.run(text_card("avatars"))


async def test_as_you_a_rule_a_model_planted_in_an_earlier_story_stops_the_next_ones_trunk_fetch(
    tmp_path: Path,
) -> None:
    """The shared .git/config outlives a story: a model's shell left a url.<base>.insteadOf in it that sends
    the remote, still written as o/r, to another repository. The next story would take its base commit and
    its AGENTS.md gate from that repository's trunk, and push there: its fetch is refused before git runs, and
    the story stops before any model works."""
    repo = on_github(tmp_path)
    git(repo, "config", "url.https://github.com/evil/other.insteadOf", "git@github.com:o/r.git")
    board, engine, gh = RecordingBoard(), happy_engine(), RecordedGh()
    st = await pipeline(tmp_path, repo, engine, gh, board=board, github_repo="o/r").run(ISSUE)
    assert st.stage == "intake" and st.base is None and st.gate is None and engine.requests == []
    assert gh.calls == []  # nothing fetched, and nothing published
    assert st.blocked is not None and st.blocked.startswith("the trunk ")
    assert "would fetch from https://github.com/evil/other, not o/r on github.com: " in st.blocked
    assert board.moves == ["building", "blocked"]


async def test_as_the_bot_a_token_a_command_prints_reaches_no_error_state_event_or_jira_comment(
    tmp_path: Path,
) -> None:
    """gh and git can echo what they authenticate with; run_cmd masks the step's token before a PublishError
    carries it to the stop reason, the state, the events and the issue."""
    repo = on_github(tmp_path)
    github = FakeGitHub()

    class Leaky(RecordedGh):
        """Echoes its token when `leaks` fails: the fetch, or the submit."""

        def __init__(self, leaks: str) -> None:
            super().__init__()
            self.leaks = leaks

        async def __call__(self, args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
            if args[0] == "git" and self.leaks == "fetch":
                said = "fatal: unable to access https://x-access-token:$GH_TOKEN@github.com/o/r.git/"
            elif args[:3] == ["gh", "stack", "submit"] and self.leaks == "submit":
                said = "remote: Permission to o/r.git denied to token $GH_TOKEN"
            else:
                return await super().__call__(args, cwd, env)
            self.calls.append(args)
            return await run_cmd(["sh", "-c", f'echo "{said}" >&2; exit 1'], cwd, env)

    jira = FakeJira()
    app, board = fake_app(tmp_path, github), jira_board_for(jira)
    st = await pipeline(tmp_path, repo, happy_engine(), Leaky("fetch"), board=board, app=app).run(ISSUE)
    assert st.stage == "intake" and st.blocked is not None  # a trunk that cannot be fetched stops the story
    [failed] = kinds(st.path / "events.jsonl", "fetch_failed")
    for said in (failed["error"], st.blocked):
        assert "x-access-token:***@github.com" in said or "https://***@github.com" in said
    (st.path / "state.json").unlink()  # the story starts over: the fetch works now, and the submit leaks
    st = await pipeline(tmp_path, repo, happy_engine(), Leaky("submit"), board=board, app=app).run(ISSUE)
    assert st.stage == "build" and st.blocked is not None
    assert "denied to token ***" in st.blocked
    comments = [r.content.decode() for r in jira.requests if r.url.path.endswith("/comment")]
    assert any("denied to token ***" in c for c in comments)
    # the first run's stop is on the issue too (its masked URL holds "**", which ADF renders as bold markup)
    assert any("could not be fetched from origin" in c for c in comments)
    secrets = [*github.tokens, *github.jwts]
    assert len(github.tokens) >= 3 and not [
        t for t in secrets if t in files_under(st.path) + "".join(comments)
    ]


async def test_a_bot_that_cannot_publish_to_the_remote_stops_the_story_before_any_work(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    git(repo, "remote", "add", "origin", "git@gitlab.com:o/r.git")
    engine, github = happy_engine(), FakeGitHub()
    st = await pipeline(tmp_path, repo, engine, app=fake_app(tmp_path, github)).run(text_card("avatars"))
    assert st.stage == "new" and st.blocked is not None
    assert st.blocked.startswith(
        "PublishError: story.remote 'origin': 'git@gitlab.com:o/r.git' is not a github"
    )
    assert engine.requests == [] and github.requests == []


async def test_a_bot_that_cannot_be_found_stops_the_story_before_any_model_works(tmp_path: Path) -> None:
    """Who commits the layers is asked for right after prepare: an app that cannot get a token stops the
    story before refine, not after refine and the layer plan."""
    repo = on_github(tmp_path)
    engine, gh = happy_engine(), RecordedGh()
    app = fake_app(tmp_path, FakeGitHub(installed=("o/elsewhere",)))
    st = await pipeline(tmp_path, repo, engine, gh, app=app).run(text_card("avatars"))
    assert st.stage == "new" and st.blocked is not None
    assert "the GitHub App airlie-bot-test is not installed on o/r" in st.blocked
    assert engine.requests == [] and gh.calls == []  # no model, not even the fetch


async def test_a_resumed_story_pins_its_repository_before_the_runs_first_model_works(tmp_path: Path) -> None:
    """A resume has its base already, so it fetches nothing first; product's turn comes before any layer. A
    product with a shell redirects the remote in it: the run pinned o/r before, so no token is ever minted
    for the other repository, and publishing refuses."""
    repo = on_github(tmp_path)
    trunk = git(repo, "branch", "--show-current")
    card = text_card("avatars")
    stories = pipeline(tmp_path, repo, FakeEngine()).stories_dir
    st = StoryState.open(stories / card.key, card, repo.resolve(), github_repo="o/r")
    st.base = {"ref": trunk, "sha": git(repo, "rev-parse", trunk)}
    st.finish("intake")  # started, and stopped before refine
    redirect = "git config remote.origin.url git@github.com:evil/other.git"
    plant = FakeReply(tool_calls=[ToolCall("p", "shell", json.dumps({"cmd": redirect}))])
    roles = {**STORY_ROLES, "product": (STORY_ROLES["product"][0], ["read_file", "list_dir", "shell"])}
    github = FakeGitHub(installed=("o/r", "evil/other"))  # the app could mint for either
    gh = RecordedGh()
    app = fake_app(tmp_path, github)
    st = await pipeline(tmp_path, repo, happy_engine(product=[plant, STORY]), gh, app=app, roles=roles).run(
        card
    )
    assert (
        git(repo, "config", "remote.origin.url") == "git@github.com:evil/other.git"
    )  # the product's turn ran
    assert (
        st.stage == "build"
        and st.blocked is not None
        and "would fetch from https://github.com/evil" in st.blocked
    )
    assert gh.calls == []  # nothing was fetched before it, and nothing published after it
    assert "/api/repos/evil/other/installation" not in github.asked("GET")
    assert github.minted_for() == [["r"]] * len(github.tokens)


@pytest.mark.parametrize("moved", [False, True], ids=["same remote", "remote moved"])
@pytest.mark.parametrize("stage", ["publish", "review"])
async def test_a_story_that_has_published_needs_no_working_app_nor_its_remote(
    tmp_path: Path, stage: str, moved: bool
) -> None:
    """Its PRs are open. The run that tells the issue (or a tick that finds the issue in To Do again) asks
    GitHub nothing, and the repository its remote names no longer matters: nothing is published again."""
    repo = on_github(tmp_path)
    card = text_card("avatars")
    gh, app = RecordedGh(), fake_app(tmp_path, FakeGitHub())
    first = await pipeline(tmp_path, repo, happy_engine(), gh, app=app).run(card)
    assert first.stage == "review" and first.github_repo == "o/r"
    if stage == "publish":  # the PR links never reached the issue: the next run posts them
        first.stage = "publish"
        first.save()
    if moved:
        git(repo, "remote", "set-url", "origin", "git@github.com:o/moved.git")
    github = FakeGitHub(installed=())  # an app that cannot get a token now
    after = RecordedGh()
    st = await pipeline(tmp_path, repo, FakeEngine(), after, app=fake_app(tmp_path, github)).run(card)
    assert st.blocked is None and st.stage == "review"
    assert github.requests == [] and after.calls == []
    assert "stopped after" not in (st.path / "notes.md").read_text()


# ---- the test gate comes from AGENTS.md at the story's base commit ----


async def test_without_a_persons_command_the_gate_is_read_from_agents_md_once_and_kept(
    tmp_path: Path,
) -> None:
    repo = gated(tmp_path, "test -f README.md", "./check.sh")
    trunk = git(repo, "branch", "--show-current")
    blob = git(repo, "rev-parse", f"{trunk}:AGENTS.md")
    st = await pipeline(tmp_path, repo, happy_engine(), test_cmd=None).run(
        text_card("Let users upload an avatar")
    )
    assert st.blocked is None and st.stage == "review"
    assert st.gate == {"commands": ["test -f README.md", "./check.sh"], "agents_md": blob}
    [read] = kinds(st.path / "events.jsonl", "gate")
    assert (read["commands"], read["agents_md"], read["base"]) == (st.gate["commands"], blob, st.base)
    log = [json.loads(x)["kind"] for x in (st.path / "events.jsonl").read_text().splitlines()]
    assert log[:4] == ["story_start", "stage", "gate", "run"]  # after the base, before refine
    gate = "test -f README.md && ./check.sh"  # the studio's one command, as before
    assert f"layer 01/02 built: Storage (`{gate}` passes)" in (st.path / "notes.md").read_text()
    assert f"Tests: `{gate}` passes on this branch." in (st.path / "pr-01.md").read_text()


async def test_a_gate_edited_in_the_persons_checkout_or_in_a_layers_worktree_is_not_the_gate(
    tmp_path: Path,
) -> None:
    """AGENTS.md is read from the base commit: the person's own uncommitted edit and a model's edit of the
    layer's copy (a weaker gate) change nothing. The layer is held to ./check.sh, and fails it."""
    repo = gated(tmp_path)
    (repo / "AGENTS.md").write_text(agents_md("true"))  # in the person's checkout, not committed
    weaken = FakeReply(
        tool_calls=[
            ToolCall("w", "write_file", json.dumps({"path": "AGENTS.md", "content": agents_md("true")}))
        ]
    )
    engine = happy_engine(qa=[DONE], coder=[weaken, DONE], reviewer=[APPROVE])
    st = await pipeline(tmp_path, repo, engine, test_cmd=None).run(text_card("avatars"))
    assert st.gate is not None and st.gate["commands"] == ["./check.sh"]
    assert st.stage == "layers" and st.blocked is not None and "`./check.sh` fails on" in st.blocked
    assert st.attempt is not None
    assert "true" in (Path(st.attempt["worktree"]) / "AGENTS.md").read_text()  # the layer did weaken its copy


@pytest.mark.parametrize(
    ("agents", "problem"),
    [
        (None, "it has no AGENTS.md"),
        ("# Agents\n\nNo gate here.\n", "its AGENTS.md has no Test gate section"),
        (
            "## Test gate\n\n```sh\n# none yet\n```\n",
            "the Test gate section of its AGENTS.md lists no command",
        ),
        (
            "## Test gate\n\n```sh\npytest -q \\\n  --maxfail=1\n```\n",
            "the Test gate section of its AGENTS.md continues a command onto the next line "
            "('pytest -q \\\\')",
        ),
        (
            "## Test gate\n\n```sh\npnpm install &&\npnpm test\n```\n",
            "the Test gate section of its AGENTS.md continues a command onto the next line "
            "('pnpm install &&')",
        ),
        (
            "## Test gate\n\n```sh\nmake lint & make test\n```\n",
            "the Test gate section of its AGENTS.md runs a command in the background with & "
            "('make lint & make test')",
        ),
    ],
)
async def test_a_repository_without_a_test_gate_stops_the_story_before_any_model_works(
    tmp_path: Path, agents: str | None, problem: str
) -> None:
    repo = on_github(tmp_path)
    if agents is not None:
        commit_file(repo, "AGENTS.md", agents)
    trunk = git(repo, "branch", "--show-current")
    sha = git(repo, "rev-parse", trunk)
    board, engine = RecordingBoard(), happy_engine()
    pipe = pipeline(tmp_path, repo, engine, RecordedGh(), board=board, test_cmd=None, github_repo="o/r")
    st = await pipe.run(ISSUE)
    assert st.stage == "intake" and st.gate is None and engine.requests == []
    assert st.blocked is not None and st.blocked.startswith(
        f"o/r has no test gate on origin/{trunk} ({sha[:12]}): {problem}"
    )
    assert "Add a `## Test gate` section to AGENTS.md at the repository root" in st.blocked
    assert "```sh\nuv sync --frozen\nuv run ruff check .\nuv run pytest -q\n```" in st.blocked
    assert board.moves == ["building", "blocked"]  # out of intake, so the queue moves on
    assert f"stopped after 'intake': o/r has no test gate on origin/{trunk}" in board.notes[-1]


async def test_a_resume_builds_with_the_gate_the_story_read_when_it_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = gated(tmp_path)
    reads: list[str] = []
    real = pipeline_module.read_gate

    def counted(repo: Path, commit: str) -> Any:
        reads.append(commit)
        return real(repo, commit)

    monkeypatch.setattr(pipeline_module, "read_gate", counted)
    card = text_card("Let users upload an avatar")
    first = FakeEngine(
        {
            "product": [STORY],
            "architect": [LAYERS, PKG1],
            "coder": [NOTE, DONE],
            "reviewer": [APPROVE],
        }
    )
    st = await pipeline(tmp_path, repo, first, test_cmd=None).run(card)
    assert st.stage == "layers" and st.blocked is not None and "`./check.sh` fails" in st.blocked
    assert st.base is not None and reads == [st.base["sha"]]
    commit_file(repo, "AGENTS.md", agents_md("false"), "a stricter gate on the trunk")  # the trunk moves on
    again = FakeEngine(
        {
            "architect": PKGS,
            "coder": [WRITE_PASS, RUN, DONE, NOTE, DONE],
            "reviewer": [APPROVE, APPROVE],
        }
    )
    st = await pipeline(tmp_path, repo, again, test_cmd=None).run(card)
    assert st.blocked is None and st.stage == "review"
    assert len(reads) == 1 and st.gate is not None and st.gate["commands"] == ["./check.sh"]  # not read again


@pytest.mark.parametrize("given", ["./check.sh", "  ./check.sh  "])
async def test_a_persons_test_command_wins_over_agents_md_which_is_then_not_read(
    tmp_path: Path, given: str
) -> None:
    repo = gated(tmp_path, "false")  # a gate no layer passes
    st = await pipeline(tmp_path, repo, happy_engine(), test_cmd=given).run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review" and st.gate is None
    assert kinds(st.path / "events.jsonl", "gate") == []
    assert "layer 02/02 built: Profile (`./check.sh` passes)" in (st.path / "notes.md").read_text()


async def test_a_blank_test_command_is_none_and_agents_md_gates_the_story(tmp_path: Path) -> None:
    repo = gated(tmp_path, "false")
    st = await pipeline(tmp_path, repo, happy_engine(), test_cmd=" \t").run(text_card("avatars"))
    assert st.gate is not None and st.gate["commands"] == ["false"]
    assert st.blocked is not None and "`false` fails on" in st.blocked


# ---- the issue's label is the repository the story is pinned to ----


async def test_the_issues_label_is_the_repository_the_story_is_pinned_to_and_publishes_to(
    tmp_path: Path,
) -> None:
    repo = on_github(tmp_path)  # its remote names o/r
    github, gh = FakeGitHub(), RecordedGh()
    app = fake_app(tmp_path, github)
    st = await pipeline(tmp_path, repo, happy_engine(), gh, app=app, github_repo="O/R").run(ISSUE)
    assert st.blocked is None and st.stage == "review"
    assert st.github_repo == "O/R"  # as the label spells it; GitHub's names are any case
    assert github.minted_for() == [["r"]] * len(github.tokens) and len(github.tokens) == 8


async def test_a_remote_that_is_not_the_labels_repository_stops_the_story_before_any_work(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    git(repo, "remote", "add", "origin", "git@github.com:o/other.git")
    engine, gh = happy_engine(), RecordedGh()
    st = await pipeline(tmp_path, repo, engine, gh, github_repo="o/r").run(ISSUE)
    assert st.stage == "new" and st.blocked is not None
    assert "story.remote 'origin' names o/other, not o/r" in st.blocked
    assert engine.requests == [] and gh.calls == []


async def test_an_inline_comment_in_the_gate_does_not_hide_a_command_after_it_that_fails(
    tmp_path: Path,
) -> None:
    """Joined as one line, `./check.sh # …` would comment out every command after it, and the layer would pass
    on ./check.sh alone. Each command runs in a group of its own, so the one that fails fails the layer."""
    commands = ["./check.sh   # the marker tests", "test -f NEVER   # stands in for a suite that fails"]
    repo = gated(tmp_path, *commands)
    st = await pipeline(tmp_path, repo, happy_engine(), test_cmd=None).run(text_card("avatars"))
    assert st.gate is not None and st.gate["commands"] == commands
    assert st.stage == "layers" and st.blocked is not None
    assert f"`{' && '.join(commands)}` fails on" in st.blocked  # shown as the commands read, joined with &&


# ---- each layer keeps the gate it was built with: its PR says what it passed ----


async def test_each_pr_body_names_the_gate_its_own_layer_passed(tmp_path: Path) -> None:
    """Layer 01 passes AGENTS.md's gate; layer 02 fails it (it writes profile.txt), and a resume with a
    person's --test-cmd builds it with that. Each PR says what its own layer passed."""
    repo = gated(tmp_path, "./check.sh", "test ! -f profile.txt")
    card = text_card("Let users upload an avatar")
    st = await pipeline(tmp_path, repo, happy_engine(), test_cmd=None).run(card)
    assert st.stage == "layers" and st.blocked is not None and list(st.built) == ["01"]
    again = FakeEngine({"architect": [PKG2], "coder": [NOTE, DONE], "reviewer": [APPROVE]})
    st = await pipeline(tmp_path, repo, again, test_cmd="./check.sh").run(card)
    assert st.blocked is None and st.stage == "review"
    assert [st.built[n]["gate"] for n in ("01", "02")] == [
        "./check.sh && test ! -f profile.txt",
        "./check.sh",
    ]
    first, second = ((st.path / f"pr-0{n}.md").read_text(encoding="utf-8") for n in (1, 2))
    assert "Tests: `./check.sh && test ! -f profile.txt` passes on this branch." in first
    assert "Tests: `./check.sh` passes on this branch." in second


async def test_a_story_whose_layers_are_all_built_publishes_without_reading_a_gate(tmp_path: Path) -> None:
    """Every layer was built with a person's --test-cmd and the submit failed. Publishing needs no gate (each
    layer keeps its own), so a resume without --test-cmd publishes, though AGENTS.md has none to read."""
    repo = repo_with_marker_tests(tmp_path)  # no AGENTS.md
    card = text_card("Let users upload an avatar")
    st = await pipeline(tmp_path, repo, happy_engine(), FailingGh("the network fell over")).run(card)
    assert st.stage == "build" and st.blocked is not None and st.gate is None
    st = await pipeline(tmp_path, repo, FakeEngine(), test_cmd=None).run(card)
    assert st.blocked is None and st.stage == "review" and st.gate is None
    assert "Tests: `./check.sh` passes on this branch." in (st.path / "pr-02.md").read_text(encoding="utf-8")


async def test_a_layer_whose_state_kept_no_gate_is_published_with_a_tests_line_that_names_no_command(
    tmp_path: Path,
) -> None:
    """A state written before each built layer kept its gate: publishing does not fail on it, and the PR names
    no command, since the state cannot say which one the layer passed."""
    repo = repo_with_marker_tests(tmp_path)
    card = text_card("Let users upload an avatar")
    st = await pipeline(tmp_path, repo, happy_engine(), FailingGh("the network fell over")).run(card)
    assert st.stage == "build" and st.blocked is not None
    saved = json.loads((st.path / "state.json").read_text())
    for row in saved["built"].values():
        del row["gate"]
    (st.path / "state.json").write_text(json.dumps(saved))
    st = await pipeline(tmp_path, repo, FakeEngine()).run(card)
    assert st.blocked is None and st.stage == "review"
    for n in (1, 2):
        body = (st.path / f"pr-0{n}.md").read_text(encoding="utf-8")
        assert "Tests: the story's test gate passes on this branch." in body and "Tests: `" not in body


# ---- a ref a model's shell made under a short name picks neither a base nor a gate ----


@pytest.mark.parametrize("kind", ["tag", "branch"])
async def test_a_tag_or_a_branch_named_like_the_remote_trunk_picks_neither_the_base_nor_the_gate(
    tmp_path: Path, kind: str
) -> None:
    """For the short name origin/<trunk>, git reads refs/tags/origin/<trunk>, then refs/heads/origin/<trunk>,
    before refs/remotes/origin/<trunk>; a model's shell can make either from a layer's worktree (refs are
    shared). The story reads the remote-tracking branch by its full name."""
    repo = gated(tmp_path)  # the trunk's gate: ./check.sh
    trunk = git(repo, "branch", "--show-current")
    fetched = git(repo, "rev-parse", trunk)  # what the fetch brings: the recorded gh mirrors the trunk
    layer = tmp_path / "layer"
    git(repo, "worktree", "add", "-q", "--detach", str(layer))
    commit_file(layer, "AGENTS.md", agents_md("true"), "a weaker gate")
    git(layer, kind, f"origin/{trunk}")  # at the weaker commit
    st = await pipeline(tmp_path, repo, happy_engine(), test_cmd=None).run(text_card("avatars"))
    assert st.base == {"ref": f"origin/{trunk}", "sha": fetched}
    assert st.gate is not None and st.gate["commands"] == ["./check.sh"]
    assert st.blocked is None and st.stage == "review"


async def test_a_tag_named_like_a_layers_branch_is_not_the_next_layers_base(tmp_path: Path) -> None:
    """Layer 02 is built on layer 01's branch, read by its full name: for the short name, git reads a tag of
    that name first, and a model's shell can make one (at the trunk, here, where layer 01's work is not)."""
    repo = repo_with_marker_tests(tmp_path)
    trunk = git(repo, "branch", "--show-current")
    card = text_card("Let users upload an avatar")
    first = happy_engine(coder=[WRITE_PASS, RUN, DONE, DONE], reviewer=[APPROVE, APPROVE])
    st = await pipeline(tmp_path, repo, first).run(card)
    assert list(st.built) == ["01"] and st.blocked is not None and "changed nothing" in st.blocked
    git(repo, "tag", B1, trunk)
    again = FakeEngine({"architect": [PKG2], "coder": [NOTE, DONE], "reviewer": [APPROVE]})
    st = await pipeline(tmp_path, repo, again).run(card)
    assert st.blocked is None and st.stage == "review"
    git(repo, "merge-base", "--is-ancestor", f"refs/heads/{B1}", f"refs/heads/{B2}")  # on layer 01's work


SURVEY_NOTES = FakeReply(text="- store.py:10 Store class\n- tests live in tests/unit/test_store.py")


def architect_requests(engine: FakeEngine) -> list[Any]:
    return [r for r in engine.requests if r.role == "architect"]


def text_of(request: Any) -> str:
    return "\n".join(str(m.content) for m in request.messages)


async def test_the_architect_surveys_while_product_refines_and_plans_from_its_notes_in_rounds(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    (repo / "AGENTS.md").write_text("# rules\nthe gate is ./check.sh\n")
    git(repo, "add", "AGENTS.md")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "agents")
    engine = happy_engine(architect=[SURVEY_NOTES, LAYERS, LAYERS, *PKGS])
    settings = {"survey": True, "plan_round_steps": 2}
    st = await pipeline(tmp_path, repo, engine, story=settings).run(text_card("Let users upload an avatar"))
    assert st.blocked is None and st.stage == "review"
    arch = architect_requests(engine)
    # the survey, round 1, round 2 (the same plan: converged, no round 3), then a package per layer
    assert len(arch) == 5
    assert "Do not plan and do not write JSON" in text_of(arch[0])
    layers_ask = text_of(arch[1])
    assert "store.py:10 Store class" in layers_ask  # the survey's notes
    assert "the gate is ./check.sh" in layers_ask  # AGENTS.md, read for it
    assert "That is your plan so far" in text_of(arch[2])  # round 2 goes on in the same conversation
    [steer] = kinds(st.path / "events.jsonl", "steer")  # round 2's nudge, the one steering text sent
    assert {k: steer[k] for k in ("id", "role", "round", "stage")} == {
        "id": "plan-round",
        "role": "architect",
        "round": 2,
        "stage": "layers",
    }
    events = (st.path / "events.jsonl").read_text()
    assert [e["kind"] for e in kinds(st.path / "events.jsonl", "plan_done")] == ["plan_done"]
    assert kinds(st.path / "events.jsonl", "plan_done")[0]["reason"] == "converged"
    assert kinds(st.path / "events.jsonl", "survey_done")[0]["chars"] == len(str(SURVEY_NOTES.text))
    assert st.survey == SURVEY_NOTES.text and '"survey_failed"' not in events


async def test_a_survey_that_fails_is_no_notes_and_the_story_goes_on(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = happy_engine(architect=[CUT_THINKING, LAYERS, *PKGS])
    st = await pipeline(tmp_path, repo, engine, story={"survey": True}).run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review"
    assert st.survey == "" and kinds(st.path / "events.jsonl", "survey_failed")
    assert "Notes from reading the code" not in text_of(architect_requests(engine)[1])


async def test_a_story_that_has_refined_is_not_surveyed_again_on_resume(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    nonsense = FakeReply(text="no plan here")
    first = happy_engine(architect=[SURVEY_NOTES, nonsense, nonsense])  # layers: two unusable replies, a stop
    stopped = await pipeline(tmp_path, repo, first, story={"survey": True}).run(text_card("avatars"))
    assert stopped.stage == "refine" and stopped.blocked and stopped.survey == SURVEY_NOTES.text
    again = happy_engine(architect=[LAYERS, *PKGS])
    st = await pipeline(tmp_path, repo, again, story={"survey": True}).run(text_card("avatars"))
    arch = architect_requests(again)
    assert len(arch) == 3 and "Do not plan and do not write JSON" not in text_of(arch[0])  # layers, no survey
    assert "store.py:10 Store class" in text_of(arch[0])  # the notes kept from the first run
    assert st.blocked is None and st.stage == "review"


async def test_one_go_planning_asks_the_architect_once_and_never_continues_the_conversation(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = happy_engine()
    st = await pipeline(tmp_path, repo, engine).run(text_card("avatars"))  # the helper's default: 0 and off
    assert st.stage == "review" and len(architect_requests(engine)) == 3  # the plan once, a package per layer
    assert not kinds(st.path / "events.jsonl", "plan_round")


def test_the_build_is_the_commit_of_the_checkout_airlie_runs_from() -> None:
    here = Path(__file__).resolve().parents[2]
    assert build_commit() == git(here, "rev-parse", "--short", "HEAD").strip()


def _coder(stop: Any) -> TeamResult:
    return TeamResult("build", False, stop, "", 3, 10, 5, 1.0)


@pytest.mark.parametrize(
    ("result", "ending"),
    [
        (_coder("max_steps"), "out_of_steps"),
        (_coder("cut_off"), "out_of_context"),
        (_coder("context_full"), "out_of_context"),
        (_coder("done"), "gate_red"),
        (_coder("repeated_action"), "gate_red"),
        (None, "gate_red"),
    ],
)
def test_a_red_layer_is_recorded_as_how_the_coder_stopped(result: TeamResult | None, ending: str) -> None:
    """D7 of the planning-line design: a lesson says whether the layer was too big (the coder ran out of steps
    or context) or merely red."""
    assert _ending(result) == ending


def test_a_lessons_detail_is_the_first_line_that_says_what_failed() -> None:
    out = "collected 4 items\n\nFAILED tests/test_a.py::test_x - assert 1 == 2\nFAILED tests/test_b.py::t\n"
    assert _gate_detail(out) == "FAILED tests/test_a.py::test_x - assert 1 == 2"
    assert _gate_detail("src/a.py:3: error: Name 'x' is not defined  [name-defined]\nFound 1 error") == (
        "src/a.py:3: error: Name 'x' is not defined  [name-defined]"
    )
    assert _gate_detail("all quiet\nexit 1") == "exit 1"
    assert _gate_detail("") == "the gate failed"
    assert len(_gate_detail("FAILED " + "x" * 500)) == 200


# ---- stories born from an epic (P3, 2026-10-24) ----

HEADER = EpicHeader("AIR-3", "Avatars, in two stories.", ("PNG only",), "a huge file is accepted")


def planned(sid: str, title: str, *, scope: tuple[ScopedPath, ...] = ()) -> PlannedStory:
    return PlannedStory(
        sid,
        title,
        "The epic needs it.",
        "o/r",
        ("EC1",),
        scope,
        (),
        (),
        (
            Criterion("AC1", "a user", "they act", "it works"),
            Criterion("AC2", "a stored avatar", "loaded", "shown"),
        ),
        (),
        (),
        (),
        "S",
    )


def card_of_epic(key: str, ps: PlannedStory, after: tuple[str, ...] = ()) -> Card:
    """A story's board card as the epic pipeline makes it: its description closes with the contract."""
    labels = ("repo:o/r", "epic:AIR-3", f"plan:{ps.id}", *(f"after:{k}" for k in after))
    return Card(key, key, key, ps.title, render_story_card(ps, HEADER, after), None, (), labels)


async def test_a_story_card_with_a_block_skips_refine_and_carries_the_epic_header_into_its_briefs(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = happy_engine(product=[])
    card = card_of_epic("AIR-4", planned("S1", "Add avatar upload"))
    st = await pipeline(tmp_path, repo, engine).run(card)
    assert st.blocked is None and st.stage == "review", st.blocked
    assert not [r for r in engine.requests if r.role == "product"]  # product's refine is the meeting's job
    assert Story.from_dict(st.story or {}).epic == HEADER and st.story is not None
    assert st.story["title"] == "Add avatar upload"
    [skipped] = kinds(st.path / "events.jsonl", "refine_skipped")
    assert skipped["reason"] == "story card"
    asked = [text_of(r) for r in architect_requests(engine)]
    for brief in (asked[0], asked[1]):  # the layers brief, then the first package brief
        assert "## The epic this story serves" in brief and "AIR-3: Avatars, in two stories." in brief
        assert "- PNG only" in brief and "Risk this story must catch: a huge file is accepted" in brief


async def test_a_story_card_whose_block_is_flawed_blocks_the_story_with_the_reason(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    description = '```json\n{"type": "story_card", "story": 3, "epic": {}}\n```\n'
    card = Card("AIR-4", "AIR-4", "AIR-4", "x", description, None, (), ("repo:o/r",))
    engine = happy_engine(product=[])
    st = await pipeline(tmp_path, repo, engine).run(card)
    assert st.blocked is not None and "story card's closing block is flawed" in st.blocked
    assert not engine.requests


async def test_a_dependent_story_bases_on_its_dependencys_top_branch_and_publishes_a_stack_on_it(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    low = card_of_epic("AIR-4", planned("S1", "Add avatar upload"))
    first = await pipeline(tmp_path, repo, happy_engine(product=[])).run(low)
    top_branch = f"AIR-4-{B2}"
    assert first.stage == "review" and first.built["02"]["branch"] == top_branch
    top = git(repo, "rev-parse", f"refs/heads/{top_branch}")

    view = {**layer("view", "View", "view.py", "AC1", "Show it."), "acceptance": ["AC1", "AC2"]}
    layer_reply = fenced({"layers": [view]})
    pkg = package_reply(
        {"tests/unit/test_view.py": "def test_view():\n    assert True\n"},
        {"AC1": ["tests/unit/test_view.py::test_view"], "AC2": ["tests/unit/test_view.py::test_view"]},
    )
    high = card_of_epic("AIR-5", planned("S2", "Show the avatar"), after=("AIR-4",))
    write_view = FakeReply(
        tool_calls=[ToolCall("n", "write_file", json.dumps({"path": "view.txt", "content": "view\n"}))]
    )
    engine = FakeEngine({"architect": [layer_reply, pkg], "coder": [write_view, DONE], "reviewer": [APPROVE]})
    gh = RecordedGh()
    st = await pipeline(tmp_path, repo, engine, gh).run(high)
    assert st.blocked is None and st.stage == "review", st.blocked
    assert st.base == {"ref": top_branch, "sha": top, "story": "AIR-4"}
    branch = st.built["01"]["branch"]
    assert branch == "AIR-5-show-the-avatar-01-view"
    git(repo, "merge-base", "--is-ancestor", f"refs/heads/{top_branch}", f"refs/heads/{branch}")
    assert [
        "gh",
        "stack",
        "init",
        "--base",
        top_branch,
        branch,
    ] in gh.calls  # the stack's base is its dependency
    assert "Stacks on AIR-4's pull requests; merge those first." in (st.path / "pr-01.md").read_text()


async def test_a_dependency_not_yet_in_review_is_a_wait_not_a_build(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    high = card_of_epic("AIR-5", planned("S2", "Show the avatar"), after=("AIR-4",))
    engine = FakeEngine({"architect": []})
    gh = RecordedGh()
    st = await pipeline(tmp_path, repo, engine, gh).run(high)
    assert st.stage == "new" and st.blocked is None and st.base is None
    assert not engine.requests and not gh.calls  # nothing built, nothing fetched or pushed
    [waiting] = kinds(st.path / "events.jsonl", "waiting")
    assert "AIR-4" in waiting["reason"]
    assert "waiting for AIR-4" in (st.path / "notes.md").read_text()
    # a dependency that has started but is not in review yet is a wait too
    low = card_of_epic("AIR-4", planned("S1", "Add avatar upload"))
    stopped = await pipeline(tmp_path, repo, FakeEngine({"architect": [FakeReply(text="no plan")] * 2})).run(
        low
    )
    assert stopped.blocked is not None and stopped.stage != "review"
    again = await pipeline(tmp_path, repo, FakeEngine({"architect": []})).run(high)
    assert again.stage == "new" and again.blocked is None and again.base is None


# ---- the story meeting (P2, 2026-10-24) ------------------------------------------------------------

MEETING_ROLES = {
    **STORY_ROLES,
    "tester": ("---\nsummary: Proof planning\n---\n# tester", ["read_file", "list_dir"]),
}
MEETING = {"amigos": {"enabled": True}}


class FakeMeeting:
    """A meeting with Meeting.run's signature that holds none and returns the agreement it was given."""

    def __init__(self, agreement: Agreement) -> None:
        self.agreement = agreement

    async def run(
        self,
        opening: str,
        story: Story,
        layers: tuple[Layer, ...],
        levels: Mapping[str, int],
        guidelines: Any,
        *,
        repo: Path,
        base: str,
        state: Mapping[str, Any] | None = None,
    ) -> Agreement:
        return self.agreement


def never(cfg: Any, tools: Any, on_turn: Any) -> Any:
    raise AssertionError("no meeting is held here")


def repo_with_levels(tmp_path: Path) -> Path:
    """The marker repository with a test tree of two levels, so a proof can name them."""
    repo = repo_with_marker_tests(tmp_path)
    for path in ("tests/unit/test_existing.py", "tests/integration/test_wiring.py"):
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text("def test_x():\n    assert True\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "tests")
    return repo


def block(obj: Any) -> str:
    return "```json\n" + json.dumps(obj) + "\n```"


def verdict_block(**values: str) -> str:
    v = {"requirements": "agree", "changes": "agree", "proof": "agree", **values}
    objections = (
        [{"part": "proof", "about": "AC2", "claim": "thin", "suggest": "assert the url"}]
        if v["proof"] == "object"
        else []
    )
    return block({"type": "verdict", **v, "objections": objections})


AGREE_V = verdict_block()
AGREED_PROOF = {
    "type": "proof",
    "tests": [
        {
            "criterion": "AC1",
            "layer": "storage",
            "file": "tests/unit/test_storage.py",
            "name": "test_stores_an_avatar",
            "level": "unit",
            "asserts": "the avatar is stored",
            "needs": "a tmp_path",
        },
        {
            "criterion": "AC2",
            "layer": "profile",
            "file": "tests/unit/test_profile.py",
            "name": "test_shows_the_avatar",
            "level": "unit",
            "asserts": "the profile holds the avatar",
            "needs": "a stored avatar",
        },
    ],
}
REWORDED_STORY = {
    "type": "story",
    "title": "Add avatar upload",
    "summary": "Users upload an avatar and see it.",
    "acceptance": [
        {"id": "AC1", "given": "a user", "when": "they upload a PNG", "then": "it is stored"},
        {"id": "AC2", "given": "a stored avatar", "when": "the profile loads", "then": "it is shown"},
    ],
}
AGREED_SPLIT = {
    "layers": [
        {
            **layer("storage", "Storage", "src/store.py", "AC1", "Store avatars as PNG files."),
            "files": [{"path": "src/store.py", "new": True}],
            "interfaces": ["def save(avatar: bytes) -> int"],
        },
        {
            **layer("profile", "Profile", "src/profile.py", "AC2", "Show the avatar."),
            "files": [{"path": "src/profile.py", "new": True}],
        },
    ]
}


def spoken(prose: str, *blocks: str) -> str:
    return "\n".join([prose, *blocks])


def replay_meeting(
    script: Mapping[str, list[str]], *, max_turns: int = 12, hook: Callable[..., Any] | None = None
) -> Any:
    """A MeetingFactory over replay clients: each role replays its script, one reply per call."""

    def factory(cfg: Any, tools: Any, on_turn: Callable[..., None]) -> Meeting:
        def clients(role: str, spec: RoleSpec) -> ReplayChatCompletionClient:
            return ReplayChatCompletionClient(list(script[role]))

        return Meeting(
            cfg,
            clients=clients,
            tools=None,
            max_turns=max_turns,
            tool_iterations=0,
            on_turn=hook(on_turn) if hook else on_turn,
        )

    return factory


EASY_MEETING = {
    "product": [spoken("Reworded nothing.", block(REWORDED_STORY), AGREE_V), spoken("ok", AGREE_V)],
    "architect": [spoken("Files named.", block(AGREED_SPLIT), AGREE_V), spoken("ok", AGREE_V)],
    "tester": [spoken("The proof.", block(AGREED_PROOF), AGREE_V)],
}


async def test_a_story_is_agreed_between_its_layers_and_its_first_build_and_the_card_hears_it(
    tmp_path: Path,
) -> None:
    repo = repo_with_levels(tmp_path)
    reworded = json.loads(json.dumps(REWORDED_STORY))
    reworded["acceptance"][1]["then"] = "it is shown with its alt text"
    script = {
        **EASY_MEETING,
        "product": [spoken("Sharper.", block(reworded), AGREE_V), spoken("ok", AGREE_V)],
    }
    board = RecordingBoard()
    engine = happy_engine()
    pipe = pipeline(
        tmp_path,
        repo,
        engine,
        board=board,
        story=MEETING,
        roles=MEETING_ROLES,
        meeting=replay_meeting(script),
    )
    st = await pipe.run(ISSUE)
    assert st.blocked is None and st.stage == "review", st.blocked
    events = [json.loads(x) for x in (st.path / "events.jsonl").read_text().splitlines()]
    names = [e["kind"] for e in events]
    stage_layers = next(n for n, e in enumerate(events) if e["kind"] == "stage" and e["stage"] == "layers")
    assert (
        names.index("amigos_turn") < names.index("amigos_agreed") < stage_layers < names.index("layer_start")
    )
    assert [e["role"] for e in events if e["kind"] == "amigos_turn"] == [
        "product",
        "architect",
        "tester",
        "airlie",
        "product",
        "architect",
    ]
    assert [e["turns"] for e in events if e["kind"] == "amigos_agreed"] == [6]
    # the agreement is the state: the story as worded, the layers as split, the proof as planned
    assert st.agreement is not None and st.meeting is None
    assert [t["name"] for t in st.agreement["proof"]] == ["test_stores_an_avatar", "test_shows_the_avatar"]
    assert st.story is not None and st.story["acceptance"][1]["then"] == "it is shown with its alt text"
    assert st.agreement["layers"][0]["files"] == [{"path": "src/store.py", "new": True}]
    assert st.layers is not None and st.layers[0]["brief"] == "Store avatars as PNG files."
    # the card hears it, after the layers and before the first layer is built
    heads = [n.removeprefix(NOTE_PREFIX).splitlines()[0] for n in board.notes]
    assert heads[2:5] == [
        "layers, bottom first:",
        "agreement of product, the architect and the tester (6 turns)",
        "the meeting changed the criteria:",
    ]
    agreed = board.notes[3]
    assert (
        "it is shown with its alt text" in agreed
        and "| AC1 | storage | unit | tests/unit/test_storage.py" in agreed
    )
    assert "reworded: **AC2**" in board.notes[4]
    # the layers are built from the agreement: the architect's package ask carries the agreed brief
    asked = [r.messages[1].content for r in engine.requests if r.role == "architect"]
    assert "Store avatars as PNG files." in asked[1]


async def test_the_agreement_stored_has_no_reference_to_a_dropped_criterion(tmp_path: Path) -> None:
    """Product dropped AC2 and the turns ran out before the architect and the tester fixed their parts: what
    is stored (and built from) holds no AC2 anywhere."""
    repo = repo_with_levels(tmp_path)
    agreed_story = Story.from_dict(
        {**json.loads(json.dumps(REWORDED_STORY)), "acceptance": REWORDED_STORY["acceptance"][:1]}
    )
    split = (
        Layer("storage", "Storage", "store.py", "Store.", ("AC1",)),
        Layer("profile", "Profile", "profile.py", "Show.", ("AC2",)),
    )
    proof = tuple(ProofTest.from_dict(t) for t in AGREED_PROOF["tests"])
    stale = Agreement(agreed_story, split, proof, 12, dropped={"AC2": "shown by another story"})
    pipe = pipeline(
        tmp_path,
        repo,
        happy_engine(architect=[LAYERS, PKG1]),
        story=MEETING,
        roles=MEETING_ROLES,
        meeting=lambda cfg, tools, on_turn: FakeMeeting(stale),
    )
    st = await pipe.run(text_card("avatars"))
    assert st.agreement is not None and st.layers is not None and st.story is not None
    stored = json.dumps(
        [st.agreement["story"], st.agreement["layers"], st.agreement["proof"], st.layers, st.story]
    )
    assert "AC2" not in stored
    assert st.agreement["layers"][1]["acceptance"] == []
    assert [t["criterion"] for t in st.agreement["proof"]] == ["AC1", None]
    assert [e["ids"] for e in kinds(st.path / "events.jsonl", "amigos_references_pruned")] == [["AC2"]]


async def test_an_agreement_that_cannot_be_built_stops_the_story_naming_what_is_missing(
    tmp_path: Path,
) -> None:
    repo = repo_with_levels(tmp_path)
    story = Story.from_dict(REWORDED_STORY)
    split = (Layer("storage", "Storage", "store.py", "Store.", ("AC1",)),)  # AC2 is nobody's
    proof = tuple(ProofTest.from_dict(t) for t in AGREED_PROOF["tests"][:1])
    pipe = pipeline(
        tmp_path,
        repo,
        happy_engine(),
        story=MEETING,
        roles=MEETING_ROLES,
        meeting=lambda cfg, tools, on_turn: FakeMeeting(Agreement(story, split, proof, 12)),
    )
    st = await pipe.run(text_card("avatars"))
    assert st.blocked is not None and "the meeting ended without an agreement that can be built" in st.blocked
    assert "AC2" in st.blocked and st.agreement is None and st.stage == "refine"
    assert st.meeting is None


async def test_a_story_killed_mid_meeting_resumes_the_meeting_from_its_saved_history(tmp_path: Path) -> None:
    """Cancelled right after turn 5: the story keeps the team's state, and a new run goes on from the
    architect's turn with fresh agents. Product is not asked to refine again, nor the architect to split."""
    repo = repo_with_levels(tmp_path)
    seen: list[int] = []

    def kill_after_five(on_turn: Callable[..., None]) -> Callable[..., None]:
        def hooked(turn: Any, state: Any) -> None:
            on_turn(turn, state)
            seen.append(1)
            if len(seen) == 5:
                raise asyncio.CancelledError

        return hooked

    first = pipeline(
        tmp_path,
        repo,
        happy_engine(),
        story=MEETING,
        roles=MEETING_ROLES,
        meeting=replay_meeting(EASY_MEETING, hook=kill_after_five),
    )
    with pytest.raises(asyncio.CancelledError):
        await first.run(text_card("avatars"))
    saved = StoryState.open(first.stories_dir / text_card("avatars").key, text_card("avatars"), first.repo)
    assert saved.meeting is not None and saved.agreement is None and saved.stage == "refine"
    thread = saved.meeting["agent_states"]["RoundRobinGroupChatManager"]["message_thread"]
    assert sum(1 for m in thread if m["source"] != "user") == 5
    # what each role has left to say: product has spoken twice, the architect once, the tester once
    left = {"product": [], "architect": EASY_MEETING["architect"][1:], "tester": []}
    resumed_engine = FakeEngine(
        {
            "architect": [PKG1, PKG2],
            "coder": [WRITE_PASS, RUN, DONE, NOTE, DONE],
            "reviewer": [APPROVE, APPROVE],
        }
    )
    second = pipeline(
        tmp_path,
        repo,
        resumed_engine,
        story=MEETING,
        roles=MEETING_ROLES,
        meeting=replay_meeting(left),
    )
    st = await second.run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review", st.blocked
    turns = kinds(st.path / "events.jsonl", "amigos_turn")
    assert [e["role"] for e in turns] == ["product", "architect", "tester", "airlie", "product", "architect"]
    assert st.agreement is not None and st.agreement["turns"] == 6 and st.meeting is None
    assert not [r for r in resumed_engine.requests if r.role == "product"]


async def test_a_meeting_that_ends_by_owner_decides_records_a_lesson_and_the_objections(
    tmp_path: Path,
) -> None:
    repo = repo_with_levels(tmp_path)
    object_ = verdict_block(proof="object")
    script = {
        "product": [spoken("The proof is thin.", object_)] * 2,
        "architect": [spoken("ok", AGREE_V)] * 2,
        "tester": [spoken("The proof.", block(AGREED_PROOF), AGREE_V), spoken("Same.", AGREE_V)],
    }
    board = RecordingBoard()
    pipe = pipeline(
        tmp_path,
        repo,
        happy_engine(),
        board=board,
        story=MEETING,
        roles=MEETING_ROLES,
        meeting=replay_meeting(script, max_turns=8),
    )
    st = await pipe.run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review", st.blocked  # the story goes on
    events = st.path / "events.jsonl"
    assert [e["parts"] for e in kinds(events, "amigos_owner_decided")] == [["proof"]]
    assert [(e["role"], e["part"], e["about"]) for e in kinds(events, "amigos_unresolved")] == [
        ("product", "proof", "AC2")
    ]
    assert not kinds(events, "amigos_agreed")
    [lesson] = [x for x in pipe.lessons.recent() if x.ending == "owner_decided"]
    assert lesson.detail == "proof" and lesson.title == "Add avatar upload"
    assert [e["ending"] for e in kinds(events, "lesson_recorded")] == ["owner_decided"]
    heads = [n.removeprefix(NOTE_PREFIX).splitlines()[0] for n in board.notes]
    assert "objections still open when the meeting ended (each owner's part stands):" in heads
    assert any("product on proof (AC2): thin" in n for n in board.notes)
    assert "decided by their owner at the turn cap: proof" in board.notes[3]


async def test_a_blocked_reply_from_product_in_the_meeting_stops_the_story(tmp_path: Path) -> None:
    repo = repo_with_levels(tmp_path)
    blocked = block({"type": "blocked", "reason": "which sizes?"})
    script = {
        "product": [spoken("Cannot build this.", blocked, AGREE_V)],
        "architect": [spoken("ok", AGREE_V)],
        "tester": [spoken("ok", AGREE_V)],
    }
    pipe = pipeline(
        tmp_path,
        repo,
        happy_engine(),
        story=MEETING,
        roles=MEETING_ROLES,
        meeting=replay_meeting(script),
    )
    st = await pipe.run(text_card("avatars"))
    assert (
        st.blocked is not None and "product says this cannot be built as written: which sizes?" in st.blocked
    )
    assert st.meeting is None  # the next run holds the meeting afresh


async def test_a_meeting_that_stops_the_story_leaves_its_transcript_on_disk(tmp_path: Path) -> None:
    """The tester never posts a proof (as Muse did on the first real run): the story stops, the saved team
    state is forgotten, and what each role said, with the problems each turn had, stays readable."""
    repo = repo_with_levels(tmp_path)
    script = {
        "product": [spoken("Fine by me.", AGREE_V)] * 2,
        "architect": [spoken("ok", AGREE_V)] * 2,
        "tester": ["I would test the storage first."] * 2,
    }
    pipe = pipeline(
        tmp_path,
        repo,
        happy_engine(),
        story=MEETING,
        roles=MEETING_ROLES,
        meeting=replay_meeting(script, max_turns=8),
    )
    st = await pipe.run(text_card("avatars"))
    assert st.blocked is not None and "the meeting ended without an agreement" in st.blocked
    assert st.meeting is None and not (st.path / "meeting.md").exists()
    transcript = (st.path / "meetings" / "1.md").read_text()
    assert transcript.count("## turn ") == 8
    assert "## turn 3: tester (verdict_missing)" in transcript
    assert "I would test the storage first." in transcript
    assert "no proof plan has been posted yet" in transcript  # the checker's findings, as posted


async def test_with_amigos_off_the_split_is_the_agreement(tmp_path: Path) -> None:
    repo = repo_with_levels(tmp_path)

    pipe = pipeline(tmp_path, repo, happy_engine(), meeting=never)  # the helper's default: amigos off
    st = await pipe.run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review"
    assert st.agreement is not None and st.agreement["proof"] == [] and st.agreement["turns"] == 0
    assert [x["slug"] for x in st.agreement["layers"]] == ["storage", "profile"]
    assert not kinds(st.path / "events.jsonl", "amigos_turn")


async def test_a_story_saved_before_the_meeting_existed_resumes_with_its_split_as_the_agreement(
    tmp_path: Path,
) -> None:
    """A state at stage `layers` or later with no agreement is one from before P2: no meeting is held."""
    repo = repo_with_levels(tmp_path)
    first = pipeline(tmp_path, repo, happy_engine(), roles=MEETING_ROLES)
    card = text_card("avatars")
    await first.run(card)  # amigos off: it builds and publishes
    path = first.stories_dir / card.key
    state = json.loads((path / "state.json").read_text())
    for key in ("agreement", "meeting"):
        state.pop(key)
    (path / "state.json").write_text(json.dumps(state))
    again = pipeline(
        tmp_path,
        repo,
        FakeEngine({}),
        story=MEETING,
        roles=MEETING_ROLES,
        meeting=never,
    )
    st = await again.run(card)
    assert st.agreement is not None and st.agreement["proof"] == [] and st.stage == "review"


async def test_a_meeting_without_a_tester_role_stops_the_story_before_any_model_is_asked_to_attend(
    tmp_path: Path,
) -> None:
    repo = repo_with_levels(tmp_path)
    pipe = pipeline(tmp_path, repo, happy_engine(), story=MEETING, meeting=never)  # STORY_ROLES: no tester
    st = await pipe.run(text_card("avatars"))
    assert st.blocked is not None and "story.amigos is on, but the config has no 'tester' role" in st.blocked
    assert st.agreement is None and st.stage == "refine"


async def test_the_meeting_opens_with_the_repositorys_test_levels_and_the_testers_guidelines(
    tmp_path: Path,
) -> None:
    """The tester's view of the repository (D6) is in the opening message every agent reads; a router with
    no answer for the guideline (the chief router needs a hint) leaves the meeting going without one."""
    repo = repo_with_levels(tmp_path)
    lib = tmp_path / "qa"
    lib.mkdir()
    (lib / "unit.md").write_text("---\nsummary: Pure functions in isolation\n---\n# Unit\nKeep it fast.\n")
    opened: list[str] = []

    class Spy(FakeMeeting):
        async def run(self, opening: str, *args: Any, **kwargs: Any) -> Agreement:
            opened.append(opening)
            return await super().run(opening, *args, **kwargs)

    story = Story.from_dict(REWORDED_STORY)
    split = (
        Layer("storage", "Storage", "store.py", "Store.", ("AC1",)),
        Layer("profile", "Profile", "profile.py", "Show.", ("AC2",)),
    )
    proof = tuple(ProofTest.from_dict(t) for t in AGREED_PROOF["tests"])
    pipe = pipeline(
        tmp_path,
        repo,
        happy_engine(),
        story=MEETING,
        roles=MEETING_ROLES,
        meeting=lambda cfg, tools, on_turn: Spy(Agreement(story, split, proof, 4)),
    )
    pipe.cfg.roles["tester"].skills = lib
    st = await pipe.run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review", st.blocked
    [opening] = opened
    assert "unit: 1 test module" in opening and "integration: 1 test module" in opening
    assert "unit: Pure functions in isolation" in opening and "./check.sh" in opening
    assert "Keep it fast." not in opening  # the router had no answer: only the summaries
    assert kinds(st.path / "events.jsonl", "skill")[0]["chosen"] is None


async def test_run_is_plan_then_build_and_each_resumes_at_the_boundary(tmp_path: Path) -> None:
    """The planning lane's half ends at the stored layers (and agreement); the build lane's half starts there
    and is no resume (P5b, 2026-10-25)."""
    repo = repo_with_marker_tests(tmp_path)
    card = text_card("avatars")
    board = RecordingBoard()
    engine = happy_engine()
    planner = pipeline(tmp_path, repo, engine, board=board)
    st = await planner.plan(card)
    assert st.stage == "layers" and st.blocked is None and not st.built
    assert {r.role for r in engine.requests} == {"product", "architect"}  # no coder, reviewer or package
    assert [e["layers"] for e in kinds(st.path / "events.jsonl", "story_planned")] == [2]
    asked = len(engine.requests)
    again = await planner.plan(card)  # planned: nothing is asked again
    assert again.stage == "layers" and len(engine.requests) == asked
    builder = pipeline(tmp_path, repo, happy_engine(product=[], architect=PKGS), board=board)
    built = await builder.build(card)
    assert built.stage == "review" and built.blocked is None and [p["number"] for p in built.prs] == [11, 12]
    texts = [text for call, text in board.calls if call == "comment"]
    assert not any("resumed after" in text for text in texts)  # the hand-over is no resume
    assert [c for c in board.calls if c[0] == "move"] == [
        ("move", "building"),
        ("move", "building"),
        ("move", "review"),
    ]
    # and `run` is the same two halves in one go
    elsewhere = tmp_path / "again"
    elsewhere.mkdir()
    full = await pipeline(elsewhere, repo_with_marker_tests(elsewhere), happy_engine()).run(
        text_card("again")
    )
    assert full.stage == "review" and full.blocked is None


async def test_the_build_writes_decoding_gap_and_idle_at_its_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The planning lane reads these (P5b, D2): `gap` at the start and between layers, `decoding` while a
    layer's studio or an architect package is in flight, `idle` when the build ends; planning writes none."""
    from airlie.story import phase

    seen: list[str] = []
    real = phase.write_phase

    def record(stories_dir: Path, value: Any, **kw: Any) -> None:
        seen.append(value)
        real(stories_dir, value, **kw)

    monkeypatch.setattr(phase, "write_phase", record)
    monkeypatch.setattr("airlie.story.pipeline.write_phase", record)
    repo = repo_with_marker_tests(tmp_path)
    card = text_card("avatars")
    planner = pipeline(tmp_path, repo, happy_engine())
    await planner.plan(card)
    assert seen == []  # the planning half never touches the build lane's phase
    builder = pipeline(tmp_path, repo, happy_engine(product=[], architect=PKGS))
    st = await builder.build(card)
    assert st.stage == "review"
    # gap on entry; the packages, drafted together (decoding, gap); per layer the studio (decoding, gap);
    # idle at the end
    assert seen[0] == "gap" and seen[-1] == "idle"
    assert seen[1:-1] == ["decoding", "gap"] * 3 and set(seen) == {"gap", "decoding", "idle"}
    assert phase.read_phase(builder.stories_dir)[0] == "idle"


async def test_an_overrun_stage_is_an_event_and_one_note_and_never_a_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Budgets are recorded and warned, not enforced (P5b, D8): the layer ran over, the story still reaches
    review, the events say so once each and the issue is told once."""
    from airlie.config.schema import BudgetSpec
    from airlie.story.budgets import Figures, StageMeter

    big = Figures(5000, 500, 9999.0, 40, 1)
    monkeypatch.setattr(StageMeter, "fold", lambda self, stage: big if stage == "layer" else Figures())
    repo = repo_with_marker_tests(tmp_path)
    board = RecordingBoard()
    pipe = pipeline(tmp_path, repo, happy_engine(), board=board)
    pipe.cfg.line.budgets["layer"] = BudgetSpec(wall_s=600, tokens=100_000)
    st = await pipe.run(text_card("avatars"))
    assert st.stage == "review" and st.blocked is None  # nothing stopped
    events = st.path / "events.jsonl"
    assert len(kinds(events, "budget_warning")) == 1 and len(kinds(events, "budget_exceeded")) == 1
    [exceeded] = kinds(events, "budget_exceeded")
    assert (
        exceeded["stage"] == "layer" and exceeded["wall_s"] == 9999.0 and exceeded["budget"]["wall_s"] == 600
    )
    notes = [text for call, text in board.calls if call == "comment" and "stage cost" in text]
    assert len(notes) == 1 and "the layer stage cost 167 min" in notes[0]  # two layers ran, one note
    assert json.loads((st.path / "stages" / "layer.json").read_text())["state"] == "exceeded"


async def test_the_architects_package_brief_carries_the_files_its_layer_changes(tmp_path: Path) -> None:
    """Lever 2 (2026-10-27): the drafting conversation's turn for a layer carries the existing files that
    layer names, read at the base, so the architect writes from them instead of reading them by tool."""
    repo = repo_with_marker_tests(tmp_path)
    split = {
        "layers": [
            {**layer("storage", "Storage", "README.md", "AC1", "Store."), "files": [{"path": "README.md"}]},
            layer("profile", "Profile", "profile.py", "AC2", "Show."),
        ]
    }
    engine = happy_engine(architect=[fenced(split), *PKGS])
    st = await pipeline(tmp_path, repo, engine).run(text_card("avatars"))
    assert st.blocked is None, st.blocked
    first = [r for r in engine.requests if r.role == "architect"][1].messages[1].content
    assert "## The files this layer changes, as they are on its base" in first and "### README.md" in first
    assert "hi" in first.split("### README.md")[1][:40]
