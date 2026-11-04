"""A story started over (`airlie.story.restart`): its open PRs closed and their remote branches deleted, its
local layer branches and kept worktrees removed, its record archived (all of it, or a copy when it starts
over from a later stage and keeps the earlier ones), a note on the card and the card back in approved, so the
line plans it again. A story running now, or with a merged PR, is refused with nothing changed; a re-run
after a partial failure finishes the job. On the marker-test repository, with a local board and gh faked."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from airlie.board import BoardStore
from airlie.engine import FakeEngine
from airlie.story import factory
from airlie.story.board import BoardError, Card
from airlie.story.local_board import LocalBoard
from airlie.story.model import StoryRunning
from airlie.story.publish import Publisher, PublishError
from airlie.story.restart import FRESH, Restarted, RestartError, restart
from airlie.story.state import StoryState, lane_lock
from tests.unit.git_helpers import git
from tests.unit.test_fork_orchestrator import repo_with_marker_tests
from tests.unit.test_story_pipeline import (
    LAYERS,
    NOTE,
    PKG2,
    PKGS,
    STORY,
    RecordedGh,
    happy_engine,
    kinds,
    pipeline,
    worktrees,
)
from tests.unit.test_story_restack import trunk_commit
from tests.unit.test_studio_orchestrator import APPROVE, DONE, RUN, WRITE_PASS, cfg_for


class GitHubPrs(RecordedGh):
    """RecordedGh with each PR's state kept: `gh pr view N --json state` answers it (OPEN until told
    otherwise) and `gh pr close N` makes it CLOSED; a number in `fail_close` fails its first close."""

    def __init__(self, states: Mapping[int, str] | None = None, fail_close: set[int] | None = None) -> None:
        super().__init__()
        self.states = dict(states or {})
        self.fail_close = set(fail_close or ())

    async def __call__(self, args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
        if args[:3] == ["gh", "pr", "view"] and args[4:] == ["--json", "state"]:
            self.calls.append(args)
            return json.dumps({"state": self.states.get(int(args[3]), "OPEN")})
        if args[:3] == ["gh", "pr", "close"]:
            self.calls.append(args)
            n = int(args[3])
            if n in self.fail_close:
                self.fail_close.discard(n)
                raise PublishError(f"`gh pr close {n}` exited 1: HTTP 502")
            self.states[n] = "CLOSED"
            return ""
        return await super().__call__(args, cwd, env)

    def closes(self) -> list[int]:
        return [int(c[3]) for c in self.calls if c[:3] == ["gh", "pr", "close"]]


def story_card(tmp_path: Path, status: str = "approved") -> tuple[LocalBoard, BoardStore, str]:
    store = BoardStore(tmp_path / "state" / "board.sqlite")
    item = store.create(level="story", title="Let users upload an avatar")
    store.update(item.key, status=status)  # type: ignore[arg-type]
    return LocalBoard(store), store, item.key


def github_repo(tmp_path: Path) -> Path:
    """The marker-test repository with a github.com remote (the fake gh mirrors its trunk fetch)."""
    repo = repo_with_marker_tests(tmp_path)
    git(repo, "remote", "add", "origin", "https://github.com/o/r.git")
    return repo


async def published(tmp_path: Path) -> tuple[Path, LocalBoard, BoardStore, Card, StoryState]:
    """A story whose two layers are built and published as PRs #11 and #12, in review."""
    repo = github_repo(tmp_path)
    board, store, key = story_card(tmp_path)
    card = await board.card(key)
    st = await pipeline(tmp_path, repo, happy_engine(), board=board).run(card)
    assert st.stage == "review" and [p["number"] for p in st.prs] == [11, 12]
    return repo, board, store, card, st


