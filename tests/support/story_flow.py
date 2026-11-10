"""Shared helpers extracted from tests/unit/test_story_flow.py for tests.support.story_flow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from airlie.engine import FakeEngine
from airlie.flow.composer import Registry, RunContext
from airlie.flow.spec import NodeSpec
from airlie.flow.store import Sessions
from airlie.story.pipeline import StoryPipeline
from airlie.story.state import StoryState
from tests.support.story_epic import fenced
from tests.support.story_epic_pipeline import EPIC_TEXT, SPEC, plan_obj

KEY = "AIR-1"


def write_events(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events))


def step(run: str, team: str, n: int, *, completion: int = 100, gen_ms: float = 2000.0) -> dict[str, Any]:
    return {
        "ts": 1.0,
        "run_id": run,
        "kind": "step",
        "team_id": team,
        "step": n,
        "tool": "read_file",
        "ok": True,
        "prompt_tokens": 1000,
        "completion_tokens": completion,
        "prefill_ms": 500.0,
        "gen_ms": gen_ms,
    }


def write_story(root: Path) -> Path:
    """Story KEY under `root`: refined and planned in two layers, layer 01 built, layer 02 blocked on its
    tests; the studio runs its stages and layers name, and the tester's trace of layer 01. Returns `root`."""
    story = root / "stories" / KEY
    story.mkdir(parents=True)
    layers = [
        {
            "slug": "seam",
            "title": "The seam",
            "scope": "src/a.py",
            "brief": "build the seam",
            "acceptance": ["AC1"],
        },
        {"slug": "wire", "title": "Wire it", "scope": "src/b.py", "brief": "wire the seam", "acceptance": []},
    ]
    state = {
        "card": {"key": KEY, "title": "Do the thing"},
        "repo": "/r",
        "stage": "layers",
        "story": {
            "title": "Do the thing",
            "summary": "who wants what",
            "acceptance": [{"id": "AC1", "given": "g", "when": "w", "then": "t"}],
        },
        "layers": layers,
        "built": {"01": {"branch": "b1", "sha": "abc", "run_id": "run-l1"}},
        "blocked": "layer 02 (Wire it): its tests still do not test its change after one retry",
        "runs": ["run-refine", "run-l1", "run-l2"],
    }
    (story / "state.json").write_text(json.dumps(state))
    write_events(
        story / "events.jsonl",
        [
            {"ts": 10.0, "kind": "story_start", "stage": "new"},
            {"ts": 11.0, "kind": "stage", "stage": "intake"},
            {"ts": 12.0, "kind": "run", "stage": "refine", "studio_run": "run-refine"},
            {"ts": 20.0, "kind": "retry", "stage": "refine", "error": "no acceptance criteria"},
            {"ts": 40.0, "kind": "stage", "stage": "refine"},
            {"ts": 41.0, "kind": "run", "stage": "layers", "studio_run": "run-layers"},
            {"ts": 90.0, "kind": "stage", "stage": "layers"},
            {"ts": 91.0, "kind": "layer_start", "layer": "01", "studio_run": "run-l1"},
            {"ts": 150.0, "kind": "test_checks_passed", "layer": "01", "attempt": 1},
            {"ts": 300.0, "kind": "layer_built", "layer": "01", "branch": "b1", "sha": "abc"},
            {"ts": 301.0, "kind": "layer_start", "layer": "02", "studio_run": "run-l2"},
            {"ts": 340.0, "kind": "no_new_test", "layer": "02", "attempt": 1, "why": "no new test"},
            {"ts": 341.0, "kind": "test_checks_retry", "layer": "02", "findings": 1},
            {"ts": 380.0, "kind": "no_new_test", "layer": "02", "attempt": 2, "why": "no new test"},
            {"ts": 381.0, "kind": "blocked", "stage": "layers", "reason": "layer 02 stopped"},
        ],
    )
    runs = root / "runs"
    write_events(
        runs / "run-refine" / "events.jsonl",
        [step("run-refine", "refine", 1), step("run-refine", "refine", 2)],
    )
    write_events(runs / "run-layers" / "events.jsonl", [step("run-layers", "layers", 1)])
    write_events(
        runs / "run-l1" / "events.jsonl",
        [
            {"ts": 91.0, "kind": "subtask_start", "subtask": "tests", "role": "tester"},
            step("run-l1", "tests", 1, completion=200, gen_ms=4000.0),
            {
                "ts": 150.0,
                "kind": "subtask_done",
                "subtask": "tests",
                "role": "tester",
                "ok": True,
                "stop_reason": "done",
                "steps": 1,
                "fix_rounds": 0,
            },
            {"ts": 151.0, "kind": "subtask_start", "subtask": "build", "role": "coder"},
            step("run-l1", "build", 1),
            {
                "ts": 290.0,
                "kind": "subtask_done",
                "subtask": "build",
                "role": "coder",
                "ok": True,
                "stop_reason": "done",
                "steps": 1,
                "fix_rounds": 1,
            },
            {
                "ts": 291.0,
                "kind": "review",
                "subtask": "build",
                "verdict": "approve",
                "findings": 0,
                "round": 2,
            },
            {"ts": 295.0, "kind": "run_done", "ok": True, "tests": True},
        ],
    )
    write_events(
        runs / "run-l2" / "events.jsonl",
        [
            {"ts": 301.0, "kind": "subtask_start", "subtask": "tests", "role": "tester"},
            {
                "ts": 340.0,
                "kind": "subtask_done",
                "subtask": "tests",
                "role": "tester",
                "ok": True,
                "stop_reason": "done",
                "steps": 3,
                "fix_rounds": 0,
            },
        ],
    )
    trace = runs / "run-l1" / "trace" / "tests"
    trace.mkdir(parents=True)
    (trace / "prompt.json").write_text(
        json.dumps({"role": "tester", "attempt": 0, "messages": [{"role": "user", "content": "write tests"}]})
    )
    (trace / "step-001.json").write_text(
        json.dumps(
            {
                "u_t": {"name": "read_file", "arguments": "{}"},
                "o_t": "x",
                "ok": True,
                "assistant_text": "",
                "reasoning": "think",
            }
        )
    )
    return root


