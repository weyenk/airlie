"""The composer: interprets a workflow of typed nodes over a store, one runner per node type.

The sequence rule: nodes run in order (or by `next`), a node whose outputs already exist is skipped, and every
input must be present before a node runs. The wiring moves: `parallel` and `each` run other nodes, `branch`
picks the next node by an output, `loop` (or a LoopSpec `on_fail`) re-runs from a node
with carried feedback."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from airlie.flow.spec import LoopSpec, NodeSpec, NodeType
from airlie.flow.store import Sessions, Store

# airlie.config imports airlie.flow.spec, so this package cannot import the config (or the story) at load.
if TYPE_CHECKING:
    from airlie.config import AirlieConfig

FAIL_KEY = "__fail__"


class MissingInput(RuntimeError):
    """A node was reached without an input it declares."""


class EndRun(Exception):
    """A gate or waiting check raises it: the run stops here and `run` returns the node's name."""


class Registry:
    """Callables by id: brief builders, parsers, predicates, checks."""

    def __init__(self) -> None:
        self._fns: dict[str, Callable[..., Any]] = {}

    def add(self, id: str, fn: Callable[..., Any]) -> None:
        self._fns[id] = fn

    def __getitem__(self, id: str) -> Callable[..., Any]:
        try:
            return self._fns[id]
        except KeyError:
            raise KeyError(f"registry has no '{id}'") from None


@dataclass
class RunContext:
    cfg: AirlieConfig
    store: Store
    sessions: Sessions
    registry: Registry
    workflow: str
    node: str
    item: str | None  # the each-loop item, else None
    feedback: str | None  # the carried loop feedback for this node, else None


Runner = Callable[[NodeSpec, RunContext], Awaitable[Mapping[str, Any]]]


def resolve_count(cfg: AirlieConfig, value: int | str) -> int:
    """An int as is; a str is a dotted attribute path on the config."""
    if isinstance(value, int):
        return value
    from airlie.config import ConfigError

    found: Any = cfg
    for part in value.split("."):
        if not hasattr(found, part):
            raise ConfigError(f"count '{value}': config has no '{part}'")
        found = getattr(found, part)
    return int(found)