async def stopped(tmp_path: Path) -> tuple[Path, LocalBoard, BoardStore, Card, StoryState]:
    """A story whose layer 01 is built and whose layer 02 stopped (it changed nothing): its branch and
    worktree are kept, as an attempt."""
    repo = github_repo(tmp_path)
    board, store, key = story_card(tmp_path)
    card = await board.card(key)
    engine = FakeEngine(
        {
            "product": [STORY],
            "architect": [LAYERS, *PKGS],
            "coder": [WRITE_PASS, RUN, DONE, DONE],
            "reviewer": [APPROVE, APPROVE],
        }
    )
    st = await pipeline(tmp_path, repo, engine, board=board).run(card)
    assert "01" in st.built and st.blocked is not None and st.attempt is not None
    return repo, board, store, card, st


async def start_over(
    tmp_path: Path, card: Card, board: LocalBoard, gh: RecordedGh, point: str = FRESH, **kw: Any
) -> Restarted:
    """restart, as the person noah, with a pipeline over the repository to replay the kept layers."""
    repo = tmp_path / "repo"
    kw.setdefault("replay", pipeline(tmp_path, repo, FakeEngine({}), gh, board=board).replay_layers)
    return await restart(
        card,
        point,
        board=board,
        publisher=Publisher(run=gh),
        stories_dir=tmp_path / "state" / "stories",
        trunk=git(repo, "branch", "--show-current"),
        by="noah",
        **kw,
    )


def branches(repo: Path) -> set[str]:
    return set(git(repo, "branch", "--format=%(refname:short)").split())


def archived(tmp_path: Path, key: str) -> list[Path]:
    return sorted((tmp_path / "state" / "archive" / "stories").glob(f"{key}-*"))


async def test_a_published_story_starts_fresh_from_the_trunk(tmp_path: Path) -> None:
    repo, board, store, card, st = await published(tmp_path)
    layer_branches = {str(b["branch"]) for b in st.built.values()}
    trunk = git(repo, "branch", "--show-current")
    gh = GitHubPrs()
    done = await start_over(tmp_path, card, board, gh)
    # the PRs: each looked at before any is closed, then closed with a word and their remote branches deleted
    views = [i for i, c in enumerate(gh.calls) if c[:3] == ["gh", "pr", "view"]]
    closes = [i for i, c in enumerate(gh.calls) if c[:3] == ["gh", "pr", "close"]]
    assert len(views) == 2 and max(views) < min(closes)
    said = f"started fresh from {trunk}; this PR is superseded"
    for n in (11, 12):
        assert ["gh", "pr", "close", str(n), "--comment", said, "--delete-branch", "-R", "o/r"] in gh.calls
    assert not any("merge" in a for c in gh.calls for a in c)
    # the clone: no layer branch, no worktree but the checkout itself
    assert not layer_branches & branches(repo) and trunk in branches(repo)
    assert worktrees(repo) == 1
    # the record: moved aside whole, with what it did on record
    assert not (tmp_path / "state" / "stories" / card.key).exists()
    [folder] = archived(tmp_path, card.key)
    assert folder.name.startswith(f"{card.key}-fresh-")
    assert (folder / "state.json").is_file() and (folder / "events.jsonl").is_file()
    [event] = kinds(folder / "events.jsonl", "restarted")
    assert event["from"] == FRESH and event["closed"] == [11, 12]
    # the card: a note, then approved, moved by the person who asked
    sha = git(repo, "rev-parse", trunk)
    item = store.get(card.key)
    assert item.status == "approved" and store.moves(card.key)[-1].by == "noah"
    last = item.comments[-1].text
    assert f"started fresh from {trunk} @ {sha[:12]}" in last and str(folder) in last
    assert done.closed == [11, 12] and done.archive == str(folder)
    assert set(done.branches) == layer_branches
    assert done.to_dict()["closed"] == [11, 12]


async def test_a_fresh_story_is_planned_again_from_the_cards_text(tmp_path: Path) -> None:
    """With its record gone the next run is a first run: intake, refine from the card as it reads now."""
    repo, board, store, card, _ = await published(tmp_path)
    await start_over(tmp_path, card, board, GitHubPrs())
    store.update(card.key, description="now with a crop step")
    card = await board.card(card.key)
    st = await pipeline(tmp_path, repo, happy_engine(), board=board).run(card)
    assert st.stage == "review" and st.blocked is None
    stages = [e["stage"] for e in kinds(st.path / "events.jsonl", "stage")]
    assert stages[:2] == ["intake", "refine"]


