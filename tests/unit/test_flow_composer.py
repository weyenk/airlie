import asyncio
import json
from types import SimpleNamespace

import pytest

from airlie.config import ConfigError
from airlie.flow import Composer, EndRun, MemoryStore, Registry, Sessions, Stalled, resolve_count
from airlie.flow.spec import WorkflowSpec, check_workflows
from airlie.story.asking import StoryBlocked
from tests.unit.test_studio_orchestrator import cfg_for


def composer(tmp_path, workflows: dict, runners: dict, *, store=None, registry=None, extra=None):
    """A composer over `workflows` (schema v2 mappings, checked as the config checks them, without the
    story's catalog: the runners here are fakes)."""
    specs = {name: WorkflowSpec.model_validate(w) for name, w in workflows.items()}
    assert check_workflows(specs) == []
    return Composer(
        cfg_for(tmp_path, extra=extra),
        runners=runners,
        registry=registry or Registry(),
        store=store or MemoryStore({"card": "c"}),
        sessions=Sessions(),
        workflows=specs,
    )


def check_that(fn):
    async def run(node, ctx):
        return fn(node, ctx)

    return run


def outputs(seen):
    """A runner that records the node it runs and returns every declared output, valued by the node."""
    return check_that(lambda n, c: (seen.append(c.node), {k: c.node for k in n.outputs})[1])


def chk(**kw):
    return {"type": "check", "run": "r", **kw}


def started(store):
    return [f["node"] for k, f in store.events if k == "node_started"]


def skipped(store):
    return [f["node"] for k, f in store.events if k == "node_skipped"]


async def test_two_lines_out_run_both_targets_at_once(tmp_path):
    both = asyncio.Event()
    entered = set()

    async def runner(node, ctx):
        if ctx.node in ("b", "c"):
            entered.add(ctx.node)
            if entered == {"b", "c"}:
                both.set()
            await asyncio.wait_for(both.wait(), 5)  # neither finishes before the other has started
        return {k: ctx.node for k in node.outputs}

    wf = {
        "w": {
            "nodes": {
                "a": chk(outputs={"x": "text"}, to=["b", "c"]),
                "b": chk(inputs=["x"], outputs={"y": "text"}),
                "c": chk(inputs=["x"], outputs={"z": "text"}),
            }
        }
    }
    c = composer(tmp_path, wf, {"check": runner})
    await c.run("w")
    kinds = [(k, f["node"]) for k, f in c.store.events]
    first_finish = min(kinds.index(("node_finished", n)) for n in ("b", "c"))
    assert kinds.index(("node_started", "b")) < first_finish
    assert kinds.index(("node_started", "c")) < first_finish
    assert c.store.get("y") == "b" and c.store.get("z") == "c"


async def test_a_node_waits_for_every_line_in_and_runs_once(tmp_path):
    """`d` reads `y` from b and nothing from c: the line from c only orders, and d still waits for it."""
    seen = []

    async def runner(node, ctx):
        if ctx.node == "c":
            await asyncio.sleep(0.05)
        seen.append(ctx.node)
        return {k: ctx.node for k in node.outputs}

    wf = {
        "w": {
            "nodes": {
                "a": chk(outputs={"x": "text"}, to=["b", "c"]),
                "b": chk(inputs=["x"], outputs={"y": "text"}, to=["d"]),
                "c": chk(inputs=["x"], outputs={"z": "text"}, to=["d"]),
                "d": chk(inputs=["y"], outputs={"done": "text"}),
            }
        }
    }
    c = composer(tmp_path, wf, {"check": runner})
    assert await c.run("w") == "d"
    assert seen == ["a", "b", "c", "d"]
    assert started(c.store).count("d") == 1


