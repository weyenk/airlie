import pytest

from airlie.flow.spec import NodeSpec, WorkflowSpec, check_workflows


def wf(**nodes):
    return WorkflowSpec(start=next(iter(nodes)), loose=["card"], nodes=nodes)


def test_an_input_nothing_produces_is_a_problem_naming_the_node():
    w = wf(
        refine=NodeSpec(type="ask", role="product", prompt="p", inputs=["survey"], outputs={"story": "json"})
    )
    assert check_workflows({"story": w}) == [
        "workflow story: node refine: input 'survey' is not produced by any node and not loose"
    ]


def test_a_loose_input_is_fine():
    w = wf(
        refine=NodeSpec(type="ask", role="product", prompt="p", inputs=["card"], outputs={"story": "json"})
    )
    assert check_workflows({"story": w}) == []


def test_each_type_requires_its_fields():
    w = wf(
        a=NodeSpec(type="ask"), c=NodeSpec(type="check"), e=NodeSpec(type="each"), p=NodeSpec(type="parallel")
    )
    problems = check_workflows({"w": w})
    assert "workflow w: node a: ask needs role and prompt" in problems
    assert "workflow w: node c: check needs run" in problems
    assert "workflow w: node e: each needs over and workflow" in problems
    assert "workflow w: node p: parallel needs nodes" in problems


def test_wires_must_name_known_nodes_and_bundles():
    w = wf(
        a=NodeSpec(type="check", run="r", next="zz", branch={"x": "yy"}, on="kind"),
        e=NodeSpec(type="each", over="layers", workflow="nope"),
    )
    problems = check_workflows({"w": w})
    assert "workflow w: node a: next names unknown node 'zz'" in problems
    assert "workflow w: node a: branch names unknown node 'yy'" in problems
    assert "workflow w: node e: workflow names unknown workflow 'nope'" in problems


def test_a_bundle_may_read_what_its_caller_produces():
    outer = wf(
        base=NodeSpec(type="check", run="r", outputs={"base": "json", "layers": "list"}),
        each=NodeSpec(type="each", over="layers", workflow="layer"),
    )
    inner = WorkflowSpec(
        start="pkg",
        nodes={"pkg": NodeSpec(type="check", run="r", inputs=["base"], outputs={"built.{item}": "json"})},
    )
    assert check_workflows({"story": outer, "layer": inner}) == []


def test_the_config_rejects_a_bad_workflow(tmp_path):
    from airlie.config import ConfigError, load_config
    from tests.unit.test_config import MINIMAL, write

    data = {
        **MINIMAL,
        "workflows": {
            "w": {"start": "a", "nodes": {"a": {"type": "ask", "inputs": ["ghost"], "outputs": {}}}}
        },
    }
    with pytest.raises(ConfigError, match="node a"):
        load_config(write(tmp_path, data))


def test_story_composer_defaults_off(tmp_path):
    from airlie.config import load_config
    from tests.unit.test_config import MINIMAL, write

    assert load_config(write(tmp_path, MINIMAL)).story.composer is False
