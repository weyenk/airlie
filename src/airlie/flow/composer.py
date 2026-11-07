"""The composer: interprets a workflow of typed nodes over a store, one runner per node type.

This is the sequence rule: nodes run in order (or by `next`), a node whose outputs already exist is skipped,
and every input must be present before a node runs. Wiring moves (parallel, each, branch, loop) come later."""

from __future__ import annotations

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

    async def run(self, workflow: str, *, until: str | None = None, item: str | None = None) -> str:
        """Run `workflow` from its start; stop after node `until` if given. Returns the last node reached."""
        spec = self.cfg.workflows[workflow]
        names = list(spec.nodes)
        formats = {name: fmt for n in spec.nodes.values() for name, fmt in n.outputs.items()}
        current: str | None = spec.start
        last = spec.start
        while current is not None:
            node = spec.nodes[current]
            last = current
            try:
                await self._run_node(workflow, current, node, formats, item)
            except EndRun:
                return current
            if current == until:
                return current
            if node.next is not None:
                current = node.next
            else:
                after = names.index(current) + 1
                current = names[after] if after < len(names) else None
        return last

    def _name(self, name: str, item: str | None) -> str:
        return name.replace("{item}", item) if item is not None else name

    def _present(self, name: str, formats: Mapping[str, str], item: str | None) -> bool:
        if formats.get(name) == "session":
            return self.sessions.get(self._name(name, item)) is not None
        return self.store.has(self._name(name, item))

    async def _run_node(
        self, workflow: str, name: str, node: NodeSpec, formats: Mapping[str, str], item: str | None
    ) -> None:
        if node.type in ("parallel", "each") or node.branch is not None or node.loop is not None:
            raise NotImplementedError(f"workflow {workflow}: node {name}: {node.type} wiring comes later")
        fields: dict[str, Any] = {"node": name, "type": node.type, "workflow": workflow, "item": item}
        ctx = RunContext(
            cfg=self.cfg,
            store=self.store,
            sessions=self.sessions,
            registry=self.registry,
            workflow=workflow,
            node=name,
            item=item,
            feedback=self.store.get(f"feedback.{name}") if self.store.has(f"feedback.{name}") else None,
        )
        if node.outputs and all(self._present(o, node.outputs, item) for o in node.outputs):
            self.store.emit("node_skipped", **fields)
            return
        if node.when is not None and not self.registry[node.when](ctx):
            self.store.emit("node_skipped", **fields)
            return
        for needed in node.inputs:
            if not self._present(needed, formats, item):
                raise MissingInput(f"workflow {workflow}: node {name}: missing input '{needed}'")

        self.store.emit("node_started", **fields)
        started = time.monotonic()
        try:
            result = await self.runners[node.type](node, ctx)
        except BaseException:
            self.store.emit(
                "node_finished",
                node=name,
                type=node.type,
                outcome="failed",
                ms=int((time.monotonic() - started) * 1000),
            )
            raise
        failure = result.get(FAIL_KEY)
        for key, value in result.items():
            if key == FAIL_KEY:
                continue
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
            ms=int((time.monotonic() - started) * 1000),
        )
        if failure is not None:
            if isinstance(node.on_fail, LoopSpec):
                raise NotImplementedError(f"workflow {workflow}: node {name}: on_fail loop comes later")
            if node.on_fail == "block":
                from airlie.story.asking import StoryBlocked  # asking imports config, which imports flow.spec

                raise StoryBlocked(str(failure))
