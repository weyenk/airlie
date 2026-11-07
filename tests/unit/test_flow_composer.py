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
