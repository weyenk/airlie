"""The recipe example in docs/integrations parses with the real loader."""

from pathlib import Path

from airlie.boxes.recipe import load_recipe
from airlie.config.schema import BoxesSpec

EXAMPLE = Path(__file__).resolve().parents[2] / "docs" / "integrations" / "environment.example.json"


def test_the_documented_recipe_example_loads() -> None:
    recipe = load_recipe(EXAMPLE.read_text(), BoxesSpec())
    assert recipe.setup
    assert recipe.test
    assert recipe.base == BoxesSpec().image
