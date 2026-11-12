"""The repository recipe: `.airlie/environment.json`, which travels with the checkout and names the base
image, a setup script, the environment variables and the test commands of a repository's VM."""

from __future__ import annotations

import hashlib
import json

from pydantic import ConfigDict, ValidationError

from airlie.config.schema import BoxesSpec, Strict
from airlie.engine.types import EngineError

RECIPE_PATH = ".airlie/environment.json"


class RecipeError(EngineError):
    """The recipe is malformed, or its setup failed; the message names the key or the failing line."""


class Recipe(Strict):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base: str | None = None
    """The image to start from (default: boxes.image)."""
    setup: tuple[str, ...] = ()
    """Shell lines run once, with network open, when the image is built."""
    env: dict[str, str] = {}
    """Variables of every command in the VM. Their values are in git: no secrets."""
    test: tuple[str, ...] = ()
    """The commands `run_tests` runs in the VM."""
    workdir: str = "/home/admin/work"

    def key(self, image: str) -> str:
        """Names the cached image: the base, the setup and the variable names (not their values: a value
        changes nothing that was built, and a secret is never part of it)."""
        canonical = json.dumps([image, list(self.setup), sorted(self.env)], separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def load_recipe(text: str | None, defaults: BoxesSpec) -> Recipe:
    """The recipe in `text` (the file's content; None when the repository has none). Malformed JSON or an
    unknown key raises RecipeError naming it."""
    if text is None:
        return Recipe(base=defaults.image)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RecipeError(f"{RECIPE_PATH} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise RecipeError(f"{RECIPE_PATH} must be a JSON object")
    try:
        recipe = Recipe.model_validate(data)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}" for err in e.errors()
        )
        raise RecipeError(f"{RECIPE_PATH}: {problems}") from e
    return recipe if recipe.base else recipe.model_copy(update={"base": defaults.image})
