"""The repository recipe: `.airlie/environment.json`, which travels with the checkout and names the base
image, a setup script, the environment variables and the test commands of a repository's VM."""

from __future__ import annotations

import hashlib
import json
import re

from pydantic import ConfigDict, ValidationError

from airlie.config.schema import BoxesSpec, Strict
from airlie.engine.types import EngineError

RECIPE_PATH = ".airlie/environment.json"
_COMPONENT = r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
_REGISTRY_IMAGE = re.compile(
    rf"({_COMPONENT})(?::\d+)?"  # the host[:port]
    rf"(?:/{_COMPONENT})+"  # at least one path component
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|@sha256:[0-9a-f]{64})?"  # a tag or a digest
)


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
        """Names the cached image: the base, the setup and the variables, names and values (setup runs with
        them, so a changed value is another build; they are committed, so a secret is never part of it)."""
        canonical = json.dumps([image, list(self.setup), sorted(self.env.items())], separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def registry_image(base: str) -> bool:
    """An image `tart clone` pulls from a registry (`host[:port]/path[/path...][:tag|@sha256:<64 hex>]`),
    never a local VM: a recipe that could name one could start from another repository's image, built with
    its setup secrets, or another chat's disk. Every component is lowercase `[a-z0-9]+([._-][a-z0-9]+)*`,
    which is also what makes tart parse the name as remote: a name its grammar rejects (`..`, an empty or
    upper-case part) it opens as a path under ~/.tart/vms, case-insensitively on APFS."""
    found = _REGISTRY_IMAGE.fullmatch(base)
    if found is None or not ("." in found[1] or found[1] == "localhost"):
        return False
    return not any(p.startswith("airlie-") for p in base.split("/")[1:])


def load_recipe(text: str | None, defaults: BoxesSpec) -> Recipe:
    """The recipe in `text` (the file's content; None when the repository has none). Malformed JSON, an
    unknown key, or a `base` that is neither a registry image nor `boxes.image` raises RecipeError naming
    it."""
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
    if recipe.base is not None and recipe.base != defaults.image and not registry_image(recipe.base):
        raise RecipeError(
            f"{RECIPE_PATH}: base: {recipe.base!r} is not a registry image (host/name[:tag], such as "
            f"{defaults.image}); a recipe never starts from a local VM"
        )
    return recipe if recipe.base else recipe.model_copy(update={"base": defaults.image})