class Composer:
    def __init__(
        self,
        cfg: AirlieConfig,
        *,
        runners: Mapping[NodeType, Runner],
        registry: Registry,
        store: Store,
        sessions: Sessions,
    ) -> None:
        self.cfg = cfg
        self.runners = runners
        self.registry = registry
        self.store = store
        self.sessions = sessions
        self._loops: dict[
            tuple[str, str | None, str, str], int
        ] = {}  # (workflow, item, node, which) -> re-runs

    async def run(self, workflow: str, *, until: str | None = None, item: str | None = None) -> str:
        """Run `workflow` from its start; stop after node `until` if given. Returns the last node reached."""
        return await self._walk(workflow, until, item, bundle=False)

    async def _walk(self, workflow: str, until: str | None, item: str | None, *, bundle: bool) -> str:
        spec = self.cfg.workflows[workflow]
        names = list(spec.nodes)
        formats = {name: fmt for n in spec.nodes.values() for name, fmt in n.outputs.items()}
        children = {c for n in spec.nodes.values() if n.type == "parallel" for c in n.nodes}
        current: str | None = spec.start
        forced = False  # arrived by a loop jump: redo the node even if its outputs exist
        last = spec.start
        while current is not None:
            node = spec.nodes[current]
            last = current
            try:
                result = await self._step(workflow, current, node, formats, item, force=forced)
            except EndRun:
                if bundle:
                    raise
                return current
            target, forced = self._route(workflow, current, node, names, children, result, item)
            if (
                current == until and not forced
            ):  # routed first so a spent loop blocks; a loop jump is not done yet
                return current
            current = target
        return last

    def _route(
        self,
        workflow: str,
        name: str,
        node: NodeSpec,
        names: list[str],
        children: set[str],
        result: Mapping[str, Any],
        item: str | None,
    ) -> tuple[str | None, bool]:
        """The node after `name` and whether a loop jump forces it: a loop's target, a branch, `next`, else
        dict order (stepping over parallel children, which run only through their parent)."""
        failed = FAIL_KEY in result
        if result:  # a skipped node (empty result) never loops
            ctx = self._ctx(workflow, name, item)
            if node.loop is not None:
                loop = node.loop
                if (self.registry[loop.when](ctx, result) if loop.when else failed) and self._again(
                    workflow, name, "loop", loop, result, item
                ):
                    return loop.to, True
            if (
                failed
                and isinstance(node.on_fail, LoopSpec)
                and self._again(workflow, name, "on_fail", node.on_fail, result, item)
            ):
                return node.on_fail.to, True
            if (
                failed
                and node.on_fail != "continue"
                and (node.loop is not None or isinstance(node.on_fail, LoopSpec))
            ):
                from airlie.story.asking import StoryBlocked  # asking imports config, which imports flow.spec

                raise StoryBlocked(str(result[FAIL_KEY]))
        if node.branch is not None and node.on is not None:
            value = result[node.on] if node.on in result else self.store.get(self._name(node.on, item))
            target = node.branch.get(str(value), node.branch.get("*"))
            if target is not None:
                return target, False
        if node.next is not None:
            return node.next, False
        rest = [n for n in names[names.index(name) + 1 :] if n not in children]
        return (rest[0] if rest else None), False

    def _again(
        self,
        workflow: str,
        name: str,
        which: str,
        loop: LoopSpec,
        result: Mapping[str, Any],
        item: str | None,
    ) -> bool:
        """Count one re-run if the bound allows it, carrying feedback; False once the count is spent."""
        key = (workflow, item, name, which)
        if self._loops.get(key, 0) >= resolve_count(self.cfg, loop.max):
            return False
        self._loops[key] = self._loops.get(key, 0) + 1
        if loop.carry is not None and loop.carry in result:
            self.store.put(f"feedback.{loop.to}", result[loop.carry])
        return True

    def _ctx(self, workflow: str, name: str, item: str | None) -> RunContext:
        return RunContext(
            cfg=self.cfg,
            store=self.store,
            sessions=self.sessions,
            registry=self.registry,
            workflow=workflow,
            node=name,
            item=item,
            feedback=self.store.get(f"feedback.{name}") if self.store.has(f"feedback.{name}") else None,
        )

    async def _step(
        self,
        workflow: str,
        name: str,
        node: NodeSpec,
        formats: Mapping[str, str],
        item: str | None,
        *,
        force: bool = False,
    ) -> Mapping[str, Any]:
        if node.type not in ("parallel", "each"):
            return await self._run_node(workflow, name, node, formats, item, force=force)
        fields: dict[str, Any] = {"node": name, "type": node.type, "workflow": workflow, "item": item}
        if node.when is not None and not self.registry[node.when](self._ctx(workflow, name, item)):
            self.store.emit("node_skipped", **fields)
            return {}
        self.store.emit("node_started", **fields)
        started = time.monotonic()
        outcome = "failed"
        try:
            if node.type == "parallel":
                await self._parallel(workflow, node, formats, item)
            else:
                await self._each(node)
            outcome = "done"
        except EndRun:
            outcome = "done"
            raise
        finally:
            self.store.emit("node_finished", node=name, type=node.type, outcome=outcome, ms=self._ms(started))
        return {}

    async def _parallel(
        self, workflow: str, node: NodeSpec, formats: Mapping[str, str], item: str | None
    ) -> None:
        spec = self.cfg.workflows[workflow]
        tasks = [
            asyncio.ensure_future(self._run_node(workflow, child, spec.nodes[child], formats, item))
            for child in node.nodes
        ]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _each(self, node: NodeSpec) -> None:
        assert node.over is not None and node.workflow is not None
        for i in range(len(self.store.get(node.over))):
            await self._walk(node.workflow, None, f"{i + 1:02d}", bundle=True)

    @staticmethod
    def _ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    def _name(self, name: str, item: str | None) -> str:
        return name.replace("{item}", item) if item is not None else name

    def _present(self, name: str, formats: Mapping[str, str], item: str | None) -> bool:
        if formats.get(name) == "session":
            return self.sessions.get(self._name(name, item)) is not None
        return self.store.has(self._name(name, item))

    async def _run_node(
        self,
        workflow: str,
        name: str,
        node: NodeSpec,
        formats: Mapping[str, str],
        item: str | None,
        *,
        force: bool = False,
    ) -> Mapping[str, Any]:
        """Run one node; returns the runner's full result mapping (empty when skipped)."""
        fields: dict[str, Any] = {"node": name, "type": node.type, "workflow": workflow, "item": item}
        ctx = self._ctx(workflow, name, item)
        if not force and node.outputs and all(self._present(o, node.outputs, item) for o in node.outputs):
            self.store.emit("node_skipped", **fields)
            return {}
        if node.when is not None and not self.registry[node.when](ctx):
            self.store.emit("node_skipped", **fields)
            return {}
        for needed in node.inputs:
            if not self._present(needed, formats, item):
                raise MissingInput(f"workflow {workflow}: node {name}: missing input '{needed}'")

        self.store.emit("node_started", **fields)
        started = time.monotonic()
        try:
            result = await self.runners[node.type](node, ctx)
        except EndRun:  # a gate did its job
            self.store.emit("node_finished", node=name, type=node.type, outcome="done", ms=self._ms(started))
            raise
        except BaseException:
            self.store.emit(
                "node_finished", node=name, type=node.type, outcome="failed", ms=self._ms(started)
            )
            raise
        failure = result.get(FAIL_KEY)
        for key, value in ({} if failure is not None else result).items():
            target = self._name(key, item)
            if node.outputs.get(key) == "session":
                self.sessions.put(target, value)
            else:
                self.store.put(target, value)
        self.store.save()
        self.store.emit(
            "node_finished",
            node=name,
            type=node.type,
            outcome="failed" if failure is not None else "done",
            ms=self._ms(started),
        )
        if failure is not None and node.on_fail == "block" and node.loop is None:
            from airlie.story.asking import StoryBlocked  # asking imports config, which imports flow.spec

            raise StoryBlocked(str(failure))

        return result
