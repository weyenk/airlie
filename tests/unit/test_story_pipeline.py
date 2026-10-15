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

from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.report import RunReport
from airlie.router import ChiefRouter
from airlie.story.board import NOTE_PREFIX, Board, BoardError, Card, Column, NullBoard, note, text_card
from airlie.story.briefs import pr_body
from airlie.story.github_app import GitHubApp
from airlie.story.model import Layer, Story
from airlie.story.pipeline import StoryPipeline, _layers_note, _reason, _running
from airlie.story.publish import Publisher, PublishError, run_cmd
from airlie.story.state import STAGES, StoryState
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


class RecordedGh:
    """Stands in for gh: records every command and its environment and answers `gh pr view` with numbered PRs.
    git (the trunk fetch) runs for real, unless `real_git` is off: as the bot, the remote is GitHub."""

    def __init__(self, *, real_git: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.numbers: dict[str, int] = {}
        self.real_git = real_git

    async def __call__(self, args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
        self.calls.append(args)
        self.envs.append(dict(env))
        if args[0] == "git":
            if not self.real_git:
                raise PublishError(f"`{' '.join(args)}` exited 128: no network in a unit test")
            return await run_cmd(args, cwd, env)
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
    test_cmd: str = "./check.sh",
    app: GitHubApp | None = None,
    roles: dict[str, tuple[str, list[str] | None]] = STORY_ROLES,
) -> StoryPipeline:
    settings = {"trunk": git(repo, "branch", "--show-current"), **(story or {})}
    cfg = cfg_for(tmp_path, roles=roles, extra={"story": settings})
    return StoryPipeline(
        cfg,
        board=board or NullBoard(cfg.observability.state_dir / "stories"),
        studio=lambda: studio(cfg, engine, router=ChiefRouter()),
        publisher=Publisher(run=gh or RecordedGh(), app=app),
        repo=repo,
        test_cmd=test_cmd,
    )


def kinds(path: Path, kind: str) -> list[dict[str, Any]]:
    return [e for e in (json.loads(x) for x in path.read_text().splitlines()) if e["kind"] == kind]


def happy_engine(**scripts: list[FakeReply]) -> FakeEngine:
    """The replies of a story whose two layers build first time; a keyword replaces one role's script."""
    base = {
        "product": [STORY],
        "architect": [LAYERS],
        "qa": [DONE, DONE],
        "coder": [WRITE_PASS, RUN, DONE, NOTE, DONE],
        "reviewer": [APPROVE, APPROVE],
    }
    return FakeEngine({**base, **scripts})


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

    async def comment(self, card: Card, text: str) -> None:
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

    async def comment(self, card: Card, text: str) -> None:
        if self.refuses("comment", text):
            raise BoardError(self.message)
        await super().comment(card, text)


class DownBoard(NullBoard):
    """A board that cannot be reached."""

    async def move(self, card: Card, column: Column) -> None:
        raise BoardError("jira is unreachable")

    async def comment(self, card: Card, text: str) -> None:
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


async def test_a_request_becomes_two_stacked_layers_and_the_card_reaches_review(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    trunk = git(repo, "branch", "--show-current")
    engine = FakeEngine(
        {
            "product": [STORY],
            "architect": [LAYERS],
            "qa": [DONE, DONE],
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
    assert git(repo, "show", "--name-only", "--format=", B1) == "PASS"
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


async def test_a_failing_layer_stops_and_a_resume_rebuilds_only_that_layer(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    card = text_card("Let users upload an avatar")
    first = FakeEngine(
        {
            "product": [STORY],
            "architect": [LAYERS],
            "qa": [DONE],
            "coder": [NOTE, DONE],  # a change, but not the one the tests need
            "reviewer": [APPROVE],
        }
    )
    st = await pipeline(tmp_path, repo, first).run(card)
    assert st.stage == "layers" and st.blocked is not None and "./check.sh` fails" in st.blocked
    assert st.attempt is not None and git(repo, "rev-parse", "--verify", B1)  # kept for inspection
    again = FakeEngine(
        {"qa": [DONE, DONE], "coder": [WRITE_PASS, RUN, DONE, NOTE, DONE], "reviewer": [APPROVE, APPROVE]}
    )
    st = await pipeline(tmp_path, repo, again).run(card)
    assert st.blocked is None and st.stage == "review"
    assert not [r for r in again.requests if r.role in ("product", "architect")]  # finished stages skipped
    assert git(repo, "show", "--name-only", "--format=", B1) == "PASS"  # rebuilt from the trunk
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    notes = (st.path / "notes.md").read_text()  # out of blocked while it builds again
    assert "resumed after 'layers'" in notes
    assert notes.rindex("moved to building") > notes.index("moved to blocked")


async def test_a_layer_that_changes_nothing_stops_the_story(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine(
        {"product": [STORY], "architect": [LAYERS], "qa": [DONE], "coder": [DONE], "reviewer": [APPROVE]}
    )
    st = await pipeline(tmp_path, repo, engine).run(text_card("avatars"))
    assert st.blocked is not None and "changed nothing" in st.blocked


async def test_a_git_failure_stops_the_story_with_gits_own_words(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    git(repo, "branch", B1)  # a leftover branch holds the first layer's name
    engine = FakeEngine({"product": [STORY], "architect": [LAYERS]})
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
    assert len(st.runs) == 4  # refine, layers, then one run per layer
    log = [json.loads(x) for x in (st.path / "events.jsonl").read_text().splitlines()]
    assert [e["kind"] for e in log] == [
        *("story_start", "stage", "fetch_failed"),  # intake, then the trunk (no remote here), once
        *("run", "stage", "run", "stage"),  # refine, layers
        *("layer_start", "layer_built", "layer_start", "layer_built", "stage"),  # build
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
    engine = happy_engine(architect=[leaves_ac2, LAYERS])
    st = await pipeline(tmp_path, repo, engine).run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review"
    [retry] = kinds(st.path / "events.jsonl", "retry")
    assert retry["stage"] == "layers" and "no layer covers acceptance ['AC2']" in retry["error"]
    asked = [r for r in engine.requests if r.role == "architect"]
    assert len(asked) == 2 and "could not be used" in asked[1].messages[1].content
    assert "no layer covers acceptance ['AC2']" in asked[1].messages[1].content


async def test_layer_one_builds_on_the_fetched_remote_trunk(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    trunk = git(repo, "branch", "--show-current")
    news = advance_origin(tmp_path, repo)
    mine = git(repo, "rev-parse", trunk)
    st = await pipeline(tmp_path, repo, happy_engine()).run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review"
    git(repo, "merge-base", "--is-ancestor", news, B1)  # fails unless layer 01 sits on the fetched trunk
    assert git(repo, "show", "--name-only", "--format=", B1) == "PASS"  # the layer's own change, not NEWS
    assert git(repo, "rev-parse", trunk) == mine  # the person's own trunk is not moved
    assert kinds(st.path / "events.jsonl", "fetch_failed") == []
    assert st.base == {"ref": f"origin/{trunk}", "sha": news}  # resolved once, and kept
    [first, _] = kinds(st.path / "events.jsonl", "layer_start")
    assert (first["base"], first["base_sha"]) == (f"origin/{trunk}", news)


async def test_the_trunk_ref_is_the_remote_branch_when_there_is_one_else_the_local_trunk(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    trunk = git(repo, "branch", "--show-current")
    pipe = pipeline(tmp_path, repo, FakeEngine())
    st = StoryState.open(tmp_path / "s", text_card("x"), pipe.repo)
    events = st.path / "events.jsonl"
    assert await pipe._trunk_ref(st) == trunk  # no remote: the fetch fails, which is an event, not a stop
    [failed] = kinds(events, "fetch_failed")
    assert failed["remote"] == "origin" and failed["trunk"] == trunk and "origin" in failed["error"]
    advance_origin(tmp_path, repo)
    assert await pipe._trunk_ref(st) == f"origin/{trunk}"
    # fetched, but with no remote-tracking branch to build on (no fetch refspec): the local trunk, no event
    git(repo, "update-ref", "-d", f"refs/remotes/origin/{trunk}")
    git(repo, "config", "--unset-all", "remote.origin.fetch")
    assert await pipe._trunk_ref(st) == trunk
    assert len(kinds(events, "fetch_failed")) == 1


async def test_a_failed_fetch_event_masks_url_credentials(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)

    class Refuses(RecordedGh):
        async def __call__(self, args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
            url = "https://user:secret@example.com/o/r.git"
            raise PublishError(f"`git fetch -q origin main` exited 128: fatal: repository '{url}/' not found")

    pipe = pipeline(tmp_path, repo, FakeEngine(), Refuses())
    st = StoryState.open(tmp_path / "s", text_card("x"), pipe.repo)
    await pipe._trunk_ref(st)
    [failed] = kinds(st.path / "events.jsonl", "fetch_failed")
    assert "https://***@example.com/o/r.git/" in failed["error"] and "secret" not in failed["error"]


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
    assert st.blocked == masked and board.notes[0].startswith(note(f"stopped after 'new': {masked}\n"))
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
    stopped = f"stopped after 'new': {st.blocked}\nResume: the same `task story -- --text ...` command."
    assert board.notes == [note(stopped)]  # the board is told what the state and the event hold
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
    for engine in (FakeEngine({"product": [STORY], "architect": [LAYERS]}), FakeEngine()):  # then a resume
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
    assert [(e["call"], e["error"]) for e in failed] == [  # the note and the move are each tried
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
    assert "nonesuch" in notes and "Traceback" not in notes  # the note is the reason, not the traceback
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
            "architect": [LAYERS],
            "qa": [DONE, DONE],
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
    engine = happy_engine(product=[look, STORY], architect=[look, LAYERS])
    st = await pipeline(tmp_path, repo, engine, story={"trunk": trunk}).run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review"
    for role in ("product", "architect"):
        listing = (
            [r for r in engine.requests if r.role == role][1].messages[-1].content
        )  # what list_dir showed
        assert "README.md" in listing and "check.sh" in listing and "feature_only.txt" not in listing
    assert st.base is not None and st.base["sha"] == git(repo, "rev-parse", trunk)
    assert {e["base_sha"] for e in kinds(st.path / "events.jsonl", "run")} == {st.base["sha"]}
    assert "feature_only.txt" not in git(
        repo, "ls-tree", "-r", "--name-only", B2
    )  # nor do the layers build on it
    assert git(repo, "branch", "--show-current") == "feature" and git(repo, "status", "--porcelain") == ""


async def test_a_resume_builds_on_the_base_the_story_started_from_not_on_a_newer_one(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    trunk = git(repo, "branch", "--show-current")
    news = advance_origin(tmp_path, repo)
    card = text_card("avatars")
    broken = happy_engine(qa=[DONE], coder=[NOTE, DONE], reviewer=[APPROVE])  # layer 01's tests stay red
    st = await pipeline(tmp_path, repo, broken).run(card)
    assert st.stage == "layers" and st.blocked is not None
    assert st.base == {"ref": f"origin/{trunk}", "sha": news}
    later = push_news(tmp_path, trunk, "LATER")  # the remote moves on while the story is stopped
    again = FakeEngine(
        {"qa": [DONE, DONE], "coder": [WRITE_PASS, RUN, DONE, NOTE, DONE], "reviewer": [APPROVE, APPROVE]}
    )
    st = await pipeline(tmp_path, repo, again).run(card)
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
    assert st.blocked is not None and st.blocked.startswith("the trunk 'nonesuch' does not exist in ")
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
    assert len(st.runs) == 5  # the cancelled run is on record with the four that finished


async def test_a_story_cancelled_while_a_layer_builds_keeps_the_layer_and_resumes_after_the_last_stage(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    card = text_card("avatars")
    started = asyncio.Event()
    board = RecordingBoard()
    studio = studio_failing_from(2, hangs(started))  # refine and layers run, the first layer hangs
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
    st = await pipeline(tmp_path, repo, happy_engine(product=[], architect=[]), board=again).run(card)
    assert again.calls[:2] == [("move", "building"), ("comment", note("resumed after 'layers'"))]
    assert st.blocked is None and st.stage == "review" and worktrees(repo) == 1
    assert git(repo, "show", "--name-only", "--format=", B1) == "PASS"  # rebuilt from the base


@pytest.mark.parametrize("trouble", ["slow", "no note", "no move"])
async def test_a_board_in_trouble_neither_holds_up_nor_changes_a_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trouble: str
) -> None:
    monkeypatch.setattr("airlie.story.pipeline.CANCEL_NOTE_S", 0.05)
    release = asyncio.Event()  # a slow board answers when this is set, which the cancel does not wait for

    class SlowBoard(RecordingBoard):
        async def comment(self, card: Card, text: str) -> None:
            if "stopped by a signal" in text:
                await release.wait()
            await super().comment(card, text)

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
        async def comment(self, card: Card, text: str) -> None:
            if hangs_on == "the note" and "stopped after" in text:
                telling.set()
                await asyncio.sleep(3600)
            await super().comment(card, text)

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
    st = await pipeline(tmp_path, repo, happy_engine(), studio=studio_failing_from(2, crash)).run(
        text_card("avatars")
    )
    assert st.blocked == "RuntimeError: the engine fell over" and st.stage == "layers"
    assert len(st.runs) == 3 and st.attempt is not None  # refine, layers, and the layer that died
    assert [e["studio_run"] for e in kinds(st.path / "events.jsonl", "layer_start")] == st.runs[2:]
    assert json.loads((st.path / "state.json").read_text())["runs"] == st.runs


# ---- publishing as a GitHub App's bot ----

PLANT = "git config remote.origin.url git@github.com:evil/other.git && ./check.sh"
"""A test command that redirects the remote from inside the build: model-written code runs in the test gate,
and the repository's git configuration is shared by every worktree."""


def on_github(tmp_path: Path) -> Path:
    repo = repo_with_marker_tests(tmp_path)
    git(repo, "remote", "add", "origin", "git@github.com:o/r.git")
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
    github, gh = FakeGitHub(), RecordedGh(real_git=False)  # no fetch reaches GitHub from a unit test
    pipe = pipeline(tmp_path, repo, happy_engine(), gh, app=fake_app(tmp_path, github), test_cmd=PLANT)
    st = await pipe.run(text_card("avatars"))
    assert st.blocked is None and st.stage == "review"
    # the model redirected the remote during the build; every token is still for o/r alone
    assert git(repo, "config", "remote.origin.url") == "git@github.com:evil/other.git"
    assert github.asked("GET").count("/api/repos/o/r/installation") == 1
    assert "/api/repos/evil/other/installation" not in github.asked("GET")
    assert github.minted_for() == [["r"]] * 8  # the fetch, the bot's identity, then six publish steps
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
    assert [e["GH_TOKEN"] for e in gh.envs] == [github.tokens[i] for i in (0, 2, 2, 3, 4, 5, 6, 7)]
    assert all("SSH_AUTH_SOCK" not in e and e["GIT_SSH_COMMAND"] == "false" for e in gh.envs)
    # the layers are the bot's commits, author and committer
    bot = "airlie-bot-test[bot]|9919+airlie-bot-test[bot]@users.noreply.github.com"
    for branch in (B1, B2):
        assert git(repo, "log", "-1", "--format=%an|%ae%n%cn|%ce", branch).splitlines() == [bot, bot]
    assert git(repo, "log", "--format=%s", f"{trunk}..{B1}") == "Add avatar upload: Storage (01/02)"
    # and no token is kept: not in the state, an event, a PR body, a note, or the repository's config
    kept = files_under(st.path) + (repo / ".git" / "config").read_text()
    assert not [t for t in [*github.tokens, *github.jwts] if t in kept]
    assert kinds(st.path / "events.jsonl", "fetch_failed")[0]["error"].endswith("no network in a unit test")


async def test_as_the_bot_a_token_a_command_prints_reaches_no_error_state_event_or_jira_comment(
    tmp_path: Path,
) -> None:
    """gh and git can echo what they authenticate with; run_cmd masks the step's token before a PublishError
    carries it to the stop reason, the state, the events and the issue."""
    repo = on_github(tmp_path)
    github = FakeGitHub()

    class Leaky(RecordedGh):
        async def __call__(self, args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
            if args[0] == "git":
                said = "fatal: unable to access https://x-access-token:$GH_TOKEN@github.com/o/r.git/"
            elif args[:3] == ["gh", "stack", "submit"]:
                said = "remote: Permission to o/r.git denied to token $GH_TOKEN"
            else:
                return await super().__call__(args, cwd, env)
            self.calls.append(args)
            return await run_cmd(["sh", "-c", f'echo "{said}" >&2; exit 1'], cwd, env)

    jira = FakeJira()
    st = await pipeline(
        tmp_path, repo, happy_engine(), Leaky(), board=jira_board_for(jira), app=fake_app(tmp_path, github)
    ).run(ISSUE)
    assert st.stage == "build" and st.blocked is not None
    assert "denied to token ***" in st.blocked
    [failed] = kinds(st.path / "events.jsonl", "fetch_failed")
    assert "x-access-token:***@github.com" in failed["error"] or "https://***@github.com" in failed["error"]
    comments = [r.content.decode() for r in jira.requests if r.url.path.endswith("/comment")]
    assert any("denied to token ***" in c for c in comments)
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


async def test_a_bot_that_cannot_be_found_stops_the_story_before_a_layer_is_built(tmp_path: Path) -> None:
    repo = on_github(tmp_path)
    engine = happy_engine()
    app = fake_app(tmp_path, FakeGitHub(installed=("o/elsewhere",)))
    st = await pipeline(tmp_path, repo, engine, RecordedGh(real_git=False), app=app).run(text_card("avatars"))
    assert st.stage == "layers" and st.blocked is not None
    assert "the GitHub App airlie-bot-test is not installed on o/r" in st.blocked
    assert {r.role for r in engine.requests} == {"product", "architect"}  # no layer was started
    assert st.built == {} and st.attempt is None and git(repo, "branch", "--list", f"{B1}*") == ""


async def test_a_resumed_story_pins_its_repository_before_the_runs_first_model_works(tmp_path: Path) -> None:
    """A resume has its base already, so it fetches nothing first; product's turn comes before any layer. A
    product with a shell redirects the remote in it, and the bot still publishes to o/r alone."""
    repo = on_github(tmp_path)
    trunk = git(repo, "branch", "--show-current")
    card = text_card("avatars")
    stories = pipeline(tmp_path, repo, FakeEngine()).stories_dir
    st = StoryState.open(stories / card.key, card, repo.resolve())
    st.base = {"ref": trunk, "sha": git(repo, "rev-parse", trunk)}
    st.finish("intake")  # started, and stopped before refine
    redirect = "git config remote.origin.url git@github.com:evil/other.git"
    plant = FakeReply(tool_calls=[ToolCall("p", "shell", json.dumps({"cmd": redirect}))])
    roles = {**STORY_ROLES, "product": (STORY_ROLES["product"][0], ["read_file", "list_dir", "shell"])}
    github = FakeGitHub(installed=("o/r", "evil/other"))  # the app could mint for either
    gh = RecordedGh(real_git=False)
    pipe = pipeline(
        tmp_path, repo, happy_engine(product=[plant, STORY]), gh, app=fake_app(tmp_path, github), roles=roles
    )
    st = await pipe.run(card)
    assert st.blocked is None and st.stage == "review"
    assert (
        git(repo, "config", "remote.origin.url") == "git@github.com:evil/other.git"
    )  # the product's turn ran
    assert gh.calls[0][:3] == ["gh", "stack", "view"]  # nothing was fetched before it
    assert "/api/repos/evil/other/installation" not in github.asked("GET")
    assert github.minted_for() == [["r"]] * len(github.tokens)
