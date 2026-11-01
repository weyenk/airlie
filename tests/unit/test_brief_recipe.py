"""A brief from a recipe: its entries checked, its parts joined, their sizes told."""

import re
from pathlib import Path

import pytest

from airlie.story.brief_recipe import (
    BriefInputs,
    RecipeEntry,
    RecipeError,
    assemble,
    load_recipe,
    parse_recipe,
)
from tests.unit.test_story_briefs import LAYERS, PACKAGE, STORY


def inputs(tmp: Path = Path(".")) -> BriefInputs:
    return BriefInputs(STORY, LAYERS, 1, PACKAGE, tmp, "HEAD", 196_608)


def test_a_recipe_is_its_sections_and_rules_in_order() -> None:
    r = parse_recipe("- section: layer_context\n- rule: brief-layer-plan-agents-md\n", "t")
    assert r == (RecipeEntry(section="layer_context"), RecipeEntry(rule="brief-layer-plan-agents-md"))


@pytest.mark.parametrize(
    "text, phrase",
    [
        ("- section: nope\n", "unknown section 'nope'"),
        ("- rule: no-such-rule\n", "unknown rule 'no-such-rule'"),
        ("- section: package\n  shares: 0.2\n", "unknown key 'shares'"),
        ("- section: package\n  rule: package-contract\n", "both section and rule"),
        ("- section: package\n  over: outline\n", "package does not take over"),
        ("- section: package\n  share: 1.5\n", "share must be between 0 and 1"),
    ],
)
def test_a_wrong_recipe_is_refused_naming_the_entry(text: str, phrase: str) -> None:
    with pytest.raises(RecipeError, match=re.escape(phrase)) as e:
        parse_recipe(text, "t")
    assert "recipe t, entry 0" in str(e.value)


def test_a_missing_recipe_is_refused_by_name() -> None:
    with pytest.raises(RecipeError, match="no-such-recipe"):
        load_recipe("no-such-recipe")


def test_the_assembled_brief_is_its_parts_and_their_sizes_add_up() -> None:
    recipe = parse_recipe("- section: layer_context\n- section: package\n- rule: package-contract\n", "t")
    brief = assemble("t", recipe, inputs())
    assert brief.text == "\n\n".join(p.text for p in brief.parts if p.text)
    joins = 2 * (len([p for p in brief.parts if p.text]) - 1)
    assert sum(s["chars"] for s in brief.sizes()) + joins == len(brief.text)
    assert brief.text.startswith(f"Story: {STORY.title}.")
    assert "## The tests you make pass (in the worktree, locked)" in brief.text
