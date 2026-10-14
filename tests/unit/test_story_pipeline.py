"""The story pipeline end to end on FakeEngine, a NullBoard and a recorded gh: refine → layers → each layer
test-first on its own branch → stack → review; stops, notes and resume."""

from __future__ import annotations

import io
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.report import RunReport
from airlie.router import ChiefRouter
from airlie.story.board import Board, BoardError, Card, Column, NullBoard, text_card
from airlie.story.briefs import pr_body
from airlie.story.model import Layer, Story
from airlie.story.pipeline import StoryPipeline, _reason
from airlie.story.publish import Publisher, PublishError
from airlie.story.state import StoryState
from airlie.teams.worktree import add_worktree
from airlie.topology.studio import StudioOrchestrator
from tests.unit.git_helpers import git
from tests.unit.test_fork_orchestrator import repo_with_marker_tests
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
    """Stands in for gh: records every command and answers `gh pr view` with numbered PRs."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.numbers: dict[str, int] = {}

    async def __call__(self, args: list[str], cwd: Path) -> str:
        self.calls.append(args)
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
) -> StoryPipeline:
    settings = {"trunk": git(repo, "branch", "--show-current"), **(story or {})}
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": settings})
    return StoryPipeline(
        cfg,
        board=board or NullBoard(cfg.observability.state_dir / "stories"),
        studio=lambda: studio(cfg, engine, router=ChiefRouter()),
        publisher=Publisher(run=gh or RecordedGh()),
        repo=repo,
        test_cmd="./check.sh",
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


def advance_origin(tmp_path: Path, repo: Path) -> str:
    """Give `repo` a bare `origin` (a local directory) whose trunk is one commit ahead of anything `repo` has
    fetched; returns that commit."""
    trunk = git(repo, "branch", "--show-current")
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", trunk, str(origin))
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", trunk)
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(origin), str(other))
    (other / "NEWS").write_text("news\n")
    git(other, "add", "NEWS")
    git(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "news")
    git(other, "push", "-q", "origin", f"HEAD:{trunk}")
    return git(other, "rev-parse", "HEAD")


class FailingGh(RecordedGh):
    """A gh whose `stack submit` fails with `failure`."""

    def __init__(self, failure: str) -> None:
        super().__init__()
        self.failure = failure

    async def __call__(self, args: list[str], cwd: Path) -> str:
        if args[:3] == ["gh", "stack", "submit"]:
            self.calls.append(args)
            raise PublishError(self.failure)
        return await super().__call__(args, cwd)


class ReadingGh(RecordedGh):
    """A gh that reads each `--body-file` the way gh does: as UTF-8 bytes."""

    def __init__(self) -> None:
        super().__init__()
        self.bodies: dict[str, str] = {}

    async def __call__(self, args: list[str], cwd: Path) -> str:
        if args[:3] == ["gh", "pr", "edit"]:
            self.bodies[args[3]] = Path(args[args.index("--body-file") + 1]).read_bytes().decode("utf-8")
        return await super().__call__(args, cwd)


@pytest.fixture
def ascii_locale(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Text files opened without an encoding are ASCII, as under LC_ALL=C with Python's UTF-8 mode off.
    pathlib asks io.text_encoding for the default, so that is what this replaces."""
    monkeypatch.setattr(io, "text_encoding", lambda encoding, stacklevel=2: encoding or "ascii")
    with pytest.raises(UnicodeEncodeError):  # the simulation holds, or a test that uses it proves nothing
        (tmp_path / "probe.txt").write_text("\u200b")


class RecordingBoard:
    """A board that keeps its moves and notes in memory (NullBoard writes them to a file)."""

    def __init__(self) -> None:
        self.moves: list[str] = []
        self.notes: list[str] = []

    async def card(self, ref: str) -> Card:
        raise BoardError("not needed here")

    async def next_card(self) -> Card | None:
        return None

    async def move(self, card: Card, column: Column) -> None:
        self.moves.append(column)

    async def comment(self, card: Card, text: str) -> None:
        self.notes.append(text)


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
    trail = [json.loads(x)["kind"] for x in (st.path / "events.jsonl").read_text().splitlines()]
    assert trail == [
        *("story_start", "stage", "run", "stage", "run", "stage"),  # intake, refine, layers
        *("fetch_failed", "layer_start", "layer_built", "layer_start", "layer_built", "stage"),  # build
        *("published", "stage", "stage", "story_done"),  # publish, review
    ]
    notes = (st.path / "notes.md").read_text()
    assert "layer 01/02 built: Storage (`./check.sh` passes)" in notes
    assert notes.index("pull/11") < notes.index("pull/12") and "only a human merges" in notes


async def test_a_pr_body_is_written_as_utf_8_whatever_the_locale(tmp_path: Path, ascii_locale: None) -> None:
    repo = repo_with_marker_tests(tmp_path)
    claim = "x" * 600  # cut to one line of 500, which ends in "…"
    finding = {"severity": "minor", "file": "PASS", "line": 1, "claim": claim, "evidence": "x"}
    wordy = FakeReply(text=json.dumps({"verdict": "approve", "findings": [finding]}))
    gh, board = ReadingGh(), RecordingBoard()
    st = await pipeline(tmp_path, repo, happy_engine(reviewer=[wordy, APPROVE]), gh, board=board).run(ISSUE)
    assert st.blocked is None and st.stage == "review"  # a body written in ASCII would have stopped it here
    assert board.moves == ["building", "review"]
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
    assert kinds(st.path / "events.jsonl", "layer_start")[0]["base"] == f"origin/{trunk}"


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


async def test_a_failed_fetch_event_masks_url_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)

    async def refuses(args: list[str], cwd: Path) -> str:
        url = "https://user:secret@example.com/o/r.git"
        raise PublishError(f"`git fetch -q origin main` exited 128: fatal: repository '{url}/' not found")

    monkeypatch.setattr("airlie.story.pipeline.run_cmd", refuses)
    pipe = pipeline(tmp_path, repo, FakeEngine())
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
def test_a_stop_reason_masks_the_user_and_password_of_any_url(raw: str, masked: str) -> None:
    assert _reason(RuntimeError(raw)) == f"RuntimeError: {masked}"
    said = subprocess.CalledProcessError(128, ["git", "ls-remote", raw], stderr=f"fatal: {raw}")
    assert _reason(said) == f"`git ls-remote {masked}` exited 128: fatal: {masked}"


def test_masking_a_long_unbroken_run_of_letters_takes_no_time_at_all() -> None:
    started = time.perf_counter()
    assert _reason(RuntimeError("a" * 200_000)).startswith("RuntimeError: aaa")
    assert time.perf_counter() - started < 1.0  # a pattern before the "://" made this quadratic: seconds


def test_a_password_is_masked_before_the_reason_is_cut_to_800_characters() -> None:
    password = "pw-0123456789-abcdef"
    tail = "@example.com/o/r.git "
    # cut first, the last 800 characters would begin 12 characters inside the password
    stderr = f"https://user:{password}{tail}" + "y" * (800 - len(tail) - 12)
    reason = _reason(subprocess.CalledProcessError(128, ["git", "x"], stderr=stderr))
    assert password[-12:] not in reason and "https://***@example.com/o/r.git" in reason


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
    [failed] = kinds(st.path / "events.jsonl", "board_error")
    assert failed["error"] == "BoardError: jira is unreachable"
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