async def test_a_story_with_a_merged_pr_is_refused_and_nothing_changes(tmp_path: Path) -> None:
    repo, board, store, card, st = await published(tmp_path)
    before = (st.path / "state.json").read_text()
    had = branches(repo)
    gh = GitHubPrs({12: "MERGED"})
    with pytest.raises(RestartError, match=r"#12 .*merged"):
        await start_over(tmp_path, card, board, gh)
    assert gh.closes() == []
    assert (st.path / "state.json").read_text() == before and branches(repo) == had
    assert archived(tmp_path, card.key) == [] and store.get(card.key).status == "review"


async def test_a_story_its_serve_job_is_running_is_refused(tmp_path: Path) -> None:
    repo, board, store, card, st = await published(tmp_path)
    gh = GitHubPrs()
    with pytest.raises(StoryRunning, match="running"):
        await start_over(tmp_path, card, board, gh, running=lambda job: job == f"story-{card.key}")
    assert gh.calls == [] and (st.path / "state.json").is_file()


async def test_a_story_a_lane_is_running_is_refused(tmp_path: Path) -> None:
    repo, board, store, card, st = await published(tmp_path)
    lanes = tmp_path / "state" / "line" / "lanes"
    lanes.mkdir(parents=True)
    (lanes / "build.json").write_text(json.dumps({"key": card.key, "what": "build"}))
    gh = GitHubPrs()
    with lane_lock(tmp_path / "state" / "stories", "build") as held:
        assert held
        with pytest.raises(StoryRunning, match="build lane"):
            await start_over(tmp_path, card, board, gh)
    assert gh.calls == [] and (st.path / "state.json").is_file()
    # a record the lane's holder left behind, with the lock free, is no running story
    done = await start_over(tmp_path, card, board, gh)
    assert done.closed == [11, 12]


async def test_a_rerun_after_a_partial_failure_finishes_the_job(tmp_path: Path) -> None:
    repo, board, store, card, st = await published(tmp_path)
    top = str(st.prs[-1]["branch"])
    gh = GitHubPrs(fail_close={12})
    with pytest.raises(PublishError, match="HTTP 502"):
        await start_over(tmp_path, card, board, gh)
    assert gh.states == {11: "CLOSED"} and (st.path / "state.json").is_file()
    git(repo, "branch", "-D", top)  # gone already: not an error
    done = await start_over(tmp_path, card, board, gh)
    assert gh.closes() == [11, 12, 12]  # #11 is not closed twice
    assert done.closed == [12]
    assert not (st.path).exists() and len(archived(tmp_path, card.key)) == 1
    assert store.get(card.key).status == "approved"


async def test_a_story_with_no_record_is_moved_to_approved(tmp_path: Path) -> None:
    repo_with_marker_tests(tmp_path)
    board, store, key = story_card(tmp_path, status="blocked")
    card = await board.card(key)
    gh = GitHubPrs()
    done = await start_over(tmp_path, card, board, gh)
    assert gh.calls == [] and done.archive is None and done.closed == [] and done.branches == []
    assert store.get(key).status == "approved" and store.moves(key)[-1].by == "noah"
    assert archived(tmp_path, key) == []