async def test_a_dead_line_counts_as_satisfied(tmp_path):
    """refine or take_card (exclusive conditions on the lines into them), both producing `story`: the line
    not taken kills refine and its line on, and layers runs once on take_card's story."""
    seen = []
    wf = {
        "w": {
            "inputs": ["card"],
            "nodes": {
                "gate": chk(
                    to=[
                        {"node": "refine", "when": "card.level != story"},
                        {"node": "take_card", "when": "card.level == story"},
                    ]
                ),
                "refine": chk(outputs={"story": "json"}, to=["layers"]),
                "take_card": chk(outputs={"story": "json"}, to=["layers"]),
                "layers": chk(inputs=["story"], outputs={"layers": "json"}),
            },
        }
    }
    store = MemoryStore({"card": {"level": "story"}})
    c = composer(tmp_path, wf, {"check": outputs(seen)}, store=store)
    assert await c.run("w") == "layers"
    assert seen == ["gate", "take_card", "layers"]
    assert skipped(store) == ["refine"] and store.get("story") == "take_card"


async def test_a_loop_line_reruns_the_target_and_everything_downstream_and_clears_their_outputs(tmp_path):
    calls = []

    def run(n, c):
        calls.append((c.node, c.feedback, c.store.has("y"), c.store.done("c")))
        if c.node == "c":
            again = sum(1 for name, *_ in calls if name == "c") == 1
            return {"verdict": {"again": again}, "note": "look again"}
        return {k: c.node for k in n.outputs}

    wf = {
        "w": {
            "nodes": {
                "a": chk(outputs={"x": "text"}, to=["b"]),
                "b": chk(inputs=["x"], outputs={"y": "text"}, to=["c"]),
                "c": chk(
                    inputs=["y"],
                    outputs={"verdict": "json"},
                    to=[
                        {"node": "a", "when": "verdict.again == true", "loop": {"max": 3, "carry": "note"}},
                        {"node": "d", "when": "verdict.again == false"},
                    ],
                ),
                "d": chk(inputs=["verdict"], outputs={"z": "text"}),
            }
        }
    }
    c = composer(tmp_path, wf, {"check": check_that(run)})
    assert await c.run("w") == "d"
    assert [name for name, *_ in calls] == ["a", "b", "c", "a", "b", "c", "d"]
    # the second a reads the carry, finds b's output cleared and c out of the done set
    assert calls[3] == ("a", "look again", False, False)
    assert c.store.get("verdict") == {"again": False} and not c.store.has("note")
    assert all(c.store.done(n) for n in "abcd")


@pytest.mark.parametrize("on_fail", ["block", "continue"])
async def test_a_loop_bound_spent_blocks_or_continues_by_on_fail(tmp_path, on_fail):
    calls = []
    seen = []

    def run(n, c):
        if c.node == "a":
            calls.append(c.feedback)
            return {"__fail__": "red", "tail": f"tail{len(calls)}"}
        seen.append(c.node)
        return {"y": 1}

    wf = {
        "w": {
            "nodes": {
                "a": chk(
                    outputs={"x": "text"},
                    on_fail=on_fail,
                    to=[{"node": "a", "loop": {"max": "story.layer_attempts", "carry": "tail"}}, "b"],
                ),
                "b": chk(outputs={"y": "text"}),
            }
        }
    }
    c = composer(tmp_path, wf, {"check": check_that(run)}, extra={"story": {"layer_attempts": 1}})
    if on_fail == "block":
        with pytest.raises(StoryBlocked, match="red"):
            await c.run("w")
        assert seen == []
    else:
        assert await c.run("w") == "b"
        assert seen == ["b"] and not c.store.has("x") and not c.store.done("a")
    assert calls == [None, "tail1"]


