"""The tick and the epics (D5, D10 of the plans phase, P3 of the planning line, 2026-10-24): it plans the top
approved epic before it builds a story, promotes waiting stories as their dependencies reach review, gates a
planned epic's stories on the epic's status, moves the epic along with its stories, and replays a dependent
story whose base moved before it builds any more."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from airlie.board import BoardStore
from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.story import factory
from airlie.story.epic_state import EpicState
from airlie.story.factory import stories_dir, tick
from airlie.story.publish import Publisher
from tests.unit.git_helpers import git
from tests.unit.test_story_epic import fenced as block
from tests.unit.test_story_epic_pipeline import EPIC_TEXT, SPEC, plan_obj, reply
from tests.unit.test_story_factory import Intake, cfg_for, github_answers, started_log
from tests.unit.test_story_pipeline import (
    STORY_ROLES,
    RecordedGh,
    checkout_of,
    fenced,
    kinds,
    layer,
    package_reply,
)
from tests.unit.test_studio_orchestrator import APPROVE, DONE, RUN, WRITE_PASS


class TickRig:
    """A tick over the local board: o/r's checkout is in story.repos_root, GitHub answers, gh is recorded,
    and every pipeline the tick makes runs on `engine`."""

    def __init__(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: FakeEngine | None = None
    ) -> None:
        root = tmp_path / "git"
        self.tmp = tmp_path
        self.repo = checkout_of(root, "o/r")
        self.trunk = git(self.repo, "branch", "--show-current")
        story = {
            "repos_root": str(root),
            "trunk": self.trunk,
            "plan": {"round_steps": 0},
            "layer_attempts": 1,
        }
        self.cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": story})
        self.engine = engine or FakeEngine({"architect": []})
        self.gh = RecordedGh()
        monkeypatch.setattr(factory, "_engine", lambda cfg: self.engine)
        monkeypatch.setattr(
            factory, "Publisher", lambda *, remote, app: Publisher(remote=remote, run=self.gh, app=app)
        )
        github_answers(monkeypatch)
        self.store = BoardStore(self.cfg.observability.state_dir / "board.sqlite")

    def epic(self, description: str | None = None, status: str = "approved") -> str:
        text = EPIC_TEXT + block(SPEC.to_dict()) if description is None else description
        key = self.store.create(level="epic", title="Avatars", description=text, labels=["repo:o/r"]).key
        self.store.update(key, status=status)  # type: ignore[arg-type]
        return key

    def stories(self, epic: str) -> list[str]:
        return [s.key for s in self.store.list(level="story", parent=epic)]

    def status(self, key: str) -> str:
        return self.store.get(key).status

    def epic_events(self, epic: str) -> Path:
        return self.cfg.observability.state_dir / "epics" / epic / "events.jsonl"


async def planned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TickRig, str, list[str]]:
    """A rig whose epic has been planned by one tick into S1 (approved) and S2 (waiting)."""
    rig = TickRig(tmp_path, monkeypatch, FakeEngine({"architect": [reply(plan_obj())]}))
    epic = rig.epic()
    assert await tick(rig.cfg) == f"epic {epic} Avatars: planned into 2 stories"
    return rig, epic, rig.stories(epic)


async def test_the_tick_plans_the_top_approved_epic_before_it_builds_a_story(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = TickRig(tmp_path, monkeypatch, FakeEngine({"architect": [reply(plan_obj())]}))
    mine = rig.store.create(level="story", title="by hand", labels=["repo:o/r"]).key
    rig.store.update(mine, status="approved")
    epic = rig.epic()
    started = started_log(monkeypatch)
    assert await tick(rig.cfg) == f"epic {epic} Avatars: planned into 2 stories"
    assert started == []  # planning was this beat's one piece of work
    s1, s2 = rig.stories(epic)
    assert (rig.status(s1), rig.status(s2)) == ("approved", "todo")
    # the next beat builds, as before: the oldest approved story, which is the person's
    assert await tick(rig.cfg) == f"{mine} by hand: review (taken from approved)"
    assert [c.key for c in started] == [mine]
    state = EpicState.open(rig.cfg.observability.state_dir / "epics" / epic, rig.store.get(epic), "")
    assert state.stage == "planned" and sorted(state.stories) == ["S1", "S2"]


async def test_an_approved_epic_with_no_block_is_set_aside_with_a_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = TickRig(tmp_path, monkeypatch)
    epic = rig.epic("Imported from Jira: prose only.")
    started = started_log(monkeypatch)
    line = await tick(rig.cfg)
    assert line.startswith(f"epic {epic} Avatars: set aside (it has no closing epic JSON block")
    assert rig.status(epic) == "blocked" and not rig.engine.requests and started == []
    [note] = rig.store.get(epic).comments
    assert "set aside" in note.text and "Approve the epic again" in note.text
    assert not (rig.cfg.observability.state_dir / "epics" / epic).exists()  # never planned, no record
    assert await tick(rig.cfg) == "idle: no issue matches the intake query"  # and not taken again


async def test_a_waiting_story_is_promoted_when_its_dependency_reaches_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, epic, (s1, s2) = await planned(tmp_path, monkeypatch)
    started = started_log(monkeypatch)
    rig.store.update(s1, status="doing")
    line = await tick(rig.cfg)
    assert "promoted" not in line and rig.status(s2) == "todo"
    rig.store.update(s1, status="review")
    line = await tick(rig.cfg)
    assert f"promoted {s2}" in line and rig.status(s2) == "approved"
    assert [c.key for c in started] == [s2]  # promoted, then picked: the beat's one piece of work
    [event] = kinds(rig.epic_events(epic), "story_promoted")
    assert event["story"] == s2 and event["after"] == [s1]


async def test_a_paused_epic_holds_its_stories_until_it_is_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, epic, (s1, s2) = await planned(tmp_path, monkeypatch)
    started = started_log(monkeypatch)
    rig.store.update(epic, status="blocked")  # a person pauses the epic
    assert await tick(rig.cfg) == "idle: no issue matches the intake query"
    assert started == [] and rig.status(s1) == "approved" and rig.status(epic) == "blocked"
    rig.store.update(s1, status="review")  # even a finished dependency promotes nothing while it is held
    assert "promoted" not in await tick(rig.cfg)
    assert rig.status(s2) == "todo" and started == []
    rig.store.update(epic, status="approved")  # released
    line = await tick(rig.cfg)
    assert f"promoted {s2}" in line and [c.key for c in started] == [s2]
    rig.store.update(s2, status="approved")
    rig.store.update(epic, status="todo")  # todo holds as well
    started.clear()
    assert await tick(rig.cfg) == "idle: no issue matches the intake query" and started == []


async def test_an_epics_status_follows_its_stories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig, epic, (s1, s2) = await planned(tmp_path, monkeypatch)
    started_log(monkeypatch)
    assert rig.status(epic) == "approved"
    rig.store.update(s1, status="doing")
    assert (await tick(rig.cfg)).startswith(f"epic {epic} is doing") and rig.status(epic) == "doing"
    rig.store.update(s1, status="review")
    rig.store.update(s2, status="review")
    assert (await tick(rig.cfg)).startswith(f"epic {epic} is review") and rig.status(epic) == "review"
    state_dir = rig.cfg.observability.state_dir / "epics" / epic
    assert EpicState.open(state_dir, rig.store.get(epic), "").stage == "review"
    rig.store.update(s1, status="done")
    rig.store.update(s2, status="done")
    assert (await tick(rig.cfg)).startswith(f"epic {epic} is done") and rig.status(epic) == "done"
    assert EpicState.open(state_dir, rig.store.get(epic), "").stage == "done"
    assert await tick(rig.cfg) == "idle: no issue matches the intake query"


# ---- a dependent whose base moved (Review Focus 3) ----


def two_layer_plan() -> dict[str, Any]:
    """plan_obj, whose S2 has two criteria so that it can have two layers."""
    obj = plan_obj()
    obj["stories"][1]["acceptance"].append(
        {"id": "AC2", "given": "a user", "when": "they go on", "then": "ok"}
    )
    return obj


def layer_pair() -> FakeReply:
    first = layer("view", "View", "src/view.py", "AC1", "Show it.")
    second = layer("more", "More", "src/more.py", "AC2", "Go on.")
    return fenced({"layers": [first, second]})


def pkg_of(name: str, criterion: str) -> FakeReply:
    path = f"tests/unit/test_{name}.py"
    return package_reply(
        {path: f"def test_{name}():\n    assert True\n"},
        {criterion: [f"{path}::test_{name}"]},
        files=[{"path": f"src/{name}.py", "action": "create"}],
    )


def write(path: str) -> FakeReply:
    return FakeReply(tool_calls=[ToolCall("w", "write_file", json.dumps({"path": path, "content": "x\n"}))])


def moving_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TickRig:
    nonsense = FakeReply(text="no package, sorry")
    engine = FakeEngine(
        {
            "architect": [
                reply(two_layer_plan()),
                fenced({"layers": [layer("avatar", "Avatar", "src/avatar.py", "AC1", "Build it.")]}),
                pkg_of("avatar", "AC1"),
                layer_pair(),
                pkg_of("view", "AC1"),
                nonsense,  # layer 02's package: unusable twice, so the story stops after layer 01
                nonsense,
                pkg_of("more", "AC2"),  # asked again after the replay
            ],
            "coder": [WRITE_PASS, RUN, DONE, write("view.txt"), DONE, write("more.txt"), DONE],
            "reviewer": [APPROVE, APPROVE, APPROVE],
        }
    )
    return TickRig(tmp_path, monkeypatch, engine)


def saved(rig: TickRig, key: str) -> dict[str, Any]:
    """The story's state.json as it is on disk."""
    data: dict[str, Any] = json.loads((stories_dir(rig.cfg) / key / "state.json").read_text())
    return data


