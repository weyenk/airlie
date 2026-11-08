import pytest
from pydantic import ValidationError

from airlie.flow.outputs import OutputSpec, parse_kind
from airlie.flow.spec import Layout, Line, NodeSpec, TeamRole, WorkflowSpec, check_workflows


def wf(**nodes):
    return WorkflowSpec(inputs=["card"], nodes=nodes)


def ask(role="product", **kw):
    return NodeSpec(type="ask", role=role, prompt="p", **kw)


def check(**kw):
    return NodeSpec(type="check", run="r", **kw)


def test_each_type_requires_its_fields():
    w = wf(
        a=NodeSpec(type="ask"),
        c=NodeSpec(type="conversation", role="architect"),
        wr=NodeSpec(type="write", prompt="p"),
        m=NodeSpec(type="meeting", role="product", prompt="p"),
        t=NodeSpec(type="team", role=TeamRole(supervisor="architect", worker="coder"), prompt="p"),
        k=NodeSpec(type="check"),
        g=NodeSpec(type="gate"),
        b=NodeSpec(type="bundle"),
    )
    problems = check_workflows({"w": w})
    assert "workflow w: node a: ask needs role and prompt" in problems
    assert "workflow w: node c: conversation needs role and prompt" in problems
    assert "workflow w: node wr: write needs role and prompt" in problems
    assert "workflow w: node m: meeting needs role (a list) and prompt" in problems
    assert "workflow w: node t: team needs role (supervisor and worker), prompt and worker" in problems
    assert "workflow w: node k: check needs run" in problems
    assert "workflow w: node g: gate needs run" in problems
    assert "workflow w: node b: bundle needs workflow" in problems
    assert len(problems) == 8


def test_a_model_node_may_name_no_code():
    w = wf(
        a=ask(),
        b=NodeSpec(type="ask", role="product", prompt="p", run="story.survey", args={"x": 1}),
        c=NodeSpec(type="check", run="r", prompt="p", turn="t", session="s", workers=2),
    )
    problems = check_workflows({"w": w})
    assert problems == [
        "workflow w: node b: ask names code (run, args); a model node is configuration only",
        "workflow w: node c: check takes no model fields (prompt, turn, session, workers)",
    ]


def test_lines_name_known_nodes_and_conditions_name_known_artifacts():
    w = wf(
        a=check(outputs={"x": "json"}, to=[Line(node="zz"), Line(node="b", when="x.kind == y")]),
        b=check(inputs=["x"]),
    )
    assert check_workflows({"w": w}) == ["workflow w: node a: to names unknown node 'zz'"]
    assert check_workflows({"w": wf(a=check(to=["a2"]), a2=check())}) == []  # a bare name is a line


def test_an_input_nothing_produces_is_refused_unless_the_workflow_declares_it():
    w = wf(refine=ask(inputs=["survey"], outputs={"story": "json"}))
    assert check_workflows({"story": w}) == [
        "workflow story: node refine: input 'survey' is not produced by any node and not a workflow input"
    ]
    ok = wf(refine=ask(inputs=["card"], outputs={"story": "json"}))
    assert check_workflows({"story": ok}) == []
    fed = WorkflowSpec(inputs=["card", "survey"], nodes={"refine": ask(inputs=["survey"])})
    assert check_workflows({"story": fed}) == []


def test_feedback_is_readable_where_a_loop_line_carries_it():
    w = wf(
        build=ask("coder", inputs=["feedback"], outputs={"built": "json"}),
        gate=check(
            inputs=["built"],
            outputs={"result": "json"},
            to=[Line(node="build", loop={"max": 2, "carry": "reason"})],
        ),
    )
    assert check_workflows({"w": w}) == []