async def test_a_bundle_runs_once_per_item_and_writes_back_only_its_declared_outputs(tmp_path):
    store = MemoryStore({"card": "c", "layers": [{}, {}, {}]})
    store.put("built.01", {"sha": "a"})
    seen = []

    def run(n, c):
        seen.append((c.node, c.item))
        return {"built.{item}": {"sha": c.item}, "note": "x"}

    wf = {
        "story": {
            "inputs": ["card", "layers"],
            "nodes": {
                "lay": {"type": "bundle", "workflow": "layer", "each": "layers", "inputs": ["layers"]},
            },
        },
        "layer": {
            "inputs": ["layers"],
            "outputs": ["built.{item}"],
            "nodes": {"b": chk(inputs=["layers"], outputs={"built.{item}": "json"})},
        },
    }
    c = composer(tmp_path, wf, {"check": check_that(run)}, store=store)
    assert await c.run("story") == "lay"
    assert seen == [("b", "02"), ("b", "03")] and store.get("built.03") == {"sha": "03"}
    assert not store.has("note")
    events = [(k, f["node"], f.get("item")) for k, f in store.events]
    assert events[0] == ("node_started", "lay", None) and events[-1][:2] == ("node_finished", "lay")
    assert ("node_skipped", "b", "01") in events
    assert store.done("lay/b@02") and store.done("lay/b@03") and store.done("lay")

    # a bundle node writing a name its workflow does not declare as an output is refused
    wf["layer"]["nodes"]["b"]["outputs"] = {"built.{item}": "json", "note": "text"}
    store = MemoryStore({"card": "c", "layers": [{}]})
    with pytest.raises(KeyError, match="'note' is not one of its outputs"):
        await composer(tmp_path, wf, {"check": check_that(run)}, store=store).run("story")


async def test_a_node_done_by_its_artifacts_is_skipped_on_resume_and_an_outputless_node_by_the_done_set(
    tmp_path,
):
    wf = {
        "w": {
            "nodes": {
                "a": chk(outputs={"x": "text"}, to=["mark"]),
                "mark": chk(to=["b"]),
                "b": chk(inputs=["x"], outputs={"y": "text"}),
            }
        }
    }
    store = MemoryStore({"card": "c", "x": "old"})
    store.mark_done("mark")
    seen = []
    c = composer(tmp_path, wf, {"check": outputs(seen)}, store=store)
    assert await c.run("w") == "b"
    assert seen == ["b"] and store.get("x") == "old"
    assert skipped(store) == ["a", "mark"] and started(store) == ["b"]

    store = MemoryStore({"card": "c", "x": "old"})  # the mark not recorded: it runs
    seen = []
    await composer(tmp_path, wf, {"check": outputs(seen)}, store=store).run("w")
    assert seen == ["mark", "b"] and store.done("mark")


async def test_until_returns_after_the_named_node(tmp_path):
    seen = []
    wf = {
        "w": {
            "nodes": {
                "a": chk(outputs={"x": "text"}, to=["b"]),
                "b": chk(outputs={"y": "text"}, to=["c"]),
                "c": chk(outputs={"z": "text"}),
            }
        }
    }
    assert await composer(tmp_path, wf, {"check": outputs(seen)}).run("w", until="b") == "b"
    assert seen == ["a", "b"]

    # a failed node whose loop is spent still blocks: `until` is reached only by a node that finished
    failing = {"w": {"nodes": {"a": chk(outputs={"x": "text"}, to=[{"node": "a", "loop": {"max": 1}}])}}}
    runner = check_that(lambda n, c: {"__fail__": "red"})
    with pytest.raises(StoryBlocked):
        await composer(tmp_path, failing, {"check": runner}).run("w", until="a")


async def test_a_stalled_workflow_names_what_it_waits_for(tmp_path):
    wf = {
        "w": {
            "inputs": ["ghost"],
            "nodes": {
                "a": chk(outputs={"x": "text"}),
                "b": chk(inputs=["ghost", "x"], outputs={"y": "text"}),
            },
        }
    }
    seen = []
    c = composer(tmp_path, wf, {"check": outputs(seen)})
    with pytest.raises(Stalled, match="^workflow w: nothing can run; waiting: b: ghost$"):
        await c.run("w")
    assert seen == ["a"] and "b" not in started(c.store)


