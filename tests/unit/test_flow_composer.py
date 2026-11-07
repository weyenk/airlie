import asyncio

import pytest

from airlie.config import ConfigError
from airlie.flow import Composer, EndRun, MemoryStore, MissingInput, Registry, Sessions, resolve_count
from airlie.story.asking import StoryBlocked
from tests.unit.test_studio_orchestrator import cfg_for


def composer(tmp_path, workflows: dict, runners: dict, *, store=None, registry=None):
    cfg = cfg_for(tmp_path, extra={"workflows": workflows})
    return Composer(
        cfg,
        runners=runners,
        registry=registry or Registry(),
        store=store or MemoryStore({"card": "c"}),
        sessions=Sessions(),
    )


def check_that(fn):
    async def run(node, ctx):
        return fn(node, ctx)

    return run


async def test_nodes_run_in_order_and_outputs_land_in_the_store(tmp_path):
    seen = []
    runner = check_that(lambda n, c: (seen.append(c.node), {k: c.node for k in n.outputs})[1])
    wf = {
        "w": {
            "start": "a",
            "loose": ["card"],
            "nodes": {
                "a": {"type": "check", "run": "r", "outputs": {"x": "text"}},
                "b": {"type": "check", "run": "r", "inputs": ["x"], "outputs": {"y": "text"}},
            },
        }
    }
    c = composer(tmp_path, wf, {"check": runner})
    assert await c.run("w") == "b"
    assert seen == ["a", "b"] and c.store.get("y") == "b"


async def test_a_node_whose_outputs_exist_is_skipped(tmp_path):
    store = MemoryStore({"card": "c"})
    store.put("x", "old")
    seen = []
    c = composer(
        tmp_path,
        {"w": {"start": "a", "nodes": {"a": {"type": "check", "run": "r", "outputs": {"x": "text"}}}}},
        {"check": check_that(lambda n, c: (seen.append(1), {"x": "new"})[1])},
        store=store,
    )
    await c.run("w")
    assert seen == [] and store.get("x") == "old"
    assert [k for k, _ in store.events] == ["node_skipped"]


async def test_a_missing_input_refuses_to_run_the_node(tmp_path):
    c = composer(
        tmp_path,
        {
            "w": {
                "start": "a",
                "loose": ["ghost"],
                "nodes": {"a": {"type": "check", "run": "r", "inputs": ["ghost"], "outputs": {"x": "text"}}},
            }
        },
        {"check": check_that(lambda n, c: {"x": 1})},
    )
    with pytest.raises(MissingInput, match="node a: missing input 'ghost'"):
        await c.run("w")


async def test_node_events_bracket_each_run(tmp_path):
    c = composer(
        tmp_path,
        {"w": {"start": "a", "nodes": {"a": {"type": "check", "run": "r", "outputs": {"x": "text"}}}}},
        {"check": check_that(lambda n, c: {"x": 1})},
    )
    await c.run("w")
    kinds = [(k, f["node"], f.get("outcome")) for k, f in c.store.events]
    assert kinds == [("node_started", "a", None), ("node_finished", "a", "done")]


async def test_until_stops_after_the_named_node(tmp_path):
    seen = []
    runner = check_that(lambda n, c: (seen.append(c.node), {k: 1 for k in n.outputs})[1])
    wf = {
        "w": {
            "start": "a",
            "nodes": {
                "a": {"type": "check", "run": "r", "outputs": {"x": "text"}},
                "b": {"type": "check", "run": "r", "outputs": {"y": "text"}},
            },
        }
    }
    assert await composer(tmp_path, wf, {"check": runner}).run("w", until="a") == "a"
    assert seen == ["a"]


