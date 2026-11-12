"""Cached box images: a recipe with setup is built once into `airlie-env-<key>`; every chat clones from it.
Setup secrets exist only while that build runs, and reach the guest only on stdin."""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from pathlib import Path

from airlie.boxes.protocol import Box, Network
from airlie.boxes.recipe import Recipe, RecipeError
from airlie.config.schema import BoxesSpec
from airlie.engine.types import EngineError
from airlie.subproc import box_env

TAIL_CHARS = 2000
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
REPO = re.compile(r"[\w.-]+/[\w.-]+")


class ConfirmationRequired(EngineError):
    """A step that gives setup secrets to a recipe nobody has built before needs a person's yes."""

    def __init__(self, what: str) -> None:
        super().__init__(f"confirmation required: {what}")
        self.what = what


class SecretStore:
    """Setup secrets: `<dir>/<owner>__<name>.env`, KEY=VALUE lines, readable by the owner only. Per repository
    for now; per person comes with per-person chat ownership."""

    def __init__(self, dir: Path) -> None:
        self._dir = dir

    def for_repo(self, repo: str) -> dict[str, str]:
        if not REPO.fullmatch(repo) or ".." in repo:
            raise RecipeError(f"not an owner/name repository: {repo!r}")
        path = self._dir / f"{repo.replace('/', '__')}.env"
        try:
            mode = path.stat().st_mode & 0o777
        except FileNotFoundError:
            return {}
        if mode & 0o077:
            raise RecipeError(f"{path} is readable by others (mode {mode:o}): chmod 600 {path}")
        secrets: dict[str, str] = {}
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep or not NAME.fullmatch(key.strip()):
                raise RecipeError(f"{path} line {n}: expected KEY=VALUE")
            secrets[key.strip()] = value
        return secrets


def _redact(text: str, secrets: dict[str, str]) -> str:
    for value in secrets.values():
        if value:
            text = text.replace(value, "***")
    return text


class Images:
    def __init__(
        self,
        box_for: Callable[[str], Box],
        spec: BoxesSpec,
        *,
        secrets: SecretStore,
        confirm: Callable[[str], bool],
    ) -> None:
        self._box_for = box_for
        self._spec = spec
        self._secrets = secrets
        self._confirm = confirm

    def base_of(self, recipe: Recipe) -> str:
        return recipe.base or self._spec.image

    def image_name(self, recipe: Recipe) -> str:
        return f"airlie-env-{recipe.key(self.base_of(recipe))}"

    async def ensure(self, recipe: Recipe, repo: str) -> str:
        """The name of a stopped, cached image: the base itself without setup, else the built one."""
        base = self.base_of(recipe)
        if not recipe.setup:
            return base
        name = self.image_name(recipe)
        box = self._box_for(name)
        if await box.exists():
            return name
        secrets = self._secrets.for_repo(repo)
        if secrets and not self._confirm(f"build_image:{repo}:{recipe.key(base)}"):
            raise ConfirmationRequired(f"build_image:{repo}:{recipe.key(base)}")
        spec = self._spec
        try:
            await box.create(base, cpus=spec.cpus, memory_gb=spec.memory_gb, disk_gb=spec.disk_gb)
            await box.boot(network=Network("open"), timeout_s=spec.boot_timeout_s)
            header = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in secrets.items())
            env = box_env(recipe.env)
            for line in recipe.setup:
                result = await box.exec(
                    ["sh", "-s"], env=env, stdin=f"{header}{line}\n".encode(), timeout_s=spec.setup_timeout_s
                )
                if result.rc != 0:
                    tail = _redact(result.output, secrets)[-TAIL_CHARS:].strip()
                    shown = "timed out" if result.rc is None else f"exit {result.rc}"
                    raise RecipeError(f"setup failed ({shown}) at: {line}\n{tail}")
            await box.stop()
        except BaseException:
            await box.delete()  # no half-built image stays to be mistaken for a built one
            raise
        return name
