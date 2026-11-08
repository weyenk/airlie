"""The composer: a dataflow scheduler over a workflow of typed nodes joined by lines, one runner per node type
(nodes-are-config design: "The canvas", "Bundles and isolation", D4-D6, D15, D17, D23).

A line goes from the node that lists it in `to` to the node it names. A line with a `loop` bound is a loop
line (drawn back to an earlier node); every other line is a forward line, and only forward lines decide
when a node runs. The rules, as `_Run` implements them:

- A line **fires** when its source finishes (succeeds, or fails with `on_fail: continue`) and its `when` is
  absent or holds; it **dies** when its source finishes and its `when` does not hold, or when its source
  dies. A node **dies** when it has forward lines in and every one of them died; a dead node emits
  `node_skipped` and every line out of it dies, so nothing waits on a branch not taken.
- A node is **ready** when every forward line into it has fired or died, at least one fired (when it has
  any; a node with none is a start node), and every input is satisfied. An input is satisfied when it is
  present (a session in the run's sessions, else `store.has`), when its name is carried feedback
  (`feedback...`, absent on a first try), or when it has producers and every one of them that is neither
  the node itself nor downstream of it has finished or died: an artifact a producer left empty (`[]`, `""`)
  or never wrote (a failed survey, the alternative not taken) does not stall the node. A name nothing in
  the workflow produces must be present.
- A ready node is **skipped** (finished, `node_skipped`, recorded done) when it has outputs and they are
  all present, or when it has none and its done name is in the store's done set: this is the resume. A
  done name is `<node>`, `<node>@<item>` in a bundle item, behind the bundle node(s) it runs under
  (`lay/build@01`). A node a loop sent back to is never skipped so. Every other ready node **runs**: it
  leaves the done set as it starts, and all that are ready run at once, each as a task; one that raises
  cancels the others and its error is raised. A node that succeeds is recorded done; a failed one is not,
  so a resume runs it again.
- When a node finishes, its loop lines are tried first, in order: one fires when its `when` holds (with no
  `when`, when the node failed) and its bound (`max`, a count or a config path) is not spent. It puts its
  `carry` under `feedback.<target>` (`.<item>` in a bundle item), cancels any of the target and the nodes
  downstream of it (through forward lines) still running, and sends them back to pending: out of the done
  set, their outputs cleared (`store.put(name, None)`; a session popped; the memory heads kept) and the
  lines out of them unresolved; a bundle node sent back forgets its items too (their done names, the
  bundle workflow's outputs, feedback and loop counts), so each item runs again. Else a failed node with
  `on_fail: block` raises `StoryBlocked`; else its forward lines fire or die by their conditions.
- A `bundle` node runs its workflow once per item of its `each` list (`01`, `02`, ... in order), or once
  without `each`, on a `BundleStore`: the parent's store read by name, written only under the bundle
  workflow's declared outputs.
- `until`: once the named node finishes (or is skipped), nothing new starts; the run returns its name
  when the nodes still running end. A gate (`EndRun`) ends the run the same way, and inside a bundle it
  ends the parent's run at the bundle node. A run where nothing can start and some node is still pending
  raises `Stalled` naming the pending nodes and what each lacks. A condition reads `RunContext.lookup`: the
  store, the card's fields, `config.<path>`."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from airlie.flow import conditions
from airlie.flow.prompt_files import FEEDBACK, PREFIX, prompt_builder
from airlie.flow.spec import Line, NodeSpec, NodeType, WorkflowSpec
from airlie.flow.store import MEMORY, BundleStore, Sessions, Store
from airlie.prompts.hub import PromptText

# airlie.config imports airlie.flow.spec, so this package cannot import the config (or the story) at load.
if TYPE_CHECKING:
    from airlie.config import AirlieConfig
    from airlie.flow.catalog import Catalog

FAIL_KEY = "__fail__"


class MissingInput(RuntimeError):
    """A catalog check was handed a context without an input it declares. The composer never starts a node
    without its inputs (it waits, or raises `Stalled`); a check called another way raises this."""


class Stalled(RuntimeError):
    """Nothing in a workflow can run and some node has not: the message names each waiting node and what it
    lacks."""


class EndRun(Exception):
    """A gate or waiting check raises it: the run stops here and `run` returns the node's name."""