async def test_a_when_predicate_skips_the_node(tmp_path):
    reg = Registry()
    reg.add("never", lambda ctx: False)
    c = composer(
        tmp_path,
        {
            "w": {
                "start": "a",
                "nodes": {"a": {"type": "check", "run": "r", "when": "never", "outputs": {"x": "text"}}},
            }
        },
        {"check": check_that(lambda n, c: {"x": 1})},
        registry=reg,
    )
    await c.run("w")
    assert not c.store.has("x")
    assert [k for k, _ in c.store.events] == ["node_skipped"]


async def test_on_fail_block_raises_story_blocked_and_continue_moves_on(tmp_path):
    failing = check_that(lambda n, c: {"__fail__": "no"})
    wf = {"w": {"start": "a", "nodes": {"a": {"type": "check", "run": "r", "outputs": {"x": "text"}}}}}
    with pytest.raises(StoryBlocked, match="no"):
        await composer(tmp_path, wf, {"check": failing}).run("w")
    wf["w"]["nodes"]["a"]["on_fail"] = "continue"
    assert await composer(tmp_path, wf, {"check": failing}).run("w") == "a"


async def test_session_outputs_go_to_sessions_and_session_inputs_come_from_them(tmp_path):
    wf = {
        "w": {
            "start": "a",
            "nodes": {
                "a": {"type": "check", "run": "r", "outputs": {"s": "session"}},
                "b": {"type": "check", "run": "r", "inputs": ["s"], "outputs": {"y": "text"}},
            },
        }
    }
    runner = check_that(lambda n, c: {"s": "live"} if c.node == "a" else {"y": c.sessions.get("s")})
    c = composer(tmp_path, wf, {"check": runner})
    await c.run("w")
    assert c.sessions.get("s") == "live" and not c.store.has("s") and c.store.get("y") == "live"


async def test_an_end_run_stops_the_run_at_that_node(tmp_path):
    def stop(n, c):
        raise EndRun()

    wf = {
        "w": {
            "start": "a",
            "nodes": {
                "a": {"type": "check", "run": "r", "outputs": {"x": "text"}},
                "b": {"type": "check", "run": "r", "outputs": {"y": "text"}},
            },
        }
    }
    assert await composer(tmp_path, wf, {"check": check_that(stop)}).run("w") == "a"


def test_counts_resolve_from_config(tmp_path):
    cfg = cfg_for(tmp_path, extra={"story": {"stage_attempts": 3}})
    assert resolve_count(cfg, "story.stage_attempts") == 3 and resolve_count(cfg, 2) == 2
    with pytest.raises(ConfigError, match="story.nope"):
        resolve_count(cfg, "story.nope")


ONE = {"w": {"start": "a", "nodes": {"a": {"type": "check", "run": "r", "outputs": {"x": "text"}}}}}


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


async def test_a_missing_input_emits_no_node_events(tmp_path):
    wf = {
        "w": {
            "start": "a",
            "nodes": {"a": {"type": "check", "run": "r", "inputs": ["x"], "outputs": {"y": "text"}}},
        }
    }
    wf["w"]["loose"] = ["x"]
    c = composer(tmp_path, wf, {"check": check_that(lambda n, c: {"y": 1})})
    with pytest.raises(MissingInput):
        await c.run("w")
    assert c.store.events == []


async def test_an_end_run_finishes_the_node_as_done(tmp_path):
    def stop(n, c):
        raise EndRun()

    c = composer(tmp_path, ONE, {"check": check_that(stop)})
    assert await c.run("w") == "a"
    assert [(k, f.get("outcome")) for k, f in c.store.events] == [
        ("node_started", None),
        ("node_finished", "done"),
    ]


async def test_a_failed_node_stores_none_of_its_outputs(tmp_path):
    wf = {
        "w": {
            "start": "a",
            "nodes": {"a": {"type": "check", "run": "r", "outputs": {"x": "text"}, "on_fail": "continue"}},
        }
    }
    c = composer(tmp_path, wf, {"check": check_that(lambda n, c: {"__fail__": "red", "x": 1})})
    assert await c.run("w") == "a"
    assert not c.store.has("x")
    assert c.store.events[-1][1]["outcome"] == "failed"