async def test_a_condition_reads_outputs_the_card_and_config(tmp_path):
    seen = []
    looked = {}

    def run(n, c):
        seen.append(c.node)
        if c.node == "a":
            return {"verdict": {"decision": "object"}}
        if c.node == "b":
            looked["card"] = c.lookup("card.level")
            looked["config"] = c.lookup("config.story.layer_attempts")
            looked["verdict"] = c.lookup("verdict.decision")
            for missing in ("verdict.nope", "card.nope", "config.story.nope", "nope"):
                with pytest.raises(KeyError):
                    c.lookup(missing)
        return {k: 1 for k in n.outputs}

    wf = {
        "w": {
            "inputs": ["card"],
            "nodes": {
                "a": chk(
                    outputs={"verdict": "json"},
                    to=[
                        {"node": "b", "when": "verdict.decision == object and card.level == story"},
                        {"node": "c", "when": "config.story.layer_attempts == 2"},
                        {"node": "d", "when": "verdict.decision == accept or not card.level exists"},
                    ],
                ),
                "b": chk(outputs={"x": "text"}),
                "c": chk(outputs={"y": "text"}),
                "d": chk(outputs={"z": "text"}),
            },
        }
    }
    store = MemoryStore({"card": SimpleNamespace(level="story")})
    c = composer(
        tmp_path, wf, {"check": check_that(run)}, store=store, extra={"story": {"layer_attempts": 2}}
    )
    await c.run("w")
    assert sorted(seen) == ["a", "b", "c"] and skipped(store) == ["d"]
    assert looked == {"card": "story", "config": 2, "verdict": "object"}


async def test_feedback_carried_in_one_item_is_not_read_by_the_next(tmp_path):
    store = MemoryStore({"card": "c", "layers": [{}, {}]})
    calls = []

    def build(n, c):
        calls.append((c.item, c.feedback))
        first = sum(1 for item, _ in calls if item == c.item) == 1
        return {"__fail__": "red", "tail": f"red {c.item}"} if first else {"built.{item}": c.item}

    wf = {
        "story": {
            "inputs": ["card", "layers"],
            "nodes": {"lay": {"type": "bundle", "workflow": "layer", "each": "layers", "inputs": ["layers"]}},
        },
        "layer": {
            "inputs": ["layers"],
            "outputs": ["built.{item}"],
            "nodes": {
                "b": chk(
                    outputs={"built.{item}": "json"},
                    to=[{"node": "b", "loop": {"max": 1, "carry": "tail"}}],
                )
            },
        },
    }
    await composer(tmp_path, wf, {"check": check_that(build)}, store=store).run("story")
    assert calls == [("01", None), ("01", "red 01"), ("02", None), ("02", "red 02")]


async def test_until_on_a_node_skipped_by_its_outputs_stops_there(tmp_path):
    seen = []
    wf = {
        "w": {
            "nodes": {
                "a": chk(outputs={"x": "text"}, to=["b"]),
                "b": chk(outputs={"y": "text"}, to=["c"]),
                "c": chk(outputs={"z": "text"}),
            }
        }
    }
    store = MemoryStore({"card": "c", "y": "kept"})
    assert await composer(tmp_path, wf, {"check": outputs(seen)}, store=store).run("w", until="b") == "b"
    assert seen == ["a"] and skipped(store) == ["b"] and not store.has("z")


async def test_a_loop_back_to_a_bundle_runs_its_items_again(tmp_path):
    seen = []

    def run(n, c):
        seen.append((c.node, c.item))
        if c.node == "rev":
            return {"verdict": {"n": sum(1 for name, _ in seen if name == "rev")}}
        return {"built.{item}": {"try": len(seen)}}

    wf = {
        "story": {
            "inputs": ["card", "layers"],
            "nodes": {
                "lay": {
                    "type": "bundle",
                    "workflow": "layer",
                    "each": "layers",
                    "inputs": ["layers"],
                    "to": ["rev"],
                },
                "rev": chk(
                    outputs={"verdict": "json"},
                    to=[{"node": "lay", "when": "verdict.n == 1", "loop": {"max": 1}}],
                ),
            },
        },
        "layer": {
            "inputs": ["layers"],
            "outputs": ["built.{item}"],
            "nodes": {"b": chk(outputs={"built.{item}": "json"})},
        },
    }
    store = MemoryStore({"card": "c", "layers": [{}]})
    await composer(tmp_path, wf, {"check": check_that(run)}, store=store).run("story")
    assert seen == [("b", "01"), ("rev", None), ("b", "01"), ("rev", None)]
    assert store.get("built.01") == {"try": 3} and store.done("lay/b@01")