class Registry:
    """Callables by id: brief builders, parsers, predicates, checks. An id `prompt:<id>` that was not added
    is a prompt file's brief builder (`prompt_files.prompt_builder`): a draft's file from `prompts`, else
    the hub's rule. `catalog` is the checks a check or gate node's `run` names (None: they are looked up
    here by id)."""

    def __init__(
        self, prompts: Mapping[str, PromptText] | None = None, *, catalog: Catalog | None = None
    ) -> None:
        self._fns: dict[str, Callable[..., Any]] = {}
        self.prompts: Mapping[str, PromptText] = dict(prompts or {})
        self.catalog = catalog

    def add(self, id: str, fn: Callable[..., Any]) -> None:
        self._fns[id] = fn

    def __getitem__(self, id: str) -> Callable[..., Any]:
        if id in self._fns:
            return self._fns[id]
        if id.startswith(PREFIX):
            return prompt_builder(id.removeprefix(PREFIX), self.prompts)
        raise KeyError(f"registry has no '{id}'")

    def check(self, run: str) -> Callable[..., Any]:
        """The function a check or gate node's `run` names: the catalog's, else the registry's id."""
        return self.catalog[run].fn if self.catalog is not None else self[run]


@dataclass
class RunContext:
    cfg: AirlieConfig
    store: Store
    sessions: Sessions
    registry: Registry
    workflow: str
    node: str
    item: str | None  # the bundle item, else None
    feedback: str | None  # the carried loop feedback for this node, else None
    held: contextlib.AsyncExitStack | None = None  # the run's: what nodes hold open for later ones
    inputs: Collection[str] = ()  # the names the node reads: its inputs and its workflow's inputs
    texts: Mapping[str, PromptText] = field(default_factory=dict)  # the drafts' prompt files, by id

    def lookup(self, path: str) -> Any:
        """The value of a dotted path: `feedback` is the carried feedback; `config.<a>.<b>` is read on the
        config; anything else on the store, the longest name it holds first (`verdict.decision` is the
        `decision` of `verdict`; `card.level` the loose card's field, by key or attribute). A path with no
        value (missing, or None) is a KeyError."""
        if path == FEEDBACK:
            if self.feedback is None:
                raise KeyError(path)
            return self.feedback
        parts = path.split(".")
        if parts[0] == "config":
            return _descend(self.cfg, parts[1:], path)
        for cut in range(len(parts), 0, -1):
            try:
                found = self.store.get(".".join(parts[:cut]))
            except KeyError:
                continue
            return _descend(found, parts[cut:], path)
        raise KeyError(path)


def _descend(value: Any, parts: list[str], path: str) -> Any:
    for part in parts:
        if isinstance(value, Mapping):
            if part not in value:
                raise KeyError(path)
            value = value[part]
        elif value is None or not hasattr(value, part):
            raise KeyError(path)
        else:
            value = getattr(value, part)
    if value is None:
        raise KeyError(path)
    return value


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
        workflows: Mapping[str, WorkflowSpec] | None = None,
        texts: Mapping[str, PromptText] | None = None,
    ) -> None:
        self.cfg = cfg
        self.workflows = workflows
        self.runners = runners
        self.registry = registry
        self.store = store
        self.sessions = sessions
        self.texts: Mapping[str, PromptText] = registry.prompts if texts is None else texts
        self.held: contextlib.AsyncExitStack | None = None
        """Open while `run` runs: a conversation a node holds for later nodes (a session) is entered on it,
        and closed, whatever ends the run, when `run` returns or raises. None outside a run, so a node that
        would hold a session there fails loudly instead of holding it on a stack nothing closes."""
        self.loops: dict[tuple[str, str | None, str, str], int] = {}
        """Re-runs a loop line has taken: (`<bundle scope>workflow`, item, source, target) -> count."""

    def spec(self, name: str) -> WorkflowSpec:
        """The workflow `name`: from the mapping the composer was given (the repo's workflows and drafts),
        else from the config."""
        found = self.cfg.workflows if self.workflows is None else self.workflows
        if name not in found:
            raise KeyError(f"no workflow named {name!r}")
        return found[name]

    async def run(self, workflow: str, *, until: str | None = None, item: str | None = None) -> str:
        """Run `workflow` (the rules are the module's); stop once node `until` finishes, if given. Returns
        the last node that finished (or was skipped by its outputs), or the gate that ended the run. What the
        run's nodes held open (`held`) is closed when it returns or raises."""
        async with contextlib.AsyncExitStack() as held:
            self.held = held
            try:
                return await _Run(self, workflow, item, self.store, until=until, bundle=False).go()
            finally:
                self.held = None