async def test_parallel_nodes_both_run_then_the_next_in_order(tmp_path):
    seen = []
    runner = check_that(lambda n, c: (seen.append(c.node), {k: 1 for k in n.outputs})[1])
    wf = {
        "w": {
            "start": "both",
            "nodes": {
                "both": {"type": "parallel", "nodes": ["s", "r"]},
                "s": {"type": "check", "run": "r", "outputs": {"survey": "text"}},
                "r": {"type": "check", "run": "r", "outputs": {"story": "json"}},
                "after": {
                    "type": "check",
                    "run": "r",
                    "inputs": ["survey", "story"],
                    "outputs": {"z": "text"},
                },
            },
        }
    }
    assert await composer(tmp_path, wf, {"check": runner}).run("w") == "after"
    assert set(seen[:2]) == {"s", "r"} and seen[2] == "after"


async def test_a_failing_parallel_branch_cancels_the_others(tmp_path):
    cancelled = []

    async def runner(node, ctx):
        if ctx.node == "bad":
            raise RuntimeError("boom")
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(ctx.node)
            raise
        return {}

    wf = {
        "w": {
            "start": "both",
            "nodes": {
                "both": {"type": "parallel", "nodes": ["bad", "slow"]},
                "bad": {"type": "check", "run": "r", "outputs": {"a": "text"}},
                "slow": {"type": "check", "run": "r", "outputs": {"b": "text"}},
            },
        }
    }
    with pytest.raises(RuntimeError, match="boom"):
        await composer(tmp_path, wf, {"check": runner}).run("w")
    assert cancelled == ["slow"]


async def test_a_branch_picks_the_next_node_by_an_output(tmp_path):
    seen = []
    runner = check_that(
        lambda n, c: (seen.append(c.node), {"kind": "pinned"} if c.node == "base" else {"x": 1})[1]
    )
    wf = {
        "w": {
            "start": "base",
            "nodes": {
                "base": {
                    "type": "check",
                    "run": "r",
                    "outputs": {"kind": "text"},
                    "on": "kind",
                    "branch": {"pinned": "p", "*": "t"},
                },
                "t": {"type": "check", "run": "r", "outputs": {"x": "text"}},
                "p": {"type": "check", "run": "r", "outputs": {"x": "text"}},
            },
        }
    }
    await composer(tmp_path, wf, {"check": runner}).run("w")
    assert seen == ["base", "p"]


async def test_a_branch_reads_the_stored_output_of_a_skipped_node(tmp_path):
    store = MemoryStore({"card": "c"})
    store.put("kind", "other")
    seen = []
    runner = check_that(lambda n, c: (seen.append(c.node), {"x": 1})[1])
    wf = {
        "w": {
            "start": "base",
            "nodes": {
                "base": {
                    "type": "check",
                    "run": "r",
                    "outputs": {"kind": "text"},
                    "on": "kind",
                    "branch": {"pinned": "p", "*": "t"},
                },
                "p": {"type": "check", "run": "r", "outputs": {"x": "text"}},
                "t": {"type": "check", "run": "r", "outputs": {"y": "text"}},
            },
        }
    }
    await composer(tmp_path, wf, {"check": runner}, store=store).run("w")
    assert seen == ["t"]


async def test_a_loop_carries_feedback_and_stops_at_its_bound(tmp_path):
    calls = []
    runner = check_that(
        lambda n, c: (calls.append(c.feedback), {"__fail__": "red", "tail": f"tail{len(calls)}"})[1]
    )
    wf = {
        "w": {
            "start": "write",
            "nodes": {
                "write": {
                    "type": "check",
                    "run": "r",
                    "outputs": {"tail": "text"},
                    "loop": {"to": "write", "max": 2, "carry": "tail"},
                    "on_fail": "continue",
                }
            },
        }
    }
    await composer(tmp_path, wf, {"check": runner}).run("w")
    assert calls == [None, "tail1", "tail2"]


