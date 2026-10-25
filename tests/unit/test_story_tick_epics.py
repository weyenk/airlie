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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        engine: FakeEngine | None = None,
        **story_extra: Any,
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
            **story_extra,
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


# ---- one resume, then the re-plan (D8) ----


def stopped(rig: TickRig, key: str, why: str = "layer 01 changed nothing") -> None:
    """The line blocked the story `key`: its state says why, and its card is in blocked."""
    d = stories_dir(rig.cfg) / key
    d.mkdir(parents=True)
    state = {"card": {"key": key}, "repo": str(rig.repo), "stage": "layers", "blocked": why, "built": {}}
    (d / "state.json").write_text(json.dumps(state))
    rig.store.update(key, status="blocked")


def smaller_plan() -> dict[str, Any]:
    """A re-plan that replaces everything with one smaller story, S4."""
    first = plan_obj()["stories"][0] | {
        "id": "S4",
        "title": "A smaller first step",
        "epic_criteria": ["EC1", "EC2"],
    }
    return {
        "type": "story_plan",
        "summary": "Avatars, again.",
        "constraints": ["PNG only"],
        "risks": [],
        "stories": [first],
    }


async def test_a_blocked_story_is_resumed_once_then_its_epic_is_replanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = TickRig(
        tmp_path, monkeypatch, FakeEngine({"architect": [reply(plan_obj()), reply(smaller_plan())]})
    )
    epic = rig.epic()
    await tick(rig.cfg)  # plan
    s1, s2 = rig.stories(epic)
    started = started_log(monkeypatch)
    stopped(rig, s1)
    # the first stop is resumed once: back to approved with the next label, and picked again
    line = await tick(rig.cfg)
    assert f"resumed {s1}" in line and [c.key for c in started] == [s1], line
    assert rig.store.get(s1).labels[-1] == "resumed:1"
    # it stops again: the epic is re-planned, once
    rig.store.update(s1, status="blocked")
    line = await tick(rig.cfg)
    assert (
        line == f"epic {epic} Avatars: re-planned after {s1} stopped again (1 new, 1 retired, 1 dropped)"
    ), line
    assert f"replanned_for:{s1}" in rig.store.get(epic).labels
    assert rig.status(s1) == "done" and rig.status(s2) == "done"  # retired; the waiting S2 was left out
    [new] = [k for k in rig.stories(epic) if k not in (s1, s2)]
    assert rig.status(new) == "approved" and len(kinds(rig.epic_events(epic), "replan")) == 1
    # and not again for the same stop
    rig.store.update(s1, status="blocked")
    assert "re-planned" not in await tick(rig.cfg)
    assert len(rig.engine.requests) == 2  # the plan, the re-plan: the third beat asked nobody


async def test_a_story_a_person_blocked_or_the_line_did_not_make_is_not_resumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, epic, (s1, s2) = await planned(tmp_path, monkeypatch)
    started = started_log(monkeypatch)
    rig.store.update(s1, status="blocked")  # a person parked it: no state says the line stopped it
    mine = rig.store.create(level="story", title="by hand", labels=["repo:o/r"]).key
    stopped(rig, mine)  # a blocked story of a person's, with no epic
    assert "resumed" not in await tick(rig.cfg)
    assert started == [] and rig.status(s1) == "blocked" and rig.status(mine) == "blocked"


async def test_resume_attempts_is_a_setting_and_zero_goes_straight_to_the_replan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine({"architect": [reply(plan_obj()), reply(smaller_plan())]})
    rig = TickRig(tmp_path, monkeypatch, engine, resume_attempts=0)
    epic = rig.epic()
    await tick(rig.cfg)
    s1, _ = rig.stories(epic)
    started = started_log(monkeypatch)
    stopped(rig, s1)
    line = await tick(rig.cfg)
    assert "resumed" not in line and started == [] and "re-planned" in line
    assert f"replanned_for:{s1}" in rig.store.get(epic).labels


# ---- the specs phase on the tick (D4, D6, N7 of the specs phase, P4 of the planning line, 2026-10-25) ----


def specs_block(**over: Any) -> str:
    """A specs-phase epic's closing block: SPEC's repositories and criteria, with P4's fields."""
    from dataclasses import replace

    return block(replace(SPEC, **over).to_dict())


async def test_an_approved_epic_with_options_and_no_pick_is_not_planned_and_stays_in_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from airlie.story.epic import Mockups
    from airlie.story.mockups import record_pick

    rig = TickRig(tmp_path, monkeypatch, FakeEngine({"architect": [reply(plan_obj())]}))
    options = tuple(f"/v1/airlie/initiatives/AIR-1/mockups/E1/option-{n}.html" for n in (1, 2))
    epic = rig.epic(EPIC_TEXT + specs_block(mockups=Mockups(options, None)))
    for _ in range(3):  # however many ticks pass
        await tick(rig.cfg)
    assert rig.status(epic) == "review" and rig.engine.requests == []  # held, never planned
    notes = [c.text for c in rig.store.get(epic).comments if "pick a mockup first" in c.text]
    assert len(notes) == 1 and "option N" in notes[0]  # one note, not one per tick
    assert not (rig.cfg.observability.state_dir / "epics" / epic / "state.json").exists()
    # the person approves it again without a pick: bounced once more, with a note
    rig.store.update(epic, status="approved")
    await tick(rig.cfg)
    assert rig.status(epic) == "review" and rig.engine.requests == []
    # a pick, then approval: planned on the next tick
    record_pick(rig.store, rig.cfg.observability.state_dir, epic, 2)
    rig.store.update(epic, status="approved")
    assert await tick(rig.cfg) == f"epic {epic} Avatars: planned into 2 stories"


