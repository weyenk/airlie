"""The host's clone of a repository for chat: the only place the host's credentials touch git for a chat.
A bare repository at `root/<owner>__<name>` whose remote `origin` is the repository on GitHub (fetched under
the GitHub App's token for that step, Publisher._step) or, with no App configured, the person's own checkout
(story.repos_root: only read, and nothing is pushed). A VM never sees it: seed hands a VM a bundle cut from
it, and harvest fetches a bundle back into it."""

from __future__ import annotations

import os
from pathlib import Path

from airlie.boxes.recipe import RECIPE_PATH
from airlie.engine.types import EngineError
from airlie.story.publish import Publisher, PublishError, Runner
from airlie.subproc import NO_PROMPTS, agent_env, without_hooks


class MirrorError(EngineError):
    """The host's clone could not be made, refreshed or read."""


class Mirror:
    def __init__(
        self,
        root: Path,
        repo: str,
        publisher: Publisher,
        *,
        run: Runner,
        local_checkout: Path | None = None,
    ) -> None:
        owner, _, name = repo.partition("/")
        if not owner or not name or "/" in name:
            raise MirrorError(f"{repo!r} is not an owner/name repository")
        self.repo = repo
        self.path = root / f"{owner}__{name}"
        self.publisher = publisher
        self.run = run
        self.local_checkout = local_checkout
        self.can_push = publisher.app is not None
        """Only as the GitHub App's bot: a chat never pushes with the person's own login."""

    def env(self) -> dict[str, str]:
        """For airlie's own git here: no credentials, no prompt, no hook, no fsmonitor, no replace refs, and
        no GIT_* variable of the process's own (GIT_DIR, GIT_CONFIG_GLOBAL, GIT_EXEC_PATH ... point git
        elsewhere); the no-prompt switches are re-added."""
        env = {k: v for k, v in agent_env().items() if not k.startswith("GIT_")}
        return without_hooks({**env, **NO_PROMPTS})

    async def git(self, *args: str) -> str:
        return await self.run(["git", *args], self.path, self.env())

    async def refresh(self) -> None:
        """Clone on first use, then fetch every branch and prune. With the App, the fetch is the publisher's
        (a token for that step alone, scoped to the repository); without one it reads the local checkout."""
        if self.publisher.app is None and self.local_checkout is None:
            raise MirrorError("no GitHub App (story.github_app) and no local checkout to read the code from")
        if not self.path.exists():
            await self._create()
        else:
            await self._repoint()
        if self.publisher.app is not None:
            self.publisher.prepare(self.path, self.repo)
            await self.publisher.fetch_all(self.path)
        else:
            refspec = "+refs/heads/*:refs/remotes/origin/*"
            await self.git("fetch", "-q", "--prune", "--refmap=", "origin", refspec)

    def _origin(self) -> str:
        """Where this mirror fetches from: GitHub with the App, else the local checkout."""
        if self.publisher.app is not None:
            return f"https://github.com/{self.repo}.git"
        assert self.local_checkout is not None  # refresh checked
        return os.fspath(self.local_checkout)

    async def _create(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        await self.run(["git", "init", "-q", "--bare", os.fspath(self.path)], self.path.parent, self.env())
        await self.git("remote", "add", "origin", self._origin())

    async def _repoint(self) -> None:
        """A mirror made for a local checkout that GitHub now serves (or the other way round) fetches from
        where it is read now: its `origin` follows."""
        url = self._origin()
        if (await self.git("remote", "get-url", "origin")).strip() != url:
            await self.git("remote", "set-url", "origin", url)

    async def resolve(self, ref: str) -> str:
        """The commit `ref` names in the mirror."""
        if ref.startswith("-"):
            raise MirrorError(f"refusing the ref {ref!r}: git would read it as an option")
        try:
            out = await self.git("rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
        except PublishError as e:
            raise MirrorError(f"{ref} is not a commit in {self.repo}: {e}") from None
        return out.strip()

    async def recipe_text(self, ref: str) -> str | None:
        """The environment recipe at `ref`; None when the repository has none there."""
        sha = await self.resolve(ref)
        try:
            return await self.git("show", f"{sha}:{RECIPE_PATH}")
        except PublishError:
            return None