@pytest.mark.parametrize(
    ("point", "stage", "kept", "cleared"),
    [
        ("layers", "refine", ("story",), ("layers", "agreement", "gate")),
        ("amigos", "refine", ("story", "layers"), ("agreement", "gate")),
        ("packages", "layers", ("story", "layers", "agreement"), ("gate",)),
    ],
)
async def test_a_story_started_over_from_a_later_stage_keeps_the_stages_before_it(
    tmp_path: Path, point: str, stage: str, kept: tuple[str, ...], cleared: tuple[str, ...]
) -> None:
    repo, board, store, card, st = await stopped(tmp_path)
    old = json.loads((st.path / "state.json").read_text())
    layer_branches = {str(st.built["01"]["branch"]), str(st.attempt and st.attempt["branch"])}
    trunk = git(repo, "branch", "--show-current")
    news = trunk_commit(repo, {"NEWS": "news\n"})
    gh = GitHubPrs()
    done = await start_over(tmp_path, card, board, gh, point)
    now = json.loads((st.path / "state.json").read_text())
    assert now["stage"] == stage and now["blocked"] is None
    # on the trunk as it is now, its gate to be read there again
    assert now["base"] == {"ref": f"origin/{trunk}", "sha": news} != old["base"]
    assert {k: now[k] for k in kept} == {k: old[k] for k in kept}
    assert all(now[k] is None for k in cleared)
    assert now["built"] == {} and now["attempt"] is None and now["drafts"] == {} and not now["drafted"]
    assert not layer_branches & branches(repo) and worktrees(repo) == 1
    # a copy of the record as it was, beside the record kept in place
    [folder] = archived(tmp_path, card.key)
    assert folder.name.startswith(f"{card.key}-from-{point}-")
    assert json.loads((folder / "state.json").read_text())["built"] == old["built"]
    [event] = kinds(st.path / "events.jsonl", "restarted")
    assert event["from"] == point and event["archive"] == str(folder)
    item = store.get(card.key)
    assert item.status == "approved" and f"started over from {point}" in item.comments[-1].text
    assert done.archive == str(folder) and gh.closes() == []


async def test_a_story_started_over_from_its_packages_builds_every_layer_on_its_plan(tmp_path: Path) -> None:
    repo, board, store, card, st = await stopped(tmp_path)
    await start_over(tmp_path, card, board, GitHubPrs(), "packages")
    again = FakeEngine(
        {"architect": [*PKGS], "coder": [WRITE_PASS, RUN, DONE, NOTE, DONE], "reviewer": [APPROVE, APPROVE]}
    )
    st = await pipeline(tmp_path, repo, again, board=board).run(await board.card(card.key))
    assert st.blocked is None and sorted(st.built) == ["01", "02"]


async def test_a_story_started_over_from_a_layer_keeps_the_layers_below_it(tmp_path: Path) -> None:
    repo, board, store, card, st = await stopped(tmp_path)
    built_01 = dict(st.built["01"])
    assert st.attempt is not None
    attempt = st.attempt["branch"]
    done = await start_over(tmp_path, card, board, GitHubPrs(), "layer-02")
    now = StoryState.open(st.path, card, repo, github_repo="o/r")
    assert now.built == {"01": built_01} and now.attempt is None and now.stage == "layers"
    assert built_01["branch"] in branches(repo) and attempt not in branches(repo)
    assert done.branches == [attempt]
    # the next run builds layer 02 on layer 01 as it stands
    again = FakeEngine({"architect": [PKG2], "coder": [NOTE, DONE], "reviewer": [APPROVE]})
    st = await pipeline(tmp_path, repo, again, board=board).run(await board.card(card.key))
    assert st.blocked is None and "02" in st.built and st.built["01"] == built_01


