"""An old-schema workflow read as the current one (`upgrade_v1`, D21); a card adopting the current workflow in
place of its snapshot (`adopt_current`, D24); bundles knowing where they are used (nodes-are-config design,
"Bundles and isolation")."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI
from typer.testing import CliRunner

from airlie import cli
from airlie.board import BoardStore
from airlie.config import load_config
from airlie.flow.spec import WorkflowSpec
from airlie.flow.upgrade import upgrade_v1
from airlie.serve.app import create_app
from airlie.serve.workflows_api import workflows_router
from airlie.story import factory
from airlie.story.board import text_card
from airlie.story.flow import STORY_CATALOG
from airlie.story.model import StoryRunning
from airlie.story.state import StoryState
from airlie.story.workflows import (
    WorkflowError,
    adopt_current,
    draft_spec,
    from_snapshot,
    resolve_workflows,
    snapshot,
    spec_hash,
    used_by,
    workflow_problems,
)
from tests.unit.serve_helpers import TestClient
from tests.unit.test_serve import FakeUpstream
from tests.unit.test_story_pipeline import (
    RecordingBoard,
    happy_engine,
    kinds_all,
    pipeline,
    repo_with_marker_tests,
)
from tests.unit.test_story_workflows import MINI, MINI_EACH, MINI_MORE, TINY, mini_card, started
from tests.unit.test_studio_orchestrator import cfg_for

MINI_V1 = """\
workflows:
  mini:
    type: bug
    start: intake
    loose: [card, repo]
    nodes:
      intake: {type: check, run: story.intake}
      base: {type: check, run: story.base, outputs: {base: json}}
      gate: {type: check, run: story.gate, inputs: [base], outputs: {gate: json}}
      review: {type: gate, run: story.finish}
"""
"""The mini draft as the builder saved it before lines: run in listed order from `start`."""

PACKAGE_V1 = """\
workflows:
  pkg:
    start: package
    loose: [card, story, layers, base]
    nodes:
      package:
        type: ask
        role: architect
        prompt: story.ready_package
        run: story.ready_package
        inputs: [base, story, layers]
        outputs: {package: json}