def reviewer_pushes(rig: TickRig, branch: str) -> str:
    """A commit on `branch`, as a reviewer who changes the lower story leaves it; returns its sha."""
    wt = rig.tmp / "reviewer"
    git(rig.repo, "worktree", "add", "-q", "--detach", str(wt), branch)
    (wt / "reviewed.txt").write_text("a reviewer's change\n")
    git(wt, "add", "reviewed.txt")
    git(wt, "-c", "user.name=r", "-c", "user.email=r@r", "commit", "-q", "-m", "review fix")
    sha = git(wt, "rev-parse", "HEAD")
    git(rig.repo, "update-ref", f"refs/heads/{branch}", sha)
    git(rig.repo, "worktree", "remove", "--force", str(wt))
    return sha


async def test_a_dependent_whose_base_moved_is_replayed_before_it_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = moving_rig(tmp_path, monkeypatch)
    epic = rig.epic()
    assert await tick(rig.cfg) == f"epic {epic} Avatars: planned into 2 stories"
    s1, s2 = rig.stories(epic)
    assert (await tick(rig.cfg)).endswith(f"{s1} Do S1: review (taken from approved)")
    top = f"{s1}-do-s1-01-avatar"
    old_base = git(rig.repo, "rev-parse", f"refs/heads/{top}")
    line = await tick(rig.cfg)  # S1 is in review: S2 is promoted and built until its second package fails
    assert f"promoted {s2}" in line and "blocked (layer 02" in line, line
    st2 = saved(rig, s2)
    assert st2["base"] == {"ref": top, "sha": old_base, "story": s1} and list(st2["built"]) == ["01"]
    first_layer = st2["built"]["01"]["sha"]

    new_base = reviewer_pushes(rig, top)  # a reviewer changes the lower story
    rig.store.update(s2, status="approved")  # and a person puts the stopped story back
    line = await tick(rig.cfg)
    assert f"replayed {s2} onto {s1}" in line and line.endswith(
        f"{s2} Do S2: review (taken from approved)"
    ), line

    st2 = saved(rig, s2)
    assert st2["stage"] == "review" and st2["blocked"] is None, st2["blocked"]
    first, second = st2["built"]["01"], st2["built"]["02"]
    assert first["sha"] != first_layer  # replayed onto the new base
    git(rig.repo, "merge-base", "--is-ancestor", new_base, first["branch"])
    git(rig.repo, "merge-base", "--is-ancestor", first["branch"], second["branch"])
    events_file = stories_dir(rig.cfg) / s2 / "events.jsonl"
    events = [e["kind"] for e in map(json.loads, events_file.read_text().splitlines())]
    last_layer_start = len(events) - 1 - events[::-1].index("layer_start")
    assert events.index("restacked") < last_layer_start  # replayed before layer 02 was built
    [moved] = kinds(events_file, "restacked")
    assert moved["base"] == {"from": old_base, "to": new_base}
    assert ["gh", "stack", "init", "--base", top, first["branch"], second["branch"]] in rig.gh.calls