async def test_a_layer_restart_replays_the_layers_below_it_onto_a_moved_trunk(tmp_path: Path) -> None:
    """layer-02 keeps layer 01, replayed on the trunk fetched now and held to the story's gate there; its
    branch moves only then, and the next run builds layer 02 on it."""
    repo, board, store, card, st = await published(tmp_path)
    b1, old_01 = str(st.built["01"]["branch"]), str(st.built["01"]["sha"])
    b2 = str(st.built["02"]["branch"])
    trunk = git(repo, "branch", "--show-current")
    news = trunk_commit(repo, {"NEWS": "news\n"})
    gh = GitHubPrs()
    done = await start_over(tmp_path, card, board, gh, "layer-02")
    now = StoryState.open(st.path, card, repo, github_repo="o/r")
    new_01 = now.built["01"]["sha"]
    assert new_01 != old_01 and git(repo, "rev-parse", b1) == new_01
    assert git(repo, "rev-parse", f"{b1}^") == news
    assert now.base == {"ref": f"origin/{trunk}", "sha": news} and sorted(now.built) == ["01"]
    assert b2 not in branches(repo) and done.closed == [11, 12] and done.branches == [b2]
    assert now.prs == [] and now.stage == "layers"
    assert "layers 01 to 01, replayed on it, are kept" in store.get(card.key).comments[-1].text
    again = FakeEngine({"architect": [PKG2], "coder": [NOTE, DONE], "reviewer": [APPROVE]})
    st = await pipeline(tmp_path, repo, again, board=board).run(await board.card(card.key))
    assert st.blocked is None and git(repo, "rev-parse", f"{st.built['02']['branch']}^") == new_01


async def test_a_layer_restart_whose_kept_layer_conflicts_with_the_trunk_changes_nothing(
    tmp_path: Path,
) -> None:
    repo, board, store, card, st = await published(tmp_path)
    before = (st.path / "state.json").read_text()
    trunk_commit(repo, {"PASS": "the trunk's own PASS\n"})  # layer 01 adds PASS too
    had = {b: git(repo, "rev-parse", b) for b in branches(repo)}
    gh = GitHubPrs()
    with pytest.raises(RestartError, match=r"layer 01 .*conflicts .*earlier step"):
        await start_over(tmp_path, card, board, gh, "layer-02")
    assert gh.closes() == [] and (st.path / "state.json").read_text() == before
    assert {b: git(repo, "rev-parse", b) for b in had} == had and worktrees(repo) == 1
    assert archived(tmp_path, card.key) == [] and store.get(card.key).status == "review"


async def test_a_point_that_is_none_or_not_reached_is_refused(tmp_path: Path) -> None:
    repo, board, store, card, st = await stopped(tmp_path)
    before = (st.path / "state.json").read_text()
    for point, why in (
        ("deploy", "start over from one of"),
        ("layer-03", "no layer 03"),
        ("layer-1", "start over from one of"),
    ):
        with pytest.raises(RestartError, match=why):
            await start_over(tmp_path, card, board, GitHubPrs(), point)
    assert (st.path / "state.json").read_text() == before and archived(tmp_path, card.key) == []
    fresh = store.create(level="story", title="Not started")
    with pytest.raises(RestartError, match="has not started"):
        await start_over(tmp_path, await board.card(fresh.key), board, GitHubPrs(), "packages")


# ---- the front door: restart_one, from the config ----


def configured(tmp_path: Path) -> tuple[Any, BoardStore]:
    cfg = cfg_for(tmp_path)
    return cfg, BoardStore(cfg.observability.state_dir / "board.sqlite", prefix=cfg.board.prefix)


async def test_restart_one_refuses_an_item_that_is_not_a_story(tmp_path: Path) -> None:
    cfg, store = configured(tmp_path)
    epic = store.create(level="epic", title="Avatars")
    with pytest.raises(BoardError, match="only stories"):
        await factory.restart_one(cfg, epic.key)


async def test_restart_one_refuses_a_story_whose_job_runs(tmp_path: Path) -> None:
    cfg, store = configured(tmp_path)
    story = store.create(level="story", title="Avatars")
    with pytest.raises(StoryRunning):
        await factory.restart_one(cfg, story.key, running=lambda job: job == f"story-{story.key}")


async def test_restart_one_moves_a_story_with_no_record_to_approved(tmp_path: Path) -> None:
    cfg, store = configured(tmp_path)
    story = store.create(level="story", title="Avatars")
    done = await factory.restart_one(cfg, story.key, by="noah")
    assert done.archive is None and store.get(story.key).status == "approved"
    assert store.moves(story.key)[-1].by == "noah"