def by_id(items: list[dict[str, Any]], id: str) -> dict[str, Any]:
    (found,) = [x for x in items if x["id"] == id]
    return found


def write_epic(root: Path, key: str = "AIR-1", *, damaged: bool = False) -> Path:
    """Epic `key` under `root`: planned into S1 (AIR-2, in review) and S2 (AIR-3, not started), with one
    re-plan on record and a story it left out (AIR-9)."""
    d = root / "epics" / key
    d.mkdir(parents=True)
    if damaged:
        (d / "state.json").write_text("{ not json")
        return root
    state = {
        "card": {"key": key, "title": "Avatars", "description": EPIC_TEXT + fenced(SPEC.to_dict())},
        "repo": "o/r",
        "stage": "building",
        "plan": plan_obj(),
        "attempts": 2,
        "stories": {"S1": "AIR-2", "S2": "AIR-3", "S0": "AIR-9"},
        "replans": [{"reason": "AIR-9 stopped again", "created": ["AIR-3"], "retired": ["AIR-9"]}],
        "blocked": None,
        "runs": [],
        "base": {"ref": "origin/main", "sha": "a" * 40},
        "seen_comments": 0,
    }
    (d / "state.json").write_text(json.dumps(state))
    (d / "events.jsonl").write_text("")
    story = root / "stories" / "AIR-2"
    story.mkdir(parents=True)
    (story / "state.json").write_text(
        json.dumps({"card": {"key": "AIR-2"}, "stage": "review", "blocked": None})
    )
    return root


