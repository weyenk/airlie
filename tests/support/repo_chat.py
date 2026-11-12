"""Fakes for repository chats: FakeBox VMs (found again under the name a rename gives them), a host mirror
over a recording runner and a recording publisher, and the RepoSessions built over them."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from airlie.boxes.images import Images, SecretStore
from airlie.boxes.mirror import Mirror
from airlie.boxes.protocol import Box, ExecResult, Network
from airlie.chat.confirm import Confirmations
from airlie.chat.repo_session import RepoSessions, build_confirm, slug_of
from airlie.chat.threads import ThreadStore
from airlie.config import AirlieConfig
from airlie.manager.core import Manager
from tests.support.box import FakeBox
from tests.unit.test_transit import FakePublisher, FakeRunner

RECIPE = '{"env": {"A": "1"}, "test": ["pytest -q"], "workdir": "/w"}'
__all__ = ["RECIPE", "FakeMirror", "RegisteredBox", "RepoFakes", "cloning"]


class FakeMirror(Mirror):
    """The mirror's git goes through a recording runner; no fetch, and the recipe is the test's."""

    def __init__(self, root: Path, repo: str, runner: FakeRunner, recipe: str | None) -> None:
        super().__init__(root, repo, FakePublisher(), run=runner)
        self.recipe = recipe
        self.refreshes = 0
        self.refresh_gate: asyncio.Event | None = None
        """When set, a refresh waits on it (after counting itself): a session caught mid-open."""
        self.refreshing = asyncio.Event()

    async def refresh(self) -> None:
        self.refreshes += 1
        self.refreshing.set()
        if self.refresh_gate is not None:
            await self.refresh_gate.wait()

    async def recipe_text(self, ref: str) -> str | None:
        return self.recipe


@dataclass
class RegisteredBox(FakeBox):
    """A FakeBox the registry finds again under the name a rename gives it."""

    registry: dict[str, FakeBox] = field(default_factory=dict)

    async def rename(self, new_name: str) -> None:
        await super().rename(new_name)
        self.registry[new_name] = self


def cloning(box: FakeBox, argv: Sequence[str], stdin: bytes | None) -> ExecResult:
    if argv[1] == "clone":  # the seed's clone leaves a checkout where the next `test -d` finds it
        box.files[f"{argv[-1]}/.git/HEAD"] = b""
    return ExecResult(0, "")


class RepoFakes:
    def __init__(self, tmp_path: Path, *, recipe: str | None = RECIPE) -> None:
        self.tmp = tmp_path
        self.boxes: dict[str, FakeBox] = {}
        self.runner = FakeRunner()
        self.secrets = tmp_path / "secrets"
        self.secrets.mkdir(exist_ok=True)
        self.mirror = FakeMirror(tmp_path / "mirrors", "o/r", self.runner, recipe)
        self.publisher = self.mirror.publisher
        self.sessions: RepoSessions | None = None
        self.network_check: Callable[[Network], None] = lambda network: None
        """What the sessions ask before a VM is made: raise BoxUnavailable for "softnet is missing"."""

    def box_for(self, name: str) -> Box:
        if name not in self.boxes:
            box = RegisteredBox(name, registry=self.boxes)
            box.handlers["git"] = cloning
            if "-building-" in name:
                box.handlers["sh"] = lambda _b, _a, _s: ExecResult(0, "")
            self.boxes[name] = box
        return self.boxes[name]

    def vm(self, thread_id: str = "t1") -> FakeBox:
        return self.boxes[f"airlie-chat-{slug_of(thread_id)}"]

    def script_push(self, thread_id: str = "t1", *, commits: str = "2") -> None:
        """The VM of `thread_id` has `commits` commits to push and the host's git accepts the bundle."""
        branch = f"airlie/chat-{slug_of(thread_id)}"
        box = self.box_for(f"airlie-chat-{slug_of(thread_id)}")
        assert isinstance(box, FakeBox)
        box.responses[("git", "rev-list", "--count", f"{self.base}..{branch}")] = ExecResult(
            0, commits + "\n"
        )
        box.responses[("git", "bundle", "create", "-", f"{self.base}..{branch}")] = ExecResult(0, "VM-BUNDLE")
        self.runner.answers["bundle list-heads"] = f"{self.base} refs/heads/{branch}\n"

    @property
    def base(self) -> str:
        """The commit the mirror resolves every ref to."""
        return self.runner.answers["rev-parse"].strip()

    def build(
        self, cfg: AirlieConfig, manager: Manager, threads: ThreadStore, confirmations: Confirmations
    ) -> RepoSessions:
        images = Images(
            self.box_for, cfg.boxes, secrets=SecretStore(self.secrets), confirm=build_confirm(confirmations)
        )
        self.sessions = RepoSessions(
            cfg,
            manager,
            images=images,
            confirmations=confirmations,
            mirror_for=lambda repo: self.mirror,
            box_for=self.box_for,
            threads=threads,
            network_check=lambda network: self.network_check(network),
        )
        return self.sessions
