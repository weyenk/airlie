"""Cached box images: a recipe with setup is built once into `airlie-env-<key>`; every chat clones from it.
Setup secrets exist only while that build runs, and reach the guest only on stdin."""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets as secrets_module
import shlex
from collections.abc import Callable
from pathlib import Path

from airlie.boxes.protocol import Box, ConfirmationRequired, Network
from airlie.boxes.recipe import Recipe, RecipeError
from airlie.config.schema import BoxesSpec
from airlie.subproc import box_env

TAIL_CHARS = 2000
SYNC_S = 60.0
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
REPO = re.compile(r"[\w.-]+/[\w.-]+")


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


MIN_REDACTED = 4
"""A secret shorter than this is not replaced in a failure's output: every `1` would be."""


def _token() -> str:
    return secrets_module.token_hex(4)


def _redact(text: str, secrets: dict[str, str]) -> str:
    for value in secrets.values():
        if len(value) >= MIN_REDACTED:
            text = text.replace(value, "***")
    return text


LIVE_BUILDS: set[str] = set()
"""The temporary names of the builds running in this process: the leftover sweep never touches them."""


class Images:
    def __init__(
        self,
        box_for: Callable[[str], Box],
        spec: BoxesSpec,
        *,
        secrets: SecretStore,
        confirm: Callable[[str], bool],
        token: Callable[[], str] = _token,
    ) -> None:
        self._box_for = box_for
        self._spec = spec
        self._secrets = secrets
        self._confirm = confirm
        self._token = token
        self._locks: dict[str, asyncio.Lock] = {}

    def base_of(self, recipe: Recipe) -> str:
        return recipe.base or self._spec.image

    def image_name(self, recipe: Recipe, repo: str | None = None) -> str:
        """`airlie-env-<key>`; with `repo` (an image built with that repository's setup secrets) the
        repository is part of the name, so another repository never gets an image that holds them."""
        name = f"airlie-env-{recipe.key(self.base_of(recipe))}"
        return f"{name}-{hashlib.sha256(repo.encode()).hexdigest()[:8]}" if repo else name

    async def ensure(self, recipe: Recipe, repo: str) -> str:
        """The name of a stopped, cached image: the base itself without setup, else the built one."""
        base = self.base_of(recipe)
        if not recipe.setup:
            return base
        secrets = self._secrets.for_repo(repo)
        name = self.image_name(recipe, repo if secrets else None)
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:  # two chats opening at once build it once
            if await self._box_for(name).exists():
                return name
            key = f"build_image:{repo}:{recipe.key(base)}"
            if secrets and not self._confirm(key):
                raise ConfirmationRequired(key)
            await self._build(recipe, base, name, secrets)
        return name

    async def _build(self, recipe: Recipe, base: str, name: str, secrets: dict[str, str]) -> None:
        """Build under a temporary name and rename it to `name` once setup has succeeded and the VM is
        stopped: whatever a failure leaves behind (a cleanup that itself fails) is never `name`.

        The build VM is not a manager resource of its own: it runs only inside the start of the chat VM
        that needs the image (VMResource.start), whose slot is counted for memory, `starting`, while it
        builds, and that chat VM is not booted until the build VM has stopped. So one footprint covers
        both, and the manager's budget holds."""
        spec = self._spec
        box = self._box_for(f"{name}-building-{self._token()}")
        building = box.name
        LIVE_BUILDS.add(building)
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
            synced = await box.exec(["sync"], timeout_s=SYNC_S)  # a VM's stop loses what is not on its disk
            if synced.rc != 0:
                raise RecipeError(f"the image's sync failed before it was saved: {synced.output.strip()}")
            await box.stop()
            await box.rename(name)
        except BaseException as failure:
            try:
                await box.delete()
            except Exception as cleanup:  # the failure that matters is the first one
                failure.add_note(f"deleting the partial image {box.name} failed too: {cleanup}")
            raise
        finally:
            LIVE_BUILDS.discard(building)