def delivered_initiative(tmp_path: Path) -> tuple[Any, str, str, str]:
    """A board with an initiative whose core and extension are carded (the core with three mockup pages, one
    picked) and its record on disk: returns the store, the initiative's key and the two epics' keys."""
    from airlie.board import BoardStore
    from airlie.story.costs import Estimate
    from airlie.story.epic import EpicCriterion, EpicSpec, Mockups, Viability, with_block

    store = BoardStore(tmp_path / "board.sqlite")
    init = store.create(
        level="initiative", title="Linear feel", description="Faster.", labels=["repo:o/r", "budget:20h"]
    ).key
    urls = tuple(f"/v1/airlie/initiatives/{init}/mockups/E1/option-{n}.html" for n in (1, 3))
    core_spec = EpicSpec(
        ("o/r",),
        (EpicCriterion("EC1", "a card moves"),),
        (),
        urls,
        init,
        "core",
        estimate=Estimate(5.0, "1 M at 5.0 h (default)", "2026-10-25T00:00:00Z", revised=6.0),
        viability=Viability("risky", ("old store",)),
        mockups=Mockups(urls, 3),
    )
    core = store.create(
        level="epic",
        title="Fast board",
        parent=init,
        description=with_block("# Fast board\n", core_spec),
        labels=[f"initiative:{init}"],
    ).key
    ext_spec = EpicSpec(
        ("o/r",),
        (EpicCriterion("EC1", "cards are cropped"),),
        initiative=init,
        kind="extension",
        depends_on=(core,),
        estimate=Estimate(2.0, "2 S at 1.0 h (default)", "2026-10-25T00:00:00Z"),
        viability=Viability("viable", ("small",)),
    )
    ext = store.create(
        level="epic",
        title="Crops",
        parent=init,
        description=with_block("# Crops\n", ext_spec),
        labels=[f"initiative:{init}", f"after:{core}"],
    ).key
    store.update(core, status="approved")  # approved and not done: its hours are in the queue
    store.update(ext, status="review")
    folder = tmp_path / "initiatives" / init
    folder.mkdir(parents=True)
    state = {
        "card": {"key": init, "title": "Linear feel"},
        "stage": "delivered",
        "summary": "Make the board fast.",
        "assumptions": ["one user"],
        "qa": [
            {"q": "What is it for?", "asked_at": 1.0, "a": "Speed.", "answered_at": 2.0},
            {"q": "Who?", "asked_at": 3.0, "a": None, "answered_at": None},
        ],
        "consultations": [
            {"seat": "ux", "question": "How?", "answer": "- keys", "seconds": 12.3, "round": 1, "chars": 6},
        ],
        "epics": [
            {
                "id": "E1",
                "key": core,
                "kind": "core",
                "title": "Fast board",
                "viability": {"verdict": "risky"},
            },
            {"id": "E2", "key": ext, "kind": "extension", "title": "Crops"},
            {"id": "E3", "kind": "extension", "title": "Not carded yet"},
        ],
    }
    (folder / "state.json").write_text(json.dumps(state))
    epics_dir = tmp_path / "epics" / core
    epics_dir.mkdir(parents=True)
    (epics_dir / "state.json").write_text(
        json.dumps({"estimate": {"hours": 5.0, "basis": "b", "at": "t", "revised": 6.0}})
    )
    return store, init, core, ext


def run_context(pipe: StoryPipeline, registry: Registry, store: Any, node: str) -> RunContext:
    """The context the composer hands a story node's runner, over `store`."""
    return RunContext(
        cfg=pipe.cfg,
        store=store,
        sessions=Sessions(),
        registry=registry,
        workflow="story",
        node=node,
        item=None,
        feedback=None,
    )


def v2(**fields: Any) -> NodeSpec:
    """A schema v2 node, as a workflow file would declare it."""
    return NodeSpec.model_validate(fields)


def bare_state(tmp_path: Path, repo: Path, title: str = "add a marker") -> StoryState:
    """A story's state with nothing in it yet but its card's title (what an ask names its run by)."""
    st = StoryState(path=tmp_path / "story", card={"title": title}, repo=str(repo))
    st.path.mkdir()
    return st


def briefs_to(engine: FakeEngine, role: str) -> list[str]:
    """Every request the engine was sent for `role`, its messages joined."""
    return ["\n".join(m.content for m in r.messages) for r in engine.requests if r.role == role]