class MergingGh(RecordedGh):
    """RecordedGh that also answers `gh pr view N --json state,mergedAt`: merged for those in `merged`."""

    def __init__(self) -> None:
        super().__init__()
        self.merged: set[str] = set()

    async def __call__(self, args: list[str], cwd: Path, env: Mapping[str, str]) -> str:
        if args[:3] == ["gh", "pr", "view"] and args[-2:] == ["--json", "state,mergedAt"]:
            self.calls.append(args)
            done = args[3] in self.merged
            view = {"state": "MERGED", "mergedAt": "2026-10-25T10:00:00Z"} if done else {"state": "OPEN"}
            return json.dumps(view)
        return await super().__call__(args, cwd, env)


@pytest.mark.parametrize("squash", [True, False])
async def test_a_dependent_whose_dependency_was_merged_is_replayed_onto_the_trunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, squash: bool
) -> None:
    rig = moving_rig(tmp_path, monkeypatch)
    rig.gh = MergingGh()
    epic = rig.epic()
    await tick(rig.cfg)  # plan
    s1, s2 = rig.stories(epic)
    await tick(rig.cfg)  # S1 built
    top = f"{s1}-do-s1-01-avatar"
    await tick(rig.cfg)  # S2 promoted, built until its second package fails
    assert saved(rig, s2)["base"]["story"] == s1 and list(saved(rig, s2)["built"]) == ["01"]
    first_layer = saved(rig, s2)["built"]["01"]["sha"]

    # a person merged the lower story: squashed (the trunk has its changes in a commit of its own), or as it
    # stood (the trunk is now at its top commit)
    if squash:
        git(rig.repo, "merge", "-q", "--squash", top)
        who = ["-c", "user.name=m", "-c", "user.email=m@m"]
        git(rig.repo, *who, "commit", "-q", "-m", "squash the lower story")
    else:
        git(rig.repo, "merge", "-q", "--ff-only", top)
    trunk_sha = git(rig.repo, "rev-parse", rig.trunk)
    assert isinstance(rig.gh, MergingGh)
    rig.gh.merged = {str(p["number"]) for p in saved(rig, s1)["prs"]}
    rig.store.update(s2, status="approved")
    line = await tick(rig.cfg)
    assert s2 in line and "trunk" in line, line

    st2 = saved(rig, s2)
    assert st2["stage"] == "review" and st2["blocked"] is None, st2["blocked"]
    assert st2["base"] == {"ref": f"origin/{rig.trunk}", "sha": trunk_sha}  # on the trunk now: no story
    first = st2["built"]["01"]
    assert (first["sha"] != first_layer) is squash  # replayed only where the trunk differs
    git(rig.repo, "merge-base", "--is-ancestor", trunk_sha, first["branch"])
    branches = [first["branch"], st2["built"]["02"]["branch"]]
    assert ["gh", "stack", "init", "--base", rig.trunk, *branches] in rig.gh.calls  # published on the trunk
    assert "Stacks on" not in (stories_dir(rig.cfg) / s2 / "pr-01.md").read_text()


async def test_with_a_jira_board_the_epic_steps_log_one_line_and_do_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Jira stays read-only for the line: no epic is planned, no card promoted, nothing replayed."""
    rig = TickRig(tmp_path, monkeypatch)
    story = {"board": "jira", "repos_root": str(tmp_path / "git")}
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": story})
    board = Intake(None)
    monkeypatch.setattr(factory, "make_board", lambda cfg: board)
    with caplog.at_level("INFO", logger="airlie.story.factory"):
        assert await tick(cfg) == "idle: no issue matches the intake query"
    said = [r.getMessage() for r in caplog.records if "epics" in r.getMessage()]
    assert said == ["story.board is jira: the line only reads it, so epics are neither planned nor followed"]
    assert board.asked == 1 and not rig.engine.requests