"""
"""Review Focus 4: an old draft whose ask named its runner by `run` (the layer's package ask)."""


def up(text: str, name: str | None = None) -> tuple[dict[str, Any], list[str]]:
    data = yaml.safe_load(text)["workflows"]
    return upgrade_v1(data[name or next(iter(data))])


def lines(node: dict[str, Any]) -> list[Any]:
    return list(node.get("to", []))


# ---- the upgrade rules ----


def test_a_current_mapping_passes_through_unchanged() -> None:
    data = yaml.safe_load(MINI)["workflows"]["mini"]
    out, problems = upgrade_v1(data)
    assert out == data and out is not data and problems == []


def test_the_listed_order_and_next_become_lines_loose_becomes_inputs_and_start_goes() -> None:
    out, problems = up(
        "workflows:\n  w:\n    type: bug\n    start: a\n    loose: [card]\n    nodes:\n"
        "      a: {type: check, run: x.a}\n"
        "      b: {type: check, run: x.b, next: d}\n"
        "      c: {type: check, run: x.c}\n"
        "      d: {type: check, run: x.d}\n"
    )
    assert out == {
        "type": "bug",
        "inputs": ["card"],
        "nodes": {
            "a": {"type": "check", "run": "x.a", "to": [{"node": "b"}]},  # the node listed after it
            "b": {"type": "check", "run": "x.b", "to": [{"node": "d"}]},  # `next` over the order
            "c": {"type": "check", "run": "x.c", "to": [{"node": "d"}]},
            "d": {"type": "check", "run": "x.d"},  # the last listed: nothing after it
        },
    }
    # the old run went a, b, d: c had no line in it, which would make it a start now
    assert problems == [
        "node c: the old workflow started at 'a' and never reached it; draw a line into it or remove it"
    ]
    WorkflowSpec.model_validate(out)


def test_a_branch_becomes_one_conditioned_line_per_value_and_its_default_takes_what_none_holds() -> None:
    out, problems = up(
        "workflows:\n  w:\n    start: judge\n    nodes:\n"
        "      judge: {type: check, run: x.j, outputs: {verdict: text}, 'on': verdict,\n"
        "              branch: {ok: ship, 'needs work': fix, '*': ask}}\n"
        "      fix: {type: check, run: x.f, next: judge_again}\n"
        "      ask: {type: check, run: x.a}\n"
        "      ship: {type: check, run: x.s}\n"
        "      judge_again: {type: check, run: x.j2, 'on': verdict, branch: {ok: ship}}\n"
    )
    assert lines(out["nodes"]["judge"]) == [
        {"node": "ship", "when": "verdict == ok"},
        {"node": "fix", "when": 'verdict == "needs work"'},
        {"node": "ask", "when": 'not (verdict == ok or verdict == "needs work")'},
    ]
    # no `*`: the old schema fell through to the next node when no value matched
    assert lines(out["nodes"]["judge_again"]) == [
        {"node": "ship", "when": "verdict == ok"},
    ]  # the last listed node: nothing to fall through to
    assert problems == []


def test_a_loop_and_an_on_fail_loop_become_loop_lines_first_and_a_coded_loop_condition_is_a_problem() -> None:
    out, problems = up(
        "workflows:\n  w:\n    start: package\n    nodes:\n"
        "      package: {type: check, run: x.p, outputs: {package: json}}\n"
        "      build: {type: check, run: x.b, inputs: [package],\n"
        "              loop: {to: package, max: story.layer_attempts, carry: feedback}}\n"
        "      review: {type: check, run: x.r, on_fail: {to: build, max: 2}}\n"
        "      check: {type: check, run: x.c, loop: {to: review, max: 1, when: story.has_package}}\n"
    )
    nodes = out["nodes"]
    assert lines(nodes["build"]) == [
        {"node": "package", "loop": {"max": "story.layer_attempts", "carry": "feedback"}},
        {"node": "review"},
    ]
    assert lines(nodes["review"]) == [{"node": "build", "loop": {"max": 2}}, {"node": "check"}]
    assert "on_fail" not in nodes["review"]  # a spent loop blocks, as the old on_fail loop did
    assert lines(nodes["check"]) == [{"node": "review", "loop": {"max": 1}}]
    assert problems == ["node check: condition 'story.has_package' was code; write it as an expression"]
    WorkflowSpec.model_validate(out)


def test_a_parallel_nodes_children_take_the_lines_into_it_and_out_of_it() -> None:
    out, problems = up(
        "workflows:\n  w:\n    start: gate\n    nodes:\n"
        "      gate: {type: check, run: x.g}\n"
        "      both: {type: parallel, nodes: [refine, survey]}\n"
        "      survey: {type: check, run: x.s, outputs: {survey: markdown}}\n"
        "      refine: {type: check, run: x.r, outputs: {story: json}}\n"
        "      costed: {type: check, run: x.c}\n"
        "    layout: {both: {x: 1, y: 2}, gate: {x: 0, y: 0}}\n"
    )
    nodes = out["nodes"]
    assert "both" not in nodes and out["layout"] == {"gate": {"x": 0, "y": 0}}
    assert lines(nodes["gate"]) == [{"node": "refine"}, {"node": "survey"}]
    assert lines(nodes["refine"]) == lines(nodes["survey"]) == [{"node": "costed"}]
    assert problems == []
    WorkflowSpec.model_validate(out)


def test_an_each_node_becomes_a_bundle_over_its_list_which_it_reads() -> None:
    out, problems = up(
        "workflows:\n  w:\n    start: build_layers\n    loose: [layers]\n    nodes:\n"
        "      build_layers: {type: each, over: layers, workflow: layer}\n"
        "      built: {type: check, run: x.b}\n"
    )
    assert out["nodes"]["build_layers"] == {
        "type": "bundle",
        "workflow": "layer",
        "each": "layers",
        "inputs": ["layers"],
        "to": [{"node": "built"}],
    }
    assert problems == []


def test_a_nodes_when_was_code_and_is_a_problem() -> None:
    out, problems = up(
        "workflows:\n  w:\n    start: a\n    nodes:\n"
        "      a: {type: check, run: x.a, when: story.to_build}\n"
        "      b: {type: check, run: x.b}\n"
    )
    assert "when" not in out["nodes"]["a"] and lines(out["nodes"]["a"]) == [{"node": "b"}]
    assert problems == ["node a: condition 'story.to_build' was code; write it as an expression"]


def test_a_model_nodes_code_ids_are_dropped_and_it_must_have_its_prompt_written() -> None:
    out, problems = up(
        "workflows:\n  w:\n    start: refine\n    loose: [card]\n    nodes:\n"
        "      refine: {type: ask, role: product, prompt: story.refine, parse: story.story,\n"
        "               after: story.refined, inputs: [card], outputs: {story: json}, steps: 3}\n"
        "      note: {type: ask, role: product, prompt: 'prompt:mini-note', inputs: [card],\n"
        "             outputs: {note: text}}\n"
        "      layers: {type: conversation, role: architect, prompt: 'prompt:plan', turn: story.turn,\n"
        "               args: {turns: story.layers_turns}, inputs: [card], outputs: {layers: list}}\n"
    )
    nodes = out["nodes"]
    assert nodes["refine"] == {
        "type": "ask",
        "role": "product",
        "inputs": ["card"],
        "outputs": {"story": "json"},
        "steps": 3,
        "to": [{"node": "note"}],
    }
    assert nodes["note"]["prompt"] == "prompt:mini-note"  # a prompt file is configuration: kept
    assert nodes["layers"]["prompt"] == "prompt:plan" and "turn" not in nodes["layers"]
    assert "args" not in nodes["layers"]
    assert problems == ["node refine: write this node's prompt", "node layers: write this node's prompt"]
    WorkflowSpec.model_validate(out)


def test_a_node_the_old_start_never_reached_is_a_problem_and_so_is_an_unknown_start() -> None:
    _, problems = up(
        "workflows:\n  w:\n    start: b\n    nodes:\n"
        "      a: {type: check, run: x.a}\n"
        "      b: {type: check, run: x.b}\n"
    )
    assert problems == [
        "node a: the old workflow started at 'b' and never reached it; draw a line into it or remove it"
    ]
    _, problems = up("workflows:\n  w:\n    start: x\n    nodes: {}\n")
    assert problems == ["start names unknown node 'x'"]


def test_with_the_catalog_a_check_reads_what_its_operation_declares() -> None:
    data = yaml.safe_load(MINI_V1)["workflows"]["mini"]
    out, problems = upgrade_v1(data, catalog=STORY_CATALOG)
    assert problems == []
    assert {n: v.get("inputs") for n, v in out["nodes"].items()} == {
        "intake": ["card"],
        "base": ["card", "repo"],
        "gate": ["base", "repo"],
        "review": ["card"],
    }


# ---- old drafts ----


def test_an_old_draft_upgrades_and_resolves() -> None:
    cfg = load_config(Path("airlie.yaml"))
    assert workflow_problems(cfg, {"mini": MINI_V1}, "mini") == []
    assert set(resolve_workflows(cfg, {"mini": MINI_V1}, "mini")) == {"mini"}


async def test_an_old_mini_draft_runs_its_nodes_in_the_old_order(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    pipe = pipeline(tmp_path, repo, happy_engine(), composer=False)
    pipe.drafts = lambda: {"mini": MINI_V1}
    card = mini_card()
    await pipe.plan(card)
    st = await pipe.build(card)
    assert st.blocked is None and st.stage == "review", st.blocked
    assert [n for n, _ in started(st.path / "events.jsonl")] == ["intake", "base", "gate", "review"]


def test_an_old_draft_with_run_on_an_ask_is_a_problem_not_a_raise(tmp_path: Path) -> None:
    """Review Focus 4: the draft loads (the builder can open it), and what stops it running is listed where
    a person reads problems: workflow_problems, the Workflows list."""
    cfg = load_config(Path("airlie.yaml"))
    spec = draft_spec("pkg", PACKAGE_V1)  # no raise: the code ids are gone
    assert spec.nodes["package"].run is None and spec.nodes["package"].prompt is None
    [problem] = workflow_problems(cfg, {"pkg": PACKAGE_V1}, "pkg")
    assert "workflow pkg: node package: write this node's prompt" in problem
    with pytest.raises(WorkflowError, match="write this node's prompt"):
        resolve_workflows(cfg, {"pkg": PACKAGE_V1}, "pkg")
    c = cfg_for(tmp_path)
    s = BoardStore(c.observability.state_dir.expanduser() / "board.sqlite", prefix=c.board.prefix)
    s.save_draft("pkg", type=None, yaml=PACKAGE_V1)
    assert [d.name for d in s.drafts()] == ["pkg"]  # the store loads it as it is
    http = TestClient(_router(c, s))
    with http:
        rows = {w["name"]: w for w in http.get("/v1/workflows").json()["workflows"]}
        read = http.get("/v1/workflows/pkg")
    assert any("write this node's prompt" in p for p in rows["pkg"]["problems"])
    assert read.status_code == 200 and any("write this node's prompt" in p for p in read.json()["problems"])


# ---- old snapshots ----


_V1_NODE_DEFAULTS: dict[str, Any] = {
    "role": None, "prompt": None, "turn": None, "parse": None, "run": None, "after": None, "inputs": [],
    "outputs": {}, "steps": None, "tools": None, "done": None, "on_fail": "block", "when": None, "next": None,
    "on": None, "branch": None, "loop": None, "nodes": [], "over": None, "workflow": None, "args": {},
    "session": None, "advisor": None,
}  # fmt: skip
"""What the old `NodeSpec.model_dump(mode="json")` wrote for a field left unset."""


def v1_snapshot(text: str, name: str) -> dict[str, Any]:
    """The snapshot an airlie from before lines saved for workflow `name` of `text`: every field dumped."""
    data = yaml.safe_load(text)["workflows"][name]
    nodes = {n: {**_V1_NODE_DEFAULTS, **body} for n, body in data["nodes"].items()}
    spec = {"type": data.get("type"), "start": data["start"], "loose": data.get("loose", []), "nodes": nodes}
    return {"name": name, "specs": {name: {**spec, "layout": {}}}, "prompts": {}}


async def test_a_snapshot_saved_before_lines_is_upgraded_and_the_card_finishes_on_it(tmp_path: Path) -> None:
    old = v1_snapshot(MINI_V1, "mini")
    name, specs, prompts = from_snapshot(json.loads(json.dumps(old)))
    assert (
        name == "mini" and prompts == {} and [ln.node for ln in specs["mini"].nodes["intake"].to] == ["base"]
    )
    repo = repo_with_marker_tests(tmp_path)
    pipe = pipeline(tmp_path, repo, happy_engine(), composer=False)
    pipe.drafts = lambda: {"mini": MINI}
    card = mini_card()
    st = await pipe.plan(card)
    saved = json.loads((st.path / "state.json").read_text())
    saved["workflow"] = old  # the card started before this airlie
    (st.path / "state.json").write_text(json.dumps(saved))
    pipe.drafts = lambda: {"mini": MINI_MORE}  # edited since: the card keeps what it started on
    st = await pipe.build(card)
    assert st.blocked is None and st.stage == "review", st.blocked
    assert [n for n, _ in started(st.path / "events.jsonl")] == ["intake", "base", "gate", "review"]


def test_a_snapshot_whose_upgrade_leaves_code_is_a_workflow_error_listing_it() -> None:
    old = v1_snapshot(PACKAGE_V1, "pkg")
    with pytest.raises(WorkflowError, match="snapshot of 'pkg' is the old schema") as e:
        from_snapshot(old)
    assert "workflow pkg: node package: write this node's prompt" in str(e.value)


# ---- adopting the current workflow ----


async def test_adopting_the_current_workflow_replaces_the_snapshot_and_the_next_run_skips_what_exists(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    board = RecordingBoard()
    pipe = pipeline(tmp_path, repo, happy_engine(), composer=False, board=board)
    pipe.drafts = lambda: {"mini": MINI}
    card = mini_card()
    await pipe.plan(card)
    st = await pipe.build(card)
    assert st.blocked is None and st.stage == "review", st.blocked
    old = st.workflow
    base, gate, done = st.base, st.gate, list(st.nodes_done)
    before = len(kinds_all(st.path / "events.jsonl"))

    pipe.drafts = lambda: {"mini": MINI_MORE}
    new = await adopt_current(pipe, st, card)
    assert new == st.workflow and "costed" in new["specs"]["mini"]["nodes"]
    saved = json.loads((st.path / "state.json").read_text())
    assert saved["workflow"] == new and saved["nodes_done"] == done and saved["base"] == base
    assert (st.base, st.gate) == (base, gate)
    [adopted] = [e for e in kinds_all(st.path / "events.jsonl")[before:] if e["kind"] == "workflow_adopted"]
    assert adopted["name"] == "mini" and adopted["from"] == spec_hash(old) and adopted["to"] == spec_hash(new)
    assert adopted["from"] != adopted["to"]
    assert "now runs the current mini workflow" in board.notes[-1] and str(adopted["to"]) in board.notes[-1]
    mark = len(kinds_all(st.path / "events.jsonl"))

    st = await pipe.build(card)
    assert st.blocked is None and st.stage == "review", st.blocked
    later = kinds_all(st.path / "events.jsonl")[mark:]
    ran = {e["node"] for e in later if e["kind"] == "node_started"}
    skipped = {e["node"] for e in later if e["kind"] == "node_skipped"}
    assert "costed" in ran  # new in the workflow: it runs
    assert "base" in skipped and "base" not in ran  # its output is there (the finish clears the gate's)


async def test_adopting_is_refused_while_the_story_runs_or_when_it_keeps_no_snapshot(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    pipe = pipeline(tmp_path, repo, happy_engine(), composer=False, board=RecordingBoard())
    pipe.drafts = lambda: {"mini": MINI}
    card = mini_card()
    st = await pipe.plan(card)
    snap = st.workflow
    pipe.drafts = lambda: {"mini": MINI_MORE}
    with pytest.raises(StoryRunning, match="running now"):
        await adopt_current(pipe, st, card, running=lambda job: job == f"story-{card.key}")
    assert json.loads((st.path / "state.json").read_text())["workflow"] == snap
    plain = text_card("a story the driver runs")
    driven = StoryState.open(pipe.stories_dir / plain.key, plain, pipe.repo)  # no snapshot
    with pytest.raises(WorkflowError, match="keeps no workflow snapshot"):
        await adopt_current(pipe, driven, plain)
    pipe.drafts = lambda: {"mini": MINI.replace("run: story.intake,", "")}  # the current one is broken
    with pytest.raises(WorkflowError, match="check needs run"):
        await adopt_current(pipe, st, card)
    assert json.loads((st.path / "state.json").read_text())["workflow"] == snap


async def test_adopt_one_adopts_for_a_card_on_the_board(tmp_path: Path) -> None:
    from airlie.story.factory import make_pipeline, stories_dir
    from airlie.story.local_board import LocalBoard
    from tests.unit.test_story_factory import STORY_ROLES

    repo = repo_with_marker_tests(tmp_path)
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"composer": False}})
    store = BoardStore(cfg.observability.state_dir / "board.sqlite", prefix=cfg.board.prefix)
    board = LocalBoard(store, stories_dir=stories_dir(cfg))
    store.save_draft("mini", type="bug", yaml=MINI)
    init = store.create(level="initiative", title="i")
    epic = store.create(level="epic", title="e", parent=init.key)
    story = store.create(level="story", title="fix it", parent=epic.key, labels=["workflow:mini"])
    pipe = make_pipeline(cfg, board=board, repo=repo, test_cmd="./check.sh")
    st = await pipe.plan(await board.card(story.key))
    assert st.workflow is not None and "costed" not in st.workflow["specs"]["mini"]["nodes"]
    store.save_draft("mini", type="bug", yaml=MINI_MORE)
    adopted = await factory.adopt_one(cfg, story.key, by="noah")
    assert adopted.name == "mini" and adopted.before == spec_hash(st.workflow) and adopted.after
    saved = json.loads((stories_dir(cfg) / story.key / "state.json").read_text())
    assert "costed" in saved["workflow"]["specs"]["mini"]["nodes"]
    assert any(
        "now runs the current mini workflow" in c.text and "as noah asked" in c.text
        for c in store.get(story.key).comments
    )
    fresh = store.create(level="story", title="not yet", parent=epic.key, labels=["workflow:mini"])
    with pytest.raises(WorkflowError, match="has not started"):
        await factory.adopt_one(cfg, fresh.key)
    assert not (stories_dir(cfg) / fresh.key).exists()


# ---- the route and the command ----


def _app(tmp_path: Path, adopt: Any, **kw: Any) -> Any:
    return create_app(
        cfg_for(tmp_path), transport=httpx.MockTransport(FakeUpstream()), story_adopt=adopt, **kw
    )


def test_the_adopt_route_runs_as_the_person_signed_in_and_says_what_changed(tmp_path: Path) -> None:
    asked: list[tuple[str, str, bool]] = []

    async def adopt(cfg: Any, key: str, *, by: str, running: Any) -> factory.Adopted:
        asked.append((key, by, running(f"story-{key}")))
        return factory.Adopted(key, "mini", "aaa", "bbb")

    with TestClient(_app(tmp_path, adopt)) as client:
        r = client.post("/v1/airlie/stories/AIR-7/adopt-workflow")
    assert r.status_code == 200 and r.json() == {"key": "AIR-7", "name": "mini", "from": "aaa", "to": "bbb"}
    [(key, by, running)] = asked
    assert key == "AIR-7" and by and running is False


def test_the_adopt_route_refuses_a_running_story_409_and_a_workflow_that_does_not_resolve_422(
    tmp_path: Path,
) -> None:
    failures: list[Exception] = [
        StoryRunning("story AIR-7 is running now"),
        WorkflowError("no workflow named 'mini'"),
    ]

    async def adopt(cfg: Any, key: str, *, by: str, running: Any) -> factory.Adopted:
        raise failures.pop(0)

    with TestClient(_app(tmp_path, adopt)) as client:
        running = client.post("/v1/airlie/stories/AIR-7/adopt-workflow")
        broken = client.post("/v1/airlie/stories/AIR-7/adopt-workflow")
    assert running.status_code == 409 and "running now" in running.json()["detail"]
    assert broken.status_code == 422 and "no workflow named" in broken.json()["detail"]


def test_the_adopt_route_needs_a_sign_in(tmp_path: Path) -> None:
    async def adopt(cfg: Any, key: str, *, by: str, running: Any) -> factory.Adopted:
        raise AssertionError("not without a sign-in")

    with TestClient(_app(tmp_path, adopt, api_key="k")) as client:
        assert client.post("/v1/airlie/stories/AIR-7/adopt-workflow").status_code == 401


def test_story_adopt_workflow_says_what_it_did_and_why_it_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.test_cli_story import story_cfg

    runner = CliRunner()
    p = str(story_cfg(tmp_path))

    async def adopted(cfg: Any, key: str, **kw: Any) -> factory.Adopted:
        return factory.Adopted(key, "mini", "aaa", "bbb")

    monkeypatch.setattr(factory, "adopt_one", adopted)
    r = runner.invoke(cli.app, ["story", "adopt-workflow", "AIR-7", "--config", p])
    assert r.exit_code == 0 and "AIR-7 now runs the current mini workflow (bbb, was aaa)" in r.output

    async def refused(cfg: Any, key: str, **kw: Any) -> factory.Adopted:
        raise StoryRunning("story AIR-7 is running now (its job story-AIR-7 is running)")

    monkeypatch.setattr(factory, "adopt_one", refused)
    r = runner.invoke(cli.app, ["story", "adopt-workflow", "AIR-7", "--config", p])
    assert r.exit_code == 1 and "running now" in r.output
    assert runner.invoke(cli.app, ["story", "adopt-workflow", "--config", p]).exit_code == 2


# ---- use tracking ----


def _router(c: Any, s: BoardStore) -> FastAPI:
    async def unwired(draft: Any) -> tuple[int, str]:
        raise AssertionError("not published here")

    app = FastAPI()
    app.include_router(workflows_router(c, s, lambda topic, key: None, unwired))
    return app


def _shipped(tmp_path: Path) -> tuple[Any, BoardStore]:
    from tests.unit.test_workflows_api import cfg, store

    c = cfg(tmp_path)
    return c, store(c)


def test_used_by_names_the_workflows_whose_bundle_nodes_run_it(tmp_path: Path) -> None:
    c, _ = _shipped(tmp_path)
    old_each = MINI_V1.replace("  mini:", "  old:").replace(
        "      review:", "      build: {type: each, over: layers, workflow: tiny}\n      review:"
    )
    drafts = {"mini": MINI_EACH, "tiny": TINY, "old": old_each, "junk": "workflows: [1"}
    assert used_by(c, drafts, "tiny") == ["mini", "old"]
    assert used_by(c, drafts, "layer") == ["story"]
    assert used_by(c, drafts, "mini") == [] and used_by(c, {}, "story") == []


def test_the_list_says_where_each_workflow_is_used(tmp_path: Path) -> None:
    c, s = _shipped(tmp_path)
    s.save_draft("mini", type="bug", yaml=MINI_EACH)
    s.save_draft("tiny", type=None, yaml=TINY)
    with TestClient(_router(c, s)) as http:
        rows = {w["name"]: w["used_by"] for w in http.get("/v1/workflows").json()["workflows"]}
    assert rows == {"story": [], "layer": ["story"], "mini": [], "tiny": ["mini"]}


def test_deleting_or_renaming_a_bundle_in_use_is_refused_with_its_users(tmp_path: Path) -> None:
    c, s = _shipped(tmp_path)
    s.save_draft("mini", type="bug", yaml=MINI_EACH)
    s.save_draft("tiny", type=None, yaml=TINY)
    renamed = TINY.replace("  tiny:", "  small:")
    with TestClient(_router(c, s)) as http:
        gone = http.delete("/v1/workflows/drafts/tiny")
        moved = http.put("/v1/workflows/drafts/tiny", json={"type": None, "yaml": renamed})
        assert (gone.status_code, gone.json()) == (409, {"detail": {"used_by": ["mini"]}})
        assert (moved.status_code, moved.json()) == (409, {"detail": {"used_by": ["mini"]}})
        assert s.draft("tiny").yaml == TINY
        # a save that keeps the name is no rename; a rename of an unused draft is the old refusal
        assert http.put("/v1/workflows/drafts/tiny", json={"type": None, "yaml": TINY}).status_code == 200
        lone = http.put(
            "/v1/workflows/drafts/mini", json={"type": "bug", "yaml": MINI.replace("  mini:", "  x:")}
        )
        assert lone.status_code == 422
        # once nothing runs it, it goes
        assert http.put("/v1/workflows/drafts/mini", json={"type": "bug", "yaml": MINI}).status_code == 200
        assert http.delete("/v1/workflows/drafts/tiny").status_code == 204
        # a draft that edits a file is discarded freely: the file still serves its users
        layer = yaml.safe_dump({"workflows": {"layer": c.workflows["layer"].model_dump(mode="json")}})
        assert http.put("/v1/workflows/drafts/layer", json={"type": None, "yaml": layer}).status_code == 200
        assert http.delete("/v1/workflows/drafts/layer").status_code == 204


def test_snapshot_round_trip_is_untouched_by_the_upgrade() -> None:
    cfg = load_config(Path("airlie.yaml"))
    specs = resolve_workflows(cfg, {}, "story")
    assert from_snapshot(json.loads(json.dumps(snapshot("story", specs))))[1] == specs