async def test_the_done_set_skips_only_an_outputless_node_and_a_re_run_producer_is_waited_for(tmp_path):
    """`a` is in the done set but its output was cleared: it runs again, and `b`, reading it without a line
    from it, waits for it (`a` leaves the done set as it starts). `mark`, with no outputs, is skipped."""
    order = []

    async def runner(node, ctx):
        order.append((ctx.node, "start", ctx.store.done("a")))
        if ctx.node == "a":
            await asyncio.sleep(0.02)
        order.append((ctx.node, "end", None))
        return {k: ctx.node for k in node.outputs}

    wf = {
        "w": {
            "nodes": {
                "a": chk(outputs={"x": "text"}),
                "b": chk(inputs=["x"], outputs={"y": "text"}),
                "mark": chk(),
            }
        }
    }
    store = MemoryStore({"card": "c"})
    for name in ("a", "mark"):
        store.mark_done(name)
    await composer(tmp_path, wf, {"check": runner}, store=store).run("w")
    assert order == [("a", "start", False), ("a", "end", None), ("b", "start", True), ("b", "end", None)]
    assert skipped(store) == ["mark"] and store.get("y") == "b"


async def test_two_bundle_nodes_over_one_workflow_resume_independently(tmp_path):
    seen = []
    wf = {
        "story": {
            "inputs": ["card", "layers"],
            "nodes": {
                "l1": {"type": "bundle", "workflow": "layer", "each": "layers", "inputs": ["layers"]},
                "l2": {"type": "bundle", "workflow": "layer", "each": "layers", "inputs": ["layers"]},
            },
        },
        "layer": {"inputs": ["layers"], "nodes": {"mark": chk()}},
    }
    store = MemoryStore({"card": "c", "layers": [{}]})
    store.mark_done("l1/mark@01")
    await composer(tmp_path, wf, {"check": outputs(seen)}, store=store).run("story")
    assert seen == ["mark"] and store.done("l2/mark@01")
    assert [(f["node"], f["item"]) for k, f in store.events if k == "node_skipped"] == [("mark", "01")]


# ---- the node step: events, outputs, sessions, failures, the run's end ----


ONE = {"w": {"nodes": {"a": chk(outputs={"x": "text"})}}}


async def test_a_raising_runner_emits_failed_and_reraises(tmp_path):
    def boom(n, c):
        raise RuntimeError("boom")

    c = composer(tmp_path, ONE, {"check": check_that(boom)})
    with pytest.raises(RuntimeError, match="boom"):
        await c.run("w")
    assert [(k, f.get("outcome")) for k, f in c.store.events] == [
        ("node_started", None),
        ("node_finished", "failed"),
    ]


async def test_a_failing_node_cancels_the_ones_running_beside_it(tmp_path):
    cancelled = []

    async def runner(node, ctx):
        if ctx.node == "bad":
            await asyncio.sleep(0)
            raise RuntimeError("boom")
        if ctx.node == "slow":
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(ctx.node)
                raise
        return {k: 1 for k in node.outputs}

    wf = {
        "w": {
            "nodes": {
                "a": chk(outputs={"x": "text"}, to=["slow", "bad"]),
                "slow": chk(outputs={"s": "text"}),
                "bad": chk(outputs={"b": "text"}),
            }
        }
    }
    with pytest.raises(RuntimeError, match="boom"):
        await asyncio.wait_for(composer(tmp_path, wf, {"check": runner}).run("w"), 5)
    assert cancelled == ["slow"]


async def test_a_failed_node_stores_none_of_its_outputs_and_a_stray_result_key_is_not_stored(tmp_path):
    wf = {"w": {"nodes": {"a": chk(outputs={"x": "text"}, on_fail="continue")}}}
    c = composer(tmp_path, wf, {"check": check_that(lambda n, c: {"__fail__": "red", "x": 1})})
    assert await c.run("w") == "a"
    assert not c.store.has("x") and c.store.events[-1][1]["outcome"] == "failed"
    c = composer(tmp_path, ONE, {"check": check_that(lambda n, c: {"x": 1, "y": 2})})
    await c.run("w")
    assert c.store.get("x") == 1 and not c.store.has("y")


