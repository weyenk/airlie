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
from airlie.story import restart as restart_module
from airlie.story.board import BoardError, Card
from airlie.story.local_board import LocalBoard
from airlie.story.model import StoryRunning
from airlie.story.publish import Publisher, PublishError
from airlie.story.restart import FRESH, Restarted, RestartError, RestartIncomplete, restart
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


async def stopped(
    tmp_path: Path, *, composer: bool = False
) -> tuple[Path, LocalBoard, BoardStore, Card, StoryState]:
    """A story whose layer 01 is built and whose layer 02 stopped (it changed nothing): its branch and
    worktree are kept, as an attempt. `composer`: run through the story workflow (story.composer)."""
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
    st = await pipeline(tmp_path, repo, engine, board=board, composer=composer).run(card)
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
    with pytest.raises(RestartIncomplete, match="HTTP 502.*run it again to finish") as failed:
        await start_over(tmp_path, card, board, gh)
    assert isinstance(failed.value.__cause__, PublishError)
    assert gh.states == {11: "CLOSED"} and (st.path / "state.json").is_file()
    assert store.get(card.key).status == "todo"  # held out of the line until the restart finishes
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
@pytest.mark.parametrize("composer", [False, True])
async def test_a_story_started_over_from_a_later_stage_keeps_the_stages_before_it(
    tmp_path: Path, point: str, stage: str, kept: tuple[str, ...], cleared: tuple[str, ...], composer: bool
) -> None:
    repo, board, store, card, st = await stopped(tmp_path, composer=composer)
    st.workflow = {"name": "story", "specs": {}}
    st.save()
    old = json.loads((st.path / "state.json").read_text())
    assert old["workflow"] is not None
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
    # kept: the steps it resumes are named by the snapshot alone (a started card's workflow cannot change)
    assert now["workflow"] == old["workflow"]
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


async def test_a_card_on_a_draft_workflow_restarted_from_a_step_runs_its_own_snapshot_again(
    tmp_path: Path,
) -> None:
    """A restart keeps the snapshot: the card resumes the workflow it started on (its node ids are named by
    the snapshot alone), not the story, and the snapshot is what it was, whatever the draft says since."""
    from tests.unit.test_story_workflows import MINI, MINI_MORE

    repo = github_repo(tmp_path)
    board, store, key = story_card(tmp_path)
    store.update(key, labels=["workflow:mini"])
    card = await board.card(key)
    pipe = pipeline(tmp_path, repo, happy_engine(), board=board)
    pipe.drafts = lambda: {"mini": MINI}
    st = await pipe.run(card)
    assert st.blocked is None and st.stage == "review", st.blocked
    saved = json.loads((st.path / "state.json").read_text())
    snap = saved["workflow"]
    assert snap["name"] == "mini"
    saved["story"] = {"title": "fix the marker"}  # what a later point needs reached; mini refines nothing
    (st.path / "state.json").write_text(json.dumps(saved))
    trunk_commit(repo, {"NEWS": "news\n"})
    assert saved["nodes_done"]  # the composer's done set, before the restart
    await start_over(tmp_path, card, board, GitHubPrs(), "layers")
    kept = json.loads((st.path / "state.json").read_text())
    assert kept["workflow"] == snap
    assert "nodes_done" not in kept  # dropped whole: an output-less node runs again
    before = len((st.path / "events.jsonl").read_text().splitlines())

    again = pipeline(tmp_path, repo, happy_engine(), board=board)
    again.drafts = lambda: {"mini": MINI_MORE}  # edited since: the restarted card does not see it
    st = await again.run(await board.card(key))
    assert st.blocked is None and st.stage == "review", st.blocked
    events = [json.loads(x) for x in (st.path / "events.jsonl").read_text().splitlines()][before:]
    ran = [e["node"] for e in events if e["kind"] == "node_started"]
    assert ran and set(ran) <= {"intake", "base", "gate", "review"}
    assert json.loads((st.path / "state.json").read_text())["workflow"] == snap


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


