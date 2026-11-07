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


@pytest.mark.parametrize(
    ("names", "missing"), [((), ["story", "layer"]), (("story",), ["layer"]), (("layer",), ["story"])]
)
def test_the_composer_on_without_its_workflows_is_a_problem(tmp_path, names, missing):
    from airlie.config import ConfigError, load_config
    from tests.unit.test_config import MINIMAL, write

    workflows = {n: {"start": "a", "nodes": {"a": {"type": "check", "run": "r"}}} for n in names}
    data = {**MINIMAL, "story": {**MINIMAL.get("story", {}), "composer": True}, "workflows": workflows}
    with pytest.raises(ConfigError) as raised:
        load_config(write(tmp_path, data))
    for name in missing:
        assert f"story.composer is on but workflows.{name} is missing" in str(raised.value)
    for name in set(("story", "layer")) - set(missing):
        assert f"workflows.{name} is missing" not in str(raised.value)


def test_the_composer_on_with_both_workflows_loads(tmp_path):
    from airlie.config import load_config
    from tests.unit.test_config import MINIMAL, write

    workflows = {n: {"start": "a", "nodes": {"a": {"type": "check", "run": "r"}}} for n in ("story", "layer")}
    data = {**MINIMAL, "story": {**MINIMAL.get("story", {}), "composer": True}, "workflows": workflows}
    assert load_config(write(tmp_path, data)).story.composer is True


def test_a_parallel_child_may_not_use_loop_branch_next_or_an_on_fail_loop():
    w = wf(
        p=NodeSpec(type="parallel", nodes=["a", "b", "c", "d", "e"]),
        a=NodeSpec(type="check", run="r", loop={"to": "a", "max": 1}),
        b=NodeSpec(type="check", run="r", branch={"x": "a"}, on="k"),
        c=NodeSpec(type="check", run="r", next="a"),
        d=NodeSpec(type="check", run="r", on_fail={"to": "d", "max": 1}),
        e=NodeSpec(type="check", run="r", on_fail="continue"),
    )
    problems = check_workflows({"w": w})
    msg = "a parallel child may not use loop, branch, next or an on_fail loop"
    assert problems == [f"workflow w: node {n}: {msg}" for n in "abcd"]
