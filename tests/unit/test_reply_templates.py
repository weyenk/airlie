"""Reply templates (2026-11-02): each node's answer shape in airlie/prompts/replies/, the contract and the
structural check made from the one file."""

from importlib.resources import files

import pytest

from airlie.prompts.reply import PART, WHOLE, ReplyError, reply
from airlie.story.briefs import LAYERS_CONTRACT
from airlie.story.model import Criterion, Story, parse_layers

NAMES = sorted(p.name.removesuffix(".yaml") for p in (files("airlie.prompts") / "replies").iterdir())


def test_there_are_templates_and_each_loads_with_a_valid_schema() -> None:
    assert {"layers", "review"} <= set(NAMES)
    for name in NAMES:
        r = reply(name)
        assert r.contract.startswith(WHOLE + "\n") and r.part.startswith(PART + "\n")
        assert r.shape.strip() in r.contract and r.rules.strip() in r.contract


def test_a_missing_template_is_named() -> None:
    with pytest.raises(ReplyError, match="reply nope: no such template"):
        reply("nope")


def test_the_layers_contract_asks_for_the_object_and_nothing_else() -> None:
    assert reply("layers").contract == LAYERS_CONTRACT
    assert "no prose before or after it" in LAYERS_CONTRACT and "End your reply" not in LAYERS_CONTRACT


def test_the_layers_schema_accepts_what_the_parser_reads_and_names_a_wrong_shape() -> None:
    story = Story("T", "s", (Criterion("AC1", "g", "w", "t"),))
    plan = {
        "layers": [
            {"title": "One", "scope": "src/a.py", "brief": "b", "acceptance": "AC1", "files": ["src/a.py"]},
            {
                "slug": "two",
                "title": "Two",
                "scope": "s",
                "brief": "b",
                "acceptance": [],
                "files": [{"path": "src/b.py", "new": True}],
                "interfaces": "def f() -> None",
            },
        ],
        "refine": {"worth_another_round": False, "reason": "r", "check": ""},
    }
    assert len(parse_layers(plan, story, 4)) == 2
    layers = reply("layers")
    assert layers.problem(plan) is None
    assert layers.problem({"layers": []}) == "layers: [] should be non-empty"
    assert (
        layers.problem({"layers": [{"title": "x", "scope": "s"}]})
        == "layers[0]: 'brief' is a required property"
    )
