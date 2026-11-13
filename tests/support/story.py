"""Shared helpers extracted from tests/unit/test_story_pipeline.py for tests.support.story."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml
from autogen_ext.models.replay import ReplayChatCompletionClient

from airlie.config import RoleSpec
from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.router import ChiefRouter
from airlie.story.amigos import Agreement, Meeting
from airlie.story.board import Board, BoardError, Card, Column, NullBoard, text_card
from airlie.story.epic_plan import PlannedStory, ScopedPath, render_story_card
from airlie.story.github_app import GitHubApp
from airlie.story.model import Criterion, EpicHeader, Layer, Story
from airlie.story.pipeline import StoryPipeline
from airlie.story.publish import Publisher, PublishError, run_cmd
from airlie.topology.studio import StudioOrchestrator
from tests.support.git import git, repo_with_marker_tests
from tests.support.studio import APPROVE, DONE, ROLES, RUN, WRITE_PASS, cfg_for

# Re-exported for test modules that imported these from test_story_pipeline before the move.
__all__ = ["repo_with_marker_tests", "text_card"]

WORKFLOWS = Path(__file__).resolve().parents[2] / "config" / "workflows"

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
    pinned_base: str | None = None,
    composer: bool = False,
) -> StoryPipeline:
    """A pipeline over `repo`. `test_cmd` is a person's (None: the gate of the repository's AGENTS.md), and
    `github_repo` the repository an issue's label names (None: a written request's, the remote as written)."""
    settings = {
        "trunk": git(repo, "branch", "--show-current"),
        "plan_round_steps": 0,  # one go, no survey: the rounds and the survey have tests of their own
        "survey": False,
        "layer_attempts": 1,  # a layer that fails stops the story; re-planning has tests of its own
        "amigos": {"enabled": False},  # the meeting has tests of its own (below)
        "composer": composer,
        **(story or {}),
    }
    # the done-check is off unless a test asks for it: it costs the coder one scripted reply per layer
    extra: dict[str, object] = {"story": settings}
    if composer:  # the shipped story and layer workflows, as airlie.yaml includes them
        extra["workflows"] = {
            name: spec
            for part in ("story", "layer")
            for name, spec in yaml.safe_load((WORKFLOWS / f"{part}.yaml").read_text())["workflows"].items()
        }
    cfg = cfg_for(tmp_path, roles=roles, extra=extra, done_check=done_check)
    return StoryPipeline(
        cfg,
        board=board or NullBoard(cfg.observability.state_dir / "stories"),
        studio=lambda: studio(cfg, engine, router=ChiefRouter()),
        publisher=Publisher(run=gh or RecordedGh(), app=app),
        repo=repo,
        test_cmd=test_cmd,
        github_repo=github_repo,
        meeting=meeting,
        pinned_base=pinned_base,
    )


def neutral(text: str, root: Path, sha: str) -> Any:
    """`text` (JSON) with this run's directory and base commit spelled the same for every run."""
    return json.loads(
        text.replace(str(root.resolve()), "<root>").replace(str(root), "<root>").replace(sha, "<sha>")
    )


def kinds_all(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text().splitlines()]


def kinds(path: Path, kind: str) -> list[dict[str, Any]]:
    return [e for e in (json.loads(x) for x in path.read_text().splitlines()) if e["kind"] == kind]


def happy_engine(**scripts: list[FakeReply]) -> FakeEngine:
    """The replies of a story whose two layers build first time; a keyword replaces one role's script."""
    scripts = dict(scripts)
    build = scripts.pop("coder", [WRITE_PASS, RUN, DONE, NOTE, DONE])
    plan = scripts.pop("architect", [LAYERS, *PKGS])
    base = {
        "product": [STORY],
        "architect": list(plan) + list(build),
        "reviewer": [APPROVE, APPROVE],
    }
    return FakeEngine({**base, **scripts})


def worktrees(repo: Path) -> int:
    return git(repo, "worktree", "list", "--porcelain").count("worktree ")


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


SATISFIED = fenced({"type": "satisfied", "by": ["01"], "why": "the profile already shows a stored avatar"})

CUT_THINKING = FakeReply(reasoning="Let me read the files once more. ", finish_reason="length")


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


SURVEY_NOTES = FakeReply(text="- store.py:10 Store class\n- tests live in tests/unit/test_store.py")

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


MEETING_ROLES = STORY_ROLES

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
    "architect": [
        spoken("Files named.", block(AGREED_SPLIT), AGREE_V),
        spoken("The proof.", block(AGREED_PROOF), AGREE_V),
        spoken("ok", AGREE_V),
    ],
}