async def test_a_layer_restart_drops_the_satisfied_layers_from_it_up_and_counts_those_below_as_done(
    tmp_path: Path,
) -> None:
    """A satisfied layer (`layer_satisfied`, 2026-11-06) has no branch: a restart from a layer above it keeps
    it as it keeps a built one, and a restart from it or below forgets it, so the layer is judged again."""
    repo, board, store, card, st = await stopped(tmp_path)
    st.built.pop("01")
    st.satisfied = {"01": {"criteria": ["AC1"], "by": ["README.md"], "why": "it holds"}}
    st.save()
    await start_over(tmp_path, card, board, GitHubPrs(), "layer-02")  # layer 01 counts as done
    now = StoryState.open(st.path, card, repo, github_repo="o/r")
    assert now.satisfied == {"01": {"criteria": ["AC1"], "by": ["README.md"], "why": "it holds"}}
    assert now.built == {} and now.stage == "layers"
    await start_over(tmp_path, card, board, GitHubPrs(), "layer-01")
    assert StoryState.open(st.path, card, repo, github_repo="o/r").satisfied == {}


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


# ---- review fixes: the hold, the lane, the refined story, a re-run after the branches moved ----


async def test_the_card_is_held_out_of_the_line_while_the_kept_layers_replay(tmp_path: Path) -> None:
    """Between the running check and the end the card waits in todo, so no beat picks it while the gate runs
    on the replay; it goes to approved only at the end."""
    repo, board, store, card, st = await published(tmp_path)
    trunk_commit(repo, {"NEWS": "news\n"})
    seen: list[str] = []
    gh = GitHubPrs()
    real = pipeline(tmp_path, repo, FakeEngine({}), gh, board=board).replay_layers

    async def watching(c: Card, onto: str, layers: Mapping[str, str]) -> dict[str, str]:
        seen.append(store.get(card.key).status)
        return await real(c, onto, layers)

    await start_over(tmp_path, card, board, gh, "layer-02", replay=watching)
    assert seen == ["todo"] and store.get(card.key).status == "approved"


async def test_a_refusal_after_the_hold_puts_the_card_back_where_it_was(tmp_path: Path) -> None:
    repo, board, store, card, st = await published(tmp_path)
    trunk_commit(repo, {"PASS": "the trunk's own PASS\n"})  # layer 01 conflicts
    with pytest.raises(RestartError, match="conflicts"):
        await start_over(tmp_path, card, board, GitHubPrs(), "layer-02")
    assert store.get(card.key).status == "review"
    assert [m.to_status for m in store.moves(card.key)][-2:] == ["todo", "review"]


async def test_a_replay_waits_for_no_other_story_the_build_lane_is_busy_is_a_refusal(tmp_path: Path) -> None:
    repo, board, store, card, st = await published(tmp_path)
    before = (st.path / "state.json").read_text()
    trunk_commit(repo, {"NEWS": "news\n"})
    gh = GitHubPrs()
    with lane_lock(tmp_path / "state" / "stories", "build") as held:
        assert held
        with pytest.raises(RestartError, match="build lane is busy"):
            await start_over(tmp_path, card, board, gh, "layer-02")
    assert gh.closes() == [] and (st.path / "state.json").read_text() == before
    assert store.get(card.key).status == "review"


class HeldBoard:
    """A board that says which column a card is in, as Jira's does (`column_of`), and keeps its moves."""

    def __init__(self, column: str) -> None:
        self.column = column
        self.moved: list[str] = []

    async def column_of(self, card: Card) -> str:
        return self.column

    async def move(self, card: Card, column: str) -> None:
        self.moved.append(column)


async def test_a_board_that_is_not_the_local_one_holds_a_card_in_intake_in_blocked() -> None:
    """On Jira the tick takes the intake status (To Do): the card waits in blocked, and a refusal puts it
    back."""
    card = Card("HDS-7", "1", "HDS-7", "x", "")
    board = HeldBoard("intake")
    back = await restart_module._hold(board, card, "noah")  # type: ignore[arg-type]
    await back()
    assert board.moved == ["blocked", "intake"]
    board = HeldBoard("review")  # not a column the tick takes: nothing to hold
    await (await restart_module._hold(board, card, "noah"))()  # type: ignore[arg-type]
    assert board.moved == []