def test_a_bundle_box_needs_its_lines_and_the_bundles_inputs_must_be_its_inputs():
    outer = wf(
        base=check(outputs={"base": "ref:branch", "layers": "list"}),
        each=NodeSpec(type="bundle", workflow="layer", each="layers", inputs=["layers", "base", "card"]),
        lone=NodeSpec(type="bundle", workflow="layer", each="nope", inputs=["layers"]),
        short=NodeSpec(type="bundle", workflow="layer", each="layers", inputs=["layers"]),
    )
    inner = WorkflowSpec(inputs=["layers", "base"], nodes={"pkg": ask("architect", inputs=["base"])})
    problems = check_workflows({"story": outer, "layer": inner})
    assert "workflow story: node each: input 'layers' is not carried by a line into the node" in problems
    assert "workflow story: node each: input 'base' is not carried by a line into the node" in problems
    assert "workflow story: node lone: each 'nope' is not among the node's inputs" in problems
    assert (
        "workflow story: node short: workflow 'layer' reads input 'base', which is not an input of the node"
        in problems
    )
    assert not [p for p in problems if "node each: " in p and "card" in p]  # a workflow input needs no line
    wired = wf(
        base=check(outputs={"base": "ref:branch", "layers": "list"}, to=["each"]),
        each=NodeSpec(type="bundle", workflow="layer", each="layers", inputs=["layers", "base"]),
    )
    assert check_workflows({"story": wired, "layer": inner}) == []
    missing = wf(each=NodeSpec(type="bundle", workflow="nope"))
    assert check_workflows({"story": missing}) == [
        "workflow story: node each: workflow names unknown workflow 'nope'"
    ]


def test_a_session_line_is_only_between_two_nodes_of_the_same_role():
    def build(second_role):
        return wf(
            drafts=NodeSpec(
                type="conversation",
                role="architect",
                prompt="p",
                outputs={"drafts_session": "session"},
                to=["pkg"],
            ),
            pkg=ask(second_role, inputs=["drafts_session"]),
        )

    assert check_workflows({"w": build("architect")}) == []
    assert check_workflows({"w": build("coder")}) == [
        "workflow w: node pkg: input 'drafts_session' is a session of node 'drafts' (role architect); "
        "a session continues only between nodes of the same role"
    ]


def test_a_bare_format_is_an_output_spec_without_fields():
    n = ask(outputs={"story": "json", "notes": {"format": "markdown"}})
    assert n.outputs["story"] == OutputSpec(format="json") and n.outputs["story"].fields is None
    assert n.outputs["notes"].format == "markdown"
    full = ask(
        outputs={"verdict": {"format": "json", "fields": {"decision": "agree | object", "why": "text?"}}}
    )
    assert full.outputs["verdict"].fields == {"decision": "agree | object", "why": "text?"}


def test_a_cycle_without_a_loop_bound_is_refused():
    w = wf(a=check(to=["b"]), b=check(to=["a"]), c=check(to=["c"]))
    assert check_workflows({"w": w}) == [
        "workflow w: node a: the line to 'b' closes a cycle and has no loop bound",
        "workflow w: node b: the line to 'a' closes a cycle and has no loop bound",
        "workflow w: node c: the line to 'c' closes a cycle and has no loop bound",
    ]
    bounded = wf(a=check(to=["b"]), b=check(to=[Line(node="a", loop={"max": "review.rounds"})]))
    assert check_workflows({"w": bounded}) == []


@pytest.mark.parametrize(
    ("kind", "name", "optional"),
    [
        ("text", "text", False),
        ("number?", "number", True),
        ("bool", "bool", False),
        ("list of text", "list", False),
        ("list of list of number?", "list", True),
        ("agree | object", "one_of", False),
        ("one of a | b | c?", "one_of", True),
    ],
)
def test_field_kinds_parse(kind, name, optional):
    parsed = parse_kind(kind)
    assert (parsed.name, parsed.optional) == (name, optional)


def test_the_field_kind_grammar_refuses_anything_else():
    assert parse_kind("list of one of a | b").item.options == ("a", "b")
    for bad in ("string", "list of", "list of wat", "a |", "", "one of"):
        with pytest.raises(ValueError, match="invalid field kind"):
            parse_kind(bad)
    with pytest.raises(ValidationError, match="field 'why': invalid field kind 'string'"):
        OutputSpec(format="json", fields={"why": "string"})