async def test_session_outputs_go_to_sessions_and_session_inputs_come_from_them(tmp_path):
    wf = {
        "w": {
            "nodes": {
                "a": chk(outputs={"s": "session"}, to=["b"]),
                "b": chk(inputs=["s"], outputs={"y": "text"}),
            }
        }
    }
    runner = check_that(lambda n, c: {"s": "live"} if c.node == "a" else {"y": c.sessions.get("s")})
    c = composer(tmp_path, wf, {"check": runner})
    await c.run("w")
    assert c.sessions.get("s") == "live" and not c.store.has("s") and c.store.get("y") == "live"


async def test_end_run_from_a_gate_returns_the_gate_and_from_inside_a_bundle_the_bundle(tmp_path):
    async def gate(node, ctx):
        raise EndRun()

    later = check_that(lambda n, c: {"x": 1})
    wf = {
        "w": {
            "nodes": {"g": {"type": "gate", "run": "r", "to": ["later"]}, "later": chk(outputs={"x": "text"})}
        }
    }
    c = composer(tmp_path, wf, {"gate": gate, "check": later})
    assert await c.run("w") == "g" and not c.store.has("x")
    assert [(k, f.get("outcome")) for k, f in c.store.events] == [
        ("node_started", None),
        ("node_finished", "done"),
    ]

    store = MemoryStore({"card": "c", "layers": [{}, {}]})
    wf = {
        "story": {
            "inputs": ["layers"],
            "nodes": {
                "lay": {
                    "type": "bundle",
                    "workflow": "layer",
                    "each": "layers",
                    "inputs": ["layers"],
                    "to": ["later"],
                },
                "later": chk(outputs={"x": "text"}),
            },
        },
        "layer": {"nodes": {"g": {"type": "gate", "run": "r"}}},
    }
    c = composer(tmp_path, wf, {"gate": gate, "check": later}, store=store)
    assert await c.run("story") == "lay" and not store.has("x")


async def test_a_loop_on_a_state_store_clears_and_records_done_without_saving_the_carry(tmp_path):
    from airlie.flow import StateStore
    from airlie.story.state import StoryState
    from tests.unit.test_fork_orchestrator import repo_with_marker_tests
    from tests.unit.test_story_pipeline import text_card

    repo = repo_with_marker_tests(tmp_path)
    st = StoryState.open(tmp_path / "AIR-1", text_card("x"), repo)
    calls = []

    def run(n, c):
        calls.append((c.node, c.feedback, c.store.has("repro")))
        if c.node == "a":
            return {"repro": f"steps {len(calls)}"}
        return {"story": {"n": len(calls)}, "note": "fb"}

    wf = {
        "w": {
            "nodes": {
                "a": chk(outputs={"repro": "text"}, to=["b"]),
                "b": chk(
                    inputs=["repro"],
                    outputs={"story": "json"},
                    to=[{"node": "a", "when": "story.n == 2", "loop": {"max": 3, "carry": "note"}}],
                ),
            }
        }
    }
    c = composer(tmp_path, wf, {"check": check_that(run)}, store=StateStore(st, loose={}))
    await c.run("w")
    assert calls == [("a", None, False), ("b", None, True), ("a", "fb", False), ("b", None, True)]
    saved = json.loads((tmp_path / "AIR-1" / "state.json").read_text())
    assert saved["story"] == {"n": 4} and saved["artifacts"] == {"repro": "steps 3"}
    assert "note" not in saved and "feedback" not in saved and sorted(saved["nodes_done"]) == ["a", "b"]


async def test_the_composer_runs_a_workflow_from_a_given_mapping_not_the_config(tmp_path):
    c = composer(tmp_path, ONE, {"check": check_that(lambda n, c: {"x": 1})})
    assert not c.cfg.workflows
    assert await c.run("w") == "a"
    with pytest.raises(KeyError, match="no workflow named 'nope'"):
        c.spec("nope")


def test_counts_resolve_from_config(tmp_path):
    cfg = cfg_for(tmp_path, extra={"story": {"stage_attempts": 3}})
    assert resolve_count(cfg, "story.stage_attempts") == 3 and resolve_count(cfg, 2) == 2
    with pytest.raises(ConfigError, match="story.nope"):
        resolve_count(cfg, "story.nope")