async def test_an_approved_epic_without_options_waits_for_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N4: an epic with no screen (needs_mockups false) has no options, so no pick is asked for."""
    rig = TickRig(tmp_path, monkeypatch, FakeEngine({"architect": [reply(plan_obj())]}))
    epic = rig.epic(EPIC_TEXT + specs_block(kind="core", initiative="AIR-1"))
    assert await tick(rig.cfg) == f"epic {epic} Avatars: planned into 2 stories"


async def test_an_approved_extension_waits_with_a_note_until_its_core_is_in_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = TickRig(tmp_path, monkeypatch, FakeEngine({"architect": [reply(plan_obj()), reply(plan_obj())]}))
    core = rig.epic(EPIC_TEXT + specs_block(kind="core"))
    ext = rig.store.create(
        level="epic",
        title="Cropping",
        description=EPIC_TEXT + specs_block(kind="extension", depends_on=(core,)),
        labels=["repo:o/r", f"after:{core}"],
    ).key
    rig.store.update(ext, status="approved")
    line = await tick(rig.cfg)  # the core first; the extension waits
    assert line.endswith(f"epic {core} Avatars: planned into 2 stories") and f"epic {ext} held" in line
    assert rig.status(ext) == "approved"  # held, not moved
    waiting = [c.text for c in rig.store.get(ext).comments if f"waits for {core}" in c.text]
    assert len(waiting) == 1
    stories = rig.stories(core)
    assert len(rig.engine.requests) == 1  # only the core was planned
    for key in stories:  # the core's stories are done: nothing is built, and the core's epic is done too
        rig.store.update(key, status="review")
    await tick(rig.cfg)  # the core reaches review; the extension is planned on this same beat
    assert rig.status(core) == "review"
    state = rig.cfg.observability.state_dir / "epics" / ext / "state.json"
    assert state.is_file() and EpicState.open(state.parent, rig.store.get(ext), "").plan is not None
    assert len([c for c in rig.store.get(ext).comments if f"waits for {core}" in c.text]) == 1


async def test_planning_an_epic_revises_its_estimate_and_finishing_it_closes_it_and_flags_a_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from airlie.story.costs import Estimate
    from airlie.story.epic import epic_of

    rig = TickRig(tmp_path, monkeypatch, FakeEngine({"architect": [reply(plan_obj())]}))
    initiative = rig.store.create(level="initiative", title="Faces", labels=["repo:o/r"]).key
    priced = Estimate(1.0, "1 S at 1.0 h (default)", "2026-10-25T00:00:00Z")
    epic = rig.store.create(
        level="epic",
        title="Avatars",
        parent=initiative,
        description=EPIC_TEXT + specs_block(estimate=priced, initiative=initiative),
        labels=["repo:o/r", f"initiative:{initiative}"],
    ).key
    rig.store.update(epic, status="approved")
    assert await tick(rig.cfg) == f"epic {epic} Avatars: planned into 2 stories"
    spec = epic_of(rig.store.get(epic).description)
    assert spec is not None and spec.estimate is not None
    assert (
        spec.estimate.hours == 1.0 and spec.estimate.revised == 2.0
    )  # two S stories, 1 h each (the default)
    record = json.loads((rig.cfg.observability.state_dir / "epics" / epic / "state.json").read_text())
    assert record["estimate"]["revised"] == 2.0
    assert [e["kind"] for e in kinds_of(rig.epic_events(epic))].count("estimate_revised") == 1
    # both stories are finished (each took two hours on its own clock); the epic follows them to done
    for key in rig.stories(epic):
        rig.store.update(key, status="done")
        story_dir = stories_dir(rig.cfg) / key
        story_dir.mkdir(parents=True, exist_ok=True)
        (story_dir / "state.json").write_text(json.dumps({"stage": "review", "built": {}}))
        starts = [{"ts": 0.0, "kind": "story_start"}, {"ts": 7200.0, "kind": "stage"}]
        (story_dir / "events.jsonl").write_text("\n".join(json.dumps(x) for x in starts) + "\n")
    await tick(rig.cfg)
    assert rig.status(epic) == "done"
    closed = epic_of(rig.store.get(epic).description)
    assert closed is not None and closed.estimate is not None and closed.estimate.actual == 4.0
    assert closed.estimate.revised == 2.0
    assert (
        json.loads((rig.cfg.observability.state_dir / "epics" / epic / "state.json").read_text())["estimate"][
            "actual"
        ]
        == 4.0
    )
    missed = kinds_of(rig.cfg.observability.state_dir / "initiatives" / initiative / "events.jsonl")
    assert [(e["kind"], e["epic"], e["estimated"], e["actual"]) for e in missed] == [
        ("estimate_missed", epic, 1.0, 4.0)
    ]
    text = rig.store.get(initiative).comments[-1].text
    assert "priced at 1 h and took 4 h" in text
    await tick(rig.cfg)  # closing is done once
    assert len(kinds_of(rig.cfg.observability.state_dir / "initiatives" / initiative / "events.jsonl")) == 1


def kinds_of(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text().splitlines()]