@pytest.mark.parametrize(
    ("node", "old"),
    [
        ({"type": "check", "run": "r", "next": "b"}, "next"),
        ({"type": "ask", "role": "x", "prompt": "p", "parse": "story.story"}, "parse"),
        ({"type": "ask", "role": "x", "prompt": "p", "when": "story.amigos_on"}, "when"),
        ({"type": "check", "run": "r", "loop": {"to": "a", "max": 1}}, "loop"),
        ({"type": "check", "run": "r", "on_fail": {"to": "a", "max": 1}}, "on_fail"),
    ],
)
def test_an_old_schema_node_is_refused_naming_the_field_and_the_upgrade(node, old):
    with pytest.raises(ValidationError, match=f"field '{old}' is the old schema; run the workflow upgrade"):
        NodeSpec.model_validate(node)


def test_an_old_schema_workflow_is_refused_naming_the_field():
    with pytest.raises(ValidationError, match="field 'start' is the old schema"):
        WorkflowSpec.model_validate({"start": "a", "nodes": {}})
    with pytest.raises(ValidationError, match="field 'loose' is the old schema"):
        WorkflowSpec.model_validate({"loose": ["card"], "nodes": {}})
    with pytest.raises(ValidationError, match="type 'each' is the old schema"):
        NodeSpec.model_validate({"type": "each"})


def test_the_config_rejects_a_bad_workflow(tmp_path):
    from airlie.config import ConfigError, load_config
    from tests.unit.test_config import MINIMAL, write

    data = {**MINIMAL, "workflows": {"w": {"nodes": {"a": {"type": "ask", "inputs": ["ghost"]}}}}}
    with pytest.raises(ConfigError, match="node a"):
        load_config(write(tmp_path, data))


def test_a_workflow_in_the_old_schema_is_set_aside_with_its_problems(tmp_path):
    from airlie.config import load_config
    from tests.unit.test_config import MINIMAL, write

    old = {"start": "a", "loose": ["card"], "nodes": {"a": {"type": "check", "run": "r", "next": "b"}}}
    new = {"nodes": {"a": {"type": "check", "run": "r"}}}
    cfg = load_config(write(tmp_path, {**MINIMAL, "workflows": {"old": old, "new": new}}))
    assert list(cfg.workflows) == ["new"]
    assert cfg.workflow_problems == [
        "workflow old: field 'start' is the old schema; run the workflow upgrade or write a `to` line",
        "workflow old: field 'loose' is the old schema; run the workflow upgrade or write a `to` line",
        "workflow old: node a: field 'next' is the old schema; run the workflow upgrade or write a `to` line",
    ]


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

    workflows = {n: {"nodes": {"a": {"type": "check", "run": "r"}}} for n in names}
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

    workflows = {n: {"nodes": {"a": {"type": "check", "run": "r"}}} for n in ("story", "layer")}
    data = {**MINIMAL, "story": {**MINIMAL.get("story", {}), "composer": True}, "workflows": workflows}
    assert load_config(write(tmp_path, data)).story.composer is True


def test_type_and_layout_are_optional_and_layout_must_name_nodes():
    w = WorkflowSpec(type="bug", layout={"a": Layout(x=10, y=20)}, nodes={"a": check()})
    assert check_workflows({"w": w}) == []
    w2 = WorkflowSpec(layout={"zz": Layout(x=0, y=0)}, nodes={"a": check()})
    assert check_workflows({"w": w2}) == ["workflow w: layout names unknown node 'zz'"]


@pytest.mark.parametrize("name", ["path", "artifacts", "workflow", "workflow.name", "path.x"])
def test_an_output_named_as_the_states_own_bookkeeping_is_refused(name):
    """`artifacts` would overwrite the artifact store, `workflow` the card's snapshot, `path` its place."""
    w = wf(a=check(outputs={name: "json"}))
    assert check_workflows({"w": w}) == [f"workflow w: node a: output '{name}' is reserved"]


def test_an_output_inside_the_artifact_store_is_fine():
    w = wf(a=check(outputs={"artifacts.note": "text", "repro": "text"}))
    assert check_workflows({"w": w}) == []