async def test_a_loop_bound_from_config_and_block_when_spent(tmp_path):
    runner = check_that(lambda n, c: {"__fail__": "red"})
    wf = {
        "w": {
            "start": "a",
            "nodes": {
                "a": {
                    "type": "check",
                    "run": "r",
                    "outputs": {"x": "text"},
                    "loop": {"to": "a", "max": "story.layer_attempts"},
                }
            },
        }
    }
    cfg = cfg_for(tmp_path, extra={"workflows": wf, "story": {"layer_attempts": 1}})
    c = Composer(
        cfg,
        runners={"check": runner},
        registry=Registry(),
        store=MemoryStore({"card": "c"}),
        sessions=Sessions(),
    )
    with pytest.raises(StoryBlocked):
        await c.run("w")


async def test_a_loop_when_predicate_re_runs_a_node_that_did_not_fail(tmp_path):
    calls = []
    registry = Registry()
    registry.add("again", lambda ctx, result: result["n"] < 2)
    runner = check_that(lambda n, c: (calls.append(c.feedback), {"n": len(calls), "note": "fb"})[1])
    wf = {
        "w": {
            "start": "a",
            "nodes": {
                "a": {
                    "type": "check",
                    "run": "r",
                    "outputs": {"n": "json"},
                    "loop": {"to": "a", "max": 5, "when": "again", "carry": "note"},
                }
            },
        }
    }
    c = composer(tmp_path, wf, {"check": runner}, registry=registry)
    await c.run("w")
    assert calls == [None, "fb"] and c.store.get("n") == 2


async def test_a_loop_to_an_earlier_succeeded_node_runs_it_again_and_overwrites(tmp_path):
    calls = []
    registry = Registry()
    registry.add("redo", lambda ctx, result: len(calls) < 3)
    runner = check_that(lambda n, c: (calls.append(c.node), {k: len(calls) for k in n.outputs})[1])
    wf = {
        "w": {
            "start": "a",
            "nodes": {
                "a": {"type": "check", "run": "r", "outputs": {"x": "json"}},
                "b": {
                    "type": "check",
                    "run": "r",
                    "outputs": {"y": "json"},
                    "loop": {"to": "a", "max": 1, "when": "redo"},
                },
            },
        }
    }
    c = composer(tmp_path, wf, {"check": runner}, registry=registry)
    await c.run("w")
    assert calls == ["a", "b", "a"] and c.store.get("x") == 3


async def test_until_on_a_failed_loop_node_still_blocks(tmp_path):
    runner = check_that(lambda n, c: {"__fail__": "red"})
    wf = {
        "w": {
            "start": "a",
            "nodes": {
                "a": {"type": "check", "run": "r", "outputs": {"x": "text"}, "loop": {"to": "a", "max": 1}}
            },
        }
    }
    with pytest.raises(StoryBlocked):
        await composer(tmp_path, wf, {"check": runner}).run("w", until="a")


async def test_parallel_children_run_once_and_are_not_walked_again(tmp_path):
    seen = []
    runner = check_that(lambda n, c: (seen.append(c.node), {k: 1 for k in n.outputs})[1])
    wf = {
        "w": {
            "start": "both",
            "nodes": {
                "both": {"type": "parallel", "nodes": ["s", "quiet"]},
                "s": {"type": "check", "run": "r", "outputs": {"survey": "text"}},
                "quiet": {"type": "check", "run": "r"},
                "after": {"type": "check", "run": "r", "inputs": ["survey"], "outputs": {"z": "text"}},
            },
        }
    }
    c = composer(tmp_path, wf, {"check": runner})
    assert await c.run("w") == "after"
    assert sorted(seen) == ["after", "quiet", "s"]
    assert [k for k, _ in c.store.events if k == "node_skipped"] == []


