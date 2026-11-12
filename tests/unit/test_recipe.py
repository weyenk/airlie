import json

import pytest

from airlie.boxes.recipe import Recipe, RecipeError, load_recipe
from airlie.config.schema import BoxesSpec

SPEC = BoxesSpec()


def test_no_recipe_is_the_default_image() -> None:
    recipe = load_recipe(None, SPEC)
    assert recipe == Recipe(base=SPEC.image)
    assert (recipe.setup, recipe.env, recipe.test) == ((), {}, ())
    assert recipe.workdir == "/home/admin/work"


def test_a_recipe_names_base_setup_env_and_tests() -> None:
    text = json.dumps(
        {
            "base": "ghcr.io/cirruslabs/debian:latest",
            "setup": ["apt-get update", "uv sync"],
            "env": {"CI": "1"},
            "test": ["uv run pytest -q"],
            "workdir": "/srv/app",
        }
    )
    recipe = load_recipe(text, SPEC)
    assert recipe.base == "ghcr.io/cirruslabs/debian:latest"
    assert recipe.setup == ("apt-get update", "uv sync")
    assert recipe.env == {"CI": "1"}
    assert recipe.test == ("uv run pytest -q",)
    assert recipe.workdir == "/srv/app"


def test_a_recipe_without_a_base_takes_the_default() -> None:
    assert load_recipe('{"setup": ["true"]}', SPEC).base == SPEC.image


def test_an_unknown_key_is_refused_naming_it() -> None:
    with pytest.raises(RecipeError, match=r"environment\.json: secrets: ") as err:
        load_recipe('{"secrets": {"A": "b"}}', SPEC)
    assert "secrets" in str(err.value)


def test_a_wrongly_typed_key_is_refused_naming_it() -> None:
    with pytest.raises(RecipeError, match=r"setup\.0: "):
        load_recipe('{"setup": [1]}', SPEC)


def test_malformed_json_is_refused() -> None:
    with pytest.raises(RecipeError, match="not valid JSON"):
        load_recipe("{", SPEC)


def test_a_recipe_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(RecipeError, match="must be a JSON object"):
        load_recipe("[]", SPEC)


def test_the_key_is_twelve_hex_characters() -> None:
    key = Recipe(setup=("a",)).key("img")
    assert len(key) == 12
    int(key, 16)


def test_the_key_changes_with_setup_and_not_with_env_values() -> None:
    base = Recipe(setup=("uv sync",), env={"A": "1"})
    assert base.key("img") == Recipe(setup=("uv sync",), env={"A": "2"}).key("img")
    assert base.key("img") != Recipe(setup=("uv sync", "x"), env={"A": "1"}).key("img")
    assert base.key("img") != base.key("other")
    assert base.key("img") != Recipe(setup=("uv sync",), env={"B": "1"}).key("img")


def test_the_key_ignores_tests_and_workdir() -> None:
    base = Recipe(setup=("uv sync",))
    assert base.key("img") == Recipe(setup=("uv sync",), test=("pytest",), workdir="/x").key("img")


def test_the_key_is_stable() -> None:
    assert Recipe(setup=("a", "b"), env={"Z": "", "A": ""}).key("img") == "3747b48be710"