async def test_a_story_run_from_the_cli_names_its_lane_so_a_restart_sees_it_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = cfg_for(tmp_path, roles={})
    stories = factory.stories_dir(cfg)
    card = Card("AIR-7", "AIR-7", "AIR-7", "x", "")
    seen: list[str | None] = []

    class Pipe:
        async def plan(self, c: Card) -> Any:
            seen.append(restart_module._running_now("AIR-7", stories, None))
            return type("St", (), {"blocked": None, "done": lambda self, s: True})()

        async def build(self, c: Card) -> Any:
            seen.append(restart_module._running_now("AIR-7", stories, None))
            return "built"

    async def placed(*a: Any, **k: Any) -> Any:
        return factory.Place(tmp_path, None)

    monkeypatch.setattr(factory, "placed", placed)
    monkeypatch.setattr(factory, "make_pipeline", lambda *a, **k: Pipe())
    assert await factory.run_one(cfg, board=None, card=card) == "built"  # type: ignore[arg-type]
    assert seen == ["the plan lane is running it", "the build lane is running it"]
    assert restart_module._running_now("AIR-7", stories, None) is None
    assert not list((cfg.observability.state_dir / "line" / "lanes").glob("*.json"))


async def test_layers_and_amigos_restore_the_story_and_plan_as_they_stood_before_the_meeting(
    tmp_path: Path,
) -> None:
    """The meeting's agreement, and a criterion reworded in the build, replace the story and the layers; the
    refined ones are kept from the meeting's start, and a restart from layers or amigos goes back to them."""
    repo, board, store, card, st = await stopped(tmp_path)
    data = json.loads((st.path / "state.json").read_text())
    assert data["refined_story"] == data["story"] and data["refined_layers"] == data["layers"]
    refined_story, refined_layers = data["refined_story"], data["refined_layers"]
    data["story"] = {**data["story"], "title": "as the meeting worded it"}
    data["layers"] = data["layers"][:1]
    (st.path / "state.json").write_text(json.dumps(data))
    await start_over(tmp_path, card, board, GitHubPrs(), "amigos")
    now = json.loads((st.path / "state.json").read_text())
    assert now["story"] == refined_story and now["layers"] == refined_layers
    data = {
        **now,
        "story": {**now["story"], "title": "again"},
        "agreement": data["agreement"],
        "stage": "layers",
    }
    (st.path / "state.json").write_text(json.dumps(data))
    await start_over(tmp_path, card, board, GitHubPrs(), "layers")
    now = json.loads((st.path / "state.json").read_text())
    assert now["story"] == refined_story and now["layers"] is None and now["refined_layers"] is None


async def test_layers_and_amigos_are_refused_for_a_story_recorded_before_the_refined_one_was_kept(
    tmp_path: Path,
) -> None:
    repo, board, store, card, st = await stopped(tmp_path)
    data = json.loads((st.path / "state.json").read_text())
    data.pop("refined_story")
    data.pop("refined_layers")
    (st.path / "state.json").write_text(json.dumps(data))
    for point in ("layers", "amigos"):
        with pytest.raises(RestartError, match="before the meeting.*refine"):
            await start_over(tmp_path, card, board, GitHubPrs(), point)
    assert archived(tmp_path, card.key) == [] and store.get(card.key).status == "blocked"


async def test_a_rerun_after_the_branches_moved_and_the_record_failed_finishes_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, board, store, card, st = await published(tmp_path)
    b1 = str(st.built["01"]["branch"])
    news = trunk_commit(repo, {"NEWS": "news\n"})
    real = restart_module._cleared
    calls: list[int] = []

    def fails_once(*a: Any, **k: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk full")
        real(*a, **k)

    monkeypatch.setattr(restart_module, "_cleared", fails_once)
    with pytest.raises(RestartIncomplete, match="disk full"):
        await start_over(tmp_path, card, board, GitHubPrs(), "layer-02")
    moved = git(repo, "rev-parse", b1)
    assert git(repo, "rev-parse", f"{b1}^") == news  # moved, and the record still names the old commit
    await start_over(tmp_path, card, board, GitHubPrs(), "layer-02")
    now = StoryState.open(st.path, card, repo, github_repo="o/r")
    assert now.built["01"]["sha"] == moved == git(repo, "rev-parse", b1) and now.base is not None
    assert now.base["sha"] == news and store.get(card.key).status == "approved"


async def test_a_board_that_fails_after_the_prs_are_closed_is_an_incomplete_restart(tmp_path: Path) -> None:
    repo, board, store, card, st = await published(tmp_path)

    class Refuses(LocalBoard):
        async def comment(self, c: Card, text: str, role: str | None = None) -> None:
            raise BoardError("the board is down")

    with pytest.raises(RestartIncomplete, match="the board is down.*run it again to finish"):
        await start_over(tmp_path, card, Refuses(store), GitHubPrs())


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