async def test_an_on_fail_loop_re_runs_then_blocks(tmp_path):
    calls = []
    runner = check_that(lambda n, c: (calls.append(c.feedback), {"__fail__": "red", "t": "x"})[1])
    wf = {
        "w": {
            "start": "a",
            "nodes": {
                "a": {
                    "type": "check",
                    "run": "r",
                    "outputs": {"y": "text"},
                    "on_fail": {"to": "a", "max": 1, "carry": "t"},
                }
            },
        }
    }
    with pytest.raises(StoryBlocked):
        await composer(tmp_path, wf, {"check": runner}).run("w")
    assert calls == [None, "x"]


async def test_each_runs_the_bundle_per_item_and_skips_built_items(tmp_path):
    store = MemoryStore({"card": "c"})
    store.put("layers", [{}, {}, {}])
    store.put("built.01", {"sha": "a"})
    seen = []
    runner = check_that(lambda n, c: (seen.append((c.node, c.item)), {"built.{item}": {"sha": c.item}})[1])
    wf = {
        "story": {"start": "lay", "nodes": {"lay": {"type": "each", "over": "layers", "workflow": "layer"}}},
        "layer": {
            "start": "b",
            "nodes": {"b": {"type": "check", "run": "r", "outputs": {"built.{item}": "json"}}},
        },
    }
    await composer(tmp_path, wf, {"check": runner}, store=store).run("story")
    assert seen == [("b", "02"), ("b", "03")] and store.get("built.03") == {"sha": "03"}


async def test_end_run_from_a_gate_returns_the_gate_node(tmp_path):
    async def gate(node, ctx):
        raise EndRun()

    wf = {
        "w": {
            "start": "g",
            "nodes": {
                "g": {"type": "gate", "run": "r"},
                "later": {"type": "check", "run": "r", "outputs": {"x": "text"}},
            },
        }
    }
    c = composer(tmp_path, wf, {"gate": gate, "check": check_that(lambda n, c: {"x": 1})})
    assert await c.run("w") == "g" and not c.store.has("x")


async def test_end_run_inside_a_bundle_ends_the_whole_run(tmp_path):
    async def gate(node, ctx):
        raise EndRun()

    store = MemoryStore({"card": "c"})
    store.put("layers", [{}, {}])
    wf = {
        "story": {
            "start": "lay",
            "nodes": {
                "lay": {"type": "each", "over": "layers", "workflow": "layer"},
                "later": {"type": "check", "run": "r", "outputs": {"x": "text"}},
            },
        },
        "layer": {"start": "g", "nodes": {"g": {"type": "gate", "run": "r"}}},
    }
    c = composer(tmp_path, wf, {"gate": gate, "check": check_that(lambda n, c: {"x": 1})}, store=store)
    assert await c.run("story") == "lay" and not store.has("x")


async def test_feedback_carried_in_one_item_is_not_read_by_the_next(tmp_path):
    store = MemoryStore({"card": "c"})
    store.put("layers", [{}, {}])
    calls = []

    def build(n, c):
        calls.append((c.item, c.feedback))
        first = sum(1 for item, _ in calls if item == c.item) == 1
        return {"__fail__": "red", "tail": f"red {c.item}"} if first else {"built.{item}": c.item}

    wf = {
        "story": {"start": "lay", "nodes": {"lay": {"type": "each", "over": "layers", "workflow": "layer"}}},
        "layer": {
            "start": "b",
            "nodes": {
                "b": {
                    "type": "check",
                    "run": "r",
                    "outputs": {"built.{item}": "json"},
                    "loop": {"to": "b", "max": 1, "carry": "tail"},
                }
            },
        },
    }
    await composer(tmp_path, wf, {"check": check_that(build)}, store=store).run("story")
    assert calls == [("01", None), ("01", "red 01"), ("02", None), ("02", "red 02")]