State = Literal["pending", "running", "finished", "dead"]
LineId = tuple[str, int]  # (source node, index in its `to`)


class _Run:
    """One run of one workflow (one bundle item) over `store`."""

    def __init__(
        self,
        c: Composer,
        workflow: str,
        item: str | None,
        store: Store,
        *,
        until: str | None,
        bundle: bool,
        scope: str = "",
    ) -> None:
        self.c, self.workflow, self.item, self.store = c, workflow, item, store
        self.until, self.bundle = until, bundle
        self.scope = scope  # `<bundle node>/` per level of bundle: the done names' prefix
        self.spec = c.spec(workflow)
        self.nodes = self.spec.nodes
        self.names = list(self.nodes)
        self.formats = {o: s.format for n in self.nodes.values() for o, s in n.outputs.items()}
        self.producers: dict[str, set[str]] = {}
        self.into: dict[str, list[LineId]] = {name: [] for name in self.names}
        self.lines: dict[LineId, Line] = {}
        for name, node in self.nodes.items():
            for out in node.outputs:
                self.producers.setdefault(out, set()).add(name)
            for i, line in enumerate(node.to):
                self.lines[(name, i)] = line
                if line.loop is None:
                    self.into[line.node].append((name, i))
        self.state: dict[str, State] = dict.fromkeys(self.names, "pending")
        self.line_state: dict[LineId, Literal["fired", "dead"] | None] = dict.fromkeys(self.lines)
        self.forced: set[str] = set()  # sent back by a loop: run, never skipped by outputs
        self.running: dict[asyncio.Task[Mapping[str, Any]], str] = {}
        self.last: str | None = None
        self._below: dict[str, set[str]] = {}
        self.items_run: dict[str, list[str | None]] = {}  # bundle node -> the items it ran this run
        self._skipped = False  # settle skipped a node: save once
        self._conditions: dict[str, conditions.Condition] = {}

    # ---- names ----

    def name(self, name: str) -> str:
        return name.replace("{item}", self.item) if self.item is not None else name

    def done_name(self, node: str) -> str:
        return _done_name(self.scope, node, self.item)

    def feedback_name(self, node: str) -> str:
        """Where the feedback carried to `node` is kept: per bundle item, so a layer's first try never reads
        what the layer before it was told."""
        return _feedback_name(node, self.item)

    def fields(self, name: str) -> dict[str, Any]:
        return {"node": name, "type": self.nodes[name].type, "workflow": self.workflow, "item": self.item}

    # ---- the graph ----

    def downstream(self, name: str) -> set[str]:
        """Every node reached from `name` through forward lines, `name` excluded unless a cycle returns."""
        if name not in self._below:
            seen: set[str] = set()
            todo = [ln.node for ln in self.nodes[name].to if ln.loop is None]
            while todo:
                at = todo.pop()
                if at not in seen:
                    seen.add(at)
                    todo.extend(ln.node for ln in self.nodes[at].to if ln.loop is None)
            self._below[name] = seen
        return self._below[name]

    def present(self, name: str) -> bool:
        if self.formats.get(name) == "session":
            return self.c.sessions.get(self.name(name)) is not None
        return self.store.has(self.name(name))

    def satisfied(self, node: str, name: str) -> bool:
        if self.present(name) or name.partition(".")[0] == FEEDBACK:
            return True
        producers = self.producers.get(name)
        if not producers:
            return False
        before = producers - {node} - self.downstream(node)
        return all(self.state[p] in ("finished", "dead") for p in before)

    def ready(self, name: str) -> bool:
        lines = [self.line_state[ln] for ln in self.into[name]]
        if None in lines or (lines and "fired" not in lines):
            return False
        return all(self.satisfied(name, x) for x in self.nodes[name].inputs)

    def skippable(self, name: str) -> bool:
        if name in self.forced:
            return False
        outs = self.nodes[name].outputs
        if outs:  # a node with outputs is done when they are all present, whatever the done set says
            return all(self.present(o) for o in outs)
        return self.store.done(self.done_name(name))

    def holds(self, when: str | None, source: str) -> bool:
        if when is None:
            return True
        if when not in self._conditions:
            self._conditions[when] = conditions.parse(when)
        return self._conditions[when].evaluate(self.ctx(source).lookup)

    def fire(self, source: str) -> None:
        for i, line in enumerate(self.nodes[source].to):
            if line.loop is None:
                self.line_state[(source, i)] = "fired" if self.holds(line.when, source) else "dead"

    def settle(self) -> str | None:
        """Kill the nodes every line into which died, and skip the ready nodes done already, until neither
        changes anything; saved once if a node was skipped. Returns `until` when it was skipped (the run
        stops there), else None."""
        self._skipped = False
        try:
            return self._settle()
        finally:
            if self._skipped:
                self.store.save()

    def _settle(self) -> str | None:
        changed = True
        while changed:
            changed = False
            for name in self.names:
                if self.state[name] != "pending":
                    continue
                lines = [self.line_state[ln] for ln in self.into[name]]
                if lines and all(s == "dead" for s in lines):
                    self.state[name] = "dead"
                    for i, line in enumerate(self.nodes[name].to):
                        if line.loop is None:
                            self.line_state[(name, i)] = "dead"
                    self.store.emit("node_skipped", **self.fields(name))
                    changed = True
                elif self.ready(name) and self.skippable(name):
                    self.state[name] = "finished"
                    self.store.mark_done(self.done_name(name))
                    self.store.emit("node_skipped", **self.fields(name))
                    self.fire(name)
                    self.last = name
                    self._skipped = changed = True
                    if name == self.until:
                        return name
        return None

    def stalled(self) -> Stalled:
        pending = [n for n in self.names if self.state[n] == "pending"]
        waiting = [n for n in pending if None not in (self.line_state[ln] for ln in self.into[n])] or pending
        parts = []
        for n in waiting:
            lacks = [x for x in self.nodes[n].inputs if not self.satisfied(n, x)]
            lacks += [f"the line from {src}" for src, i in self.into[n] if self.line_state[(src, i)] is None]
            parts.append(f"{n}: {', '.join(lacks)}")
        return Stalled(f"workflow {self.workflow}: nothing can run; waiting: {'; '.join(parts)}")

    # ---- running ----

    def ctx(self, name: str) -> RunContext:
        fb = self.feedback_name(name)
        return RunContext(
            cfg=self.c.cfg,
            store=self.store,
            sessions=self.c.sessions,
            registry=self.c.registry,
            workflow=self.workflow,
            node=name,
            item=self.item,
            feedback=self.store.get(fb) if self.store.has(fb) else None,
            held=self.c.held,
            inputs=frozenset(self.nodes[name].inputs) | set(self.spec.inputs),
            texts=self.c.texts,
        )

    def start(self, name: str) -> None:
        self.state[name] = "running"
        self.forced.discard(name)
        self.store.clear_done(self.done_name(name))  # running again: not done until it finishes
        self.store.emit("node_started", **self.fields(name))
        self.running[asyncio.ensure_future(self.step(name))] = name

    async def go(self) -> str:
        stop: str | None = None
        ended = False
        try:
            while True:
                if stop is None:
                    stop = self.settle()
                if stop is None:
                    for name in self.names:
                        if self.state[name] == "pending" and self.ready(name):
                            self.start(name)
                if not self.running:
                    break
                finished, _ = await asyncio.wait(set(self.running), return_when=asyncio.FIRST_COMPLETED)
                order = {t: self.names.index(self.running[t]) for t in finished}
                for task in sorted(finished, key=order.__getitem__):
                    name = self.running.pop(task, "")
                    if not name or task.cancelled():
                        continue  # sent back by a loop while it ran (or after it returned)
                    try:
                        result = task.result()
                    except EndRun:
                        self.state[name] = "finished"
                        self.last = stop = name
                        ended = True
                        continue
                    await self.after(name, result)
                    if name == self.until and self.state[name] == "finished":
                        stop = name
        except BaseException:
            await self.cancel(list(self.running))
            raise
        if ended and self.bundle:
            raise EndRun()
        if stop is not None:
            return stop
        if any(s == "pending" for s in self.state.values()):
            raise self.stalled()
        return self.last or (self.names[0] if self.names else self.workflow)

    async def cancel(self, tasks: list[asyncio.Task[Mapping[str, Any]]]) -> None:
        for task in tasks:
            task.cancel()
            self.running.pop(task, None)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def step(self, name: str) -> Mapping[str, Any]:
        """Run one node: its runner (a bundle's items here), its declared outputs stored unless it failed,
        `node_finished` with the outcome."""
        node = self.nodes[name]
        started = time.monotonic()
        try:
            if node.type == "bundle":
                result: Mapping[str, Any] = await self.bundle_items(name, node)
            else:
                result = await self.c.runners[node.type](node, self.ctx(name))
        except EndRun:  # a gate did its job
            self.finished(name, "done", started)
            raise
        except BaseException:
            self.finished(name, "failed", started)
            raise
        failure = result.get(FAIL_KEY)
        # only the node's declared outputs land: a loop's carry key and FAIL_KEY are for routing, and any
        # other key a runner returns is not an artifact (a StateStore would keep it as an artifact)
        for key, out in ({} if failure is not None else node.outputs).items():
            target = self.name(key)
            if key in result:
                value = result[key]
            elif target in result:
                value = result[target]
            else:
                continue
            if out.format == "session":
                self.c.sessions.put(target, value)
            else:
                self.store.put(target, value)
        if failure is None:
            self.store.mark_done(self.done_name(name))  # a loop line that fires takes it out again
        self.store.save()
        self.finished(name, "failed" if failure is not None else "done", started)
        return result

    def finished(self, name: str, outcome: str, started: float) -> None:
        ms = int((time.monotonic() - started) * 1000)
        self.store.emit("node_finished", node=name, type=self.nodes[name].type, outcome=outcome, ms=ms)

    async def bundle_items(self, name: str, node: NodeSpec) -> Mapping[str, Any]:
        assert node.workflow is not None  # spec.py: a bundle needs workflow
        spec = self.c.spec(node.workflow)
        items = self.items_of(node)
        scope = f"{self.scope}{name}/"
        self.items_run[name] = items
        for item in items:
            view = BundleStore(self.store, workflow=node.workflow, outputs=spec.outputs, item=item)
            await _Run(self.c, node.workflow, item, view, until=None, bundle=True, scope=scope).go()
        return {}

    def items_of(self, node: NodeSpec) -> list[str | None]:
        if node.each is None:
            return [self.item]
        listed = self.store.get(node.each) if self.store.has(node.each) else []
        return [f"{i + 1:02d}" for i in range(len(listed))]

    def forget(self, name: str) -> None:
        """A bundle node sent back: what its items left (their done names, the bundle workflow's outputs,
        feedback and loop counts, a nested bundle's in turn) is cleared, so every item runs again."""
        node = self.nodes[name]
        assert node.workflow is not None
        items = dict.fromkeys([*self.items_run.get(name, []), *self.items_of(node)])
        for item in items:
            self._forget(node.workflow, f"{self.scope}{name}/", item)

    def _forget(self, workflow: str, scope: str, item: str | None) -> None:
        spec = self.c.spec(workflow)
        for n, ns in spec.nodes.items():
            self.store.clear_done(_done_name(scope, n, item))
            self.store.put(_feedback_name(n, item), None)
            for out, o in ns.outputs.items():
                if o.format == "session":
                    self.c.sessions.pop(_sub(out, item))
            if ns.type == "bundle" and ns.workflow is not None:
                listed = self.store.get(ns.each) if ns.each is not None and self.store.has(ns.each) else []
                inner = [f"{i + 1:02d}" for i in range(len(listed))] if ns.each is not None else [item]
                for it in inner:
                    self._forget(ns.workflow, f"{scope}{n}/", it)
        for out in spec.outputs:
            if out.partition(".")[0] not in MEMORY:
                with contextlib.suppress(KeyError):  # a name this level may not write was never written
                    self.store.put(_sub(out, item), None)
        for key in [k for k in self.c.loops if k[0] == f"{scope}{workflow}" and k[1] == item]:
            del self.c.loops[key]

    async def after(self, name: str, result: Mapping[str, Any]) -> None:
        """Route a node that returned: a loop line that fires sends its target and what is downstream back;
        else a failed blocking node blocks; else the node is finished and its forward lines fire or die."""
        node = self.nodes[name]
        failed = FAIL_KEY in result
        self.last = name
        for line in node.to:
            if line.loop is None:
                continue
            if (self.holds(line.when, name) if line.when is not None else failed) and self.again(
                name, line, result
            ):
                again = {line.node} | self.downstream(line.node)
                if name not in again:
                    self.state[name] = "finished"
                    self.fire(name)
                await self.send_back(again)
                return
        if failed and node.on_fail == "block":
            from airlie.story.asking import StoryBlocked  # asking imports config, which imports flow.spec

            raise StoryBlocked(str(result[FAIL_KEY]))
        self.state[name] = "finished"
        self.fire(name)

    def again(self, name: str, line: Line, result: Mapping[str, Any]) -> bool:
        """Count one re-run if the loop's bound allows it, carrying feedback; False once it is spent."""
        assert line.loop is not None
        key = (f"{self.scope}{self.workflow}", self.item, name, line.node)
        if self.c.loops.get(key, 0) >= resolve_count(self.c.cfg, line.loop.max):
            return False
        self.c.loops[key] = self.c.loops.get(key, 0) + 1
        if line.loop.carry is not None and line.loop.carry in result:
            self.store.put(self.feedback_name(line.node), result[line.loop.carry])
        return True

    async def send_back(self, names: set[str]) -> None:
        await self.cancel([t for t, n in self.running.items() if n in names])
        for n in names:
            self.state[n] = "pending"
            self.forced.add(n)
            self.store.clear_done(self.done_name(n))
            for out, spec in self.nodes[n].outputs.items():
                if out.partition(".")[0] in MEMORY:
                    continue
                if spec.format == "session":
                    self.c.sessions.pop(self.name(out))
                else:
                    self.store.put(self.name(out), None)
            for i in range(len(self.nodes[n].to)):
                self.line_state[(n, i)] = None
            if self.nodes[n].type == "bundle":
                self.forget(n)
        self.store.save()


def _sub(name: str, item: str | None) -> str:
    return name.replace("{item}", item) if item is not None else name


def _done_name(scope: str, node: str, item: str | None) -> str:
    """`<node>`, `<node>@<item>` in a bundle item, behind the bundle nodes it runs under (`lay/build@01`)."""
    return f"{scope}{node}" if item is None else f"{scope}{node}@{item}"


def _feedback_name(node: str, item: str | None) -> str:
    return f"feedback.{node}" if item is None else f"feedback.{node}.{item}"
