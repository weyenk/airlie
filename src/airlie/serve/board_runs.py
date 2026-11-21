"""What `task serve` hands the board run dispatcher: the real start (a card's workflow through
`airlie.workflow.trigger.start_workflow`), the workdir a card runs in, the lock probe, and a cooperative
pause.

A card's workdir is its `repo:<owner>/<name>` checkout under `story.repos_root`; a card naming no repository
runs in `<state_dir>/runs/<key>`. `can_lock` only looks for the workdir's lock file (it never takes it), so
the dispatcher keeps a card Approved while another run holds the workdir, and `start_workflow` still takes the
lock itself when it starts.

The workflow runner has no pause of its own, so a pause is cooperative: the engine a run gets is wrapped so
each model call first waits for the run's gate. `pause_run` closes the gate (the run stops at its next model
call, which is where a run spends its time), `resume_run` opens it. A tool already running finishes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from airlie.config import AirlieConfig
from airlie.engine.protocol import Engine, OnToken
from airlie.engine.types import EngineCapabilities, EngineHealth, EngineStats, GenRequest, GenResult, Message
from airlie.serve.dispatcher import RunDispatcher
from airlie.story.checkout import find_checkout, repo_label, repo_labels
from airlie.story.model import RepositoryError
from airlie.workflow.trigger import start_workflow
from airlie.workflow.worktree import lock_path

StartWorkflow = Callable[..., Awaitable[Any]]
EngineFactory = Callable[[], Engine]


@dataclass
class RunOutcome:
    """What the dispatcher reads of a run: `.ok` and, when not, `.error`."""

    ok: bool
    error: str | None = None


class GatedEngine:
    """An engine whose model calls wait for `gate` to be open; everything else is the wrapped engine's."""

    def __init__(self, inner: Engine, gate: asyncio.Event) -> None:
        self._inner = inner
        self._gate = gate
        self.capabilities: EngineCapabilities = inner.capabilities

    async def start(self) -> None:
        await self._inner.start()

    async def stop(self) -> None:
        await self._inner.stop()

    async def health(self) -> EngineHealth:
        return await self._inner.health()

    async def generate(self, req: GenRequest, on_token: OnToken | None = None) -> GenResult:
        await self._gate.wait()
        return await self._inner.generate(req, on_token)

    async def cancel(self, request_id: str) -> None:
        await self._inner.cancel(request_id)

    async def yes_no(self, role: str, messages: list[Message]) -> tuple[float, float]:
        await self._gate.wait()
        return await self._inner.yes_no(role, messages)

    def stats(self) -> EngineStats:
        return self._inner.stats()


def card_workdir(cfg: AirlieConfig, card: dict[str, Any], *, strict: bool = False) -> Path:
    """The directory `card` runs in. A repository the card names that has no usable checkout is a
    RepositoryError when `strict` (a run must not start without it), else the card's own run directory."""
    if repo_labels(card["labels"]):
        try:
            return find_checkout(cfg.story.repos_root.expanduser(), repo_label(card["labels"]))
        except RepositoryError:
            if strict:
                raise
    own = cfg.observability.state_dir.expanduser() / "runs" / str(card["key"])
    own.mkdir(parents=True, exist_ok=True)
    return own


def lock_is_free(workdir: Path) -> bool:
    """True when no run holds `workdir` (no lock file); looks only, takes nothing."""
    return not lock_path(workdir).exists()


class BoardRuns:
    """The injected callables of one dispatcher, and the gates of its in-flight runs."""

    def __init__(
        self,
        cfg: AirlieConfig,
        engine_factory: EngineFactory,
        *,
        start_workflow: StartWorkflow = start_workflow,
    ) -> None:
        self.cfg = cfg
        self.engine_factory = engine_factory
        self._start_workflow = start_workflow
        self.gates: dict[str, asyncio.Event] = {}

    def workdir_for(self, card: dict[str, Any]) -> Path:
        return card_workdir(self.cfg, card)

    can_lock = staticmethod(lock_is_free)

    async def start(self, *, name: str, card: dict[str, Any], key: str) -> RunOutcome:
        workdir = card_workdir(self.cfg, card, strict=True)
        gate = self.gates[key] = asyncio.Event()
        gate.set()
        engine = GatedEngine(self.engine_factory(), gate)
        try:
            await engine.start()
            try:
                result = await self._start_workflow(
                    name, cfg=self.cfg, engine=engine, workdir=workdir, card=card
                )
            finally:
                await engine.stop()
        finally:
            self.gates.pop(key, None)
        if any(e.get("kind") == "already_running" for e in getattr(result, "events", ())):
            return RunOutcome(False, "workflow already running in this workdir")
        return RunOutcome(True)

    async def pause_run(self, key: str) -> None:
        gate = self.gates.get(key)
        if gate is not None:
            gate.clear()

    async def resume_run(self, key: str) -> None:
        gate = self.gates.get(key)
        if gate is not None:
            gate.set()


def default_engine_factory(cfg: AirlieConfig, transport: Any = None) -> EngineFactory:
    """One HTTP engine per run, on the chat role's engine when there is one, else the first engine."""

    def make() -> Engine:
        from airlie.cli import _http_engine  # imported here: the cli imports serve

        name = cfg.engine_for_role("chat") if "chat" in cfg.roles else next(iter(cfg.engines))
        return _http_engine(cfg, name, transport=transport)

    return make


def build_dispatcher(
    cfg: AirlieConfig,
    board: Any,
    publish: Callable[[str, str], None],
    clock: Callable[[], float],
    *,
    start: StartWorkflow | None = None,
    engine_factory: EngineFactory | None = None,
) -> RunDispatcher:
    """The dispatcher over `board` for `task serve`. `start`, when given, replaces the whole start (tests);
    `engine_factory` replaces the engine a real start builds."""
    runs = BoardRuns(cfg, engine_factory or default_engine_factory(cfg))
    return RunDispatcher(
        board=board,
        cfg=cfg,
        start=start or runs.start,
        publish=publish,
        clock=clock,
        workdir_for=runs.workdir_for,
        can_lock=runs.can_lock,
        pause_run=runs.pause_run,
        resume_run=runs.resume_run,
    )
