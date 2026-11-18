"""The generic runner: a dataflow scheduler over a four-kind workflow, one runner per kind.

This is the new engine; `airlie.flow` is retired. It reads the four-kind `WorkflowSpec`
(`airlie.workflow.spec`): a node is WORK, ROUND_ROBIN, SUPERVISOR_WORKERS or K_INDEPENDENT_TEAMS;
wires are edges (`spec.edges`: source -> [WireSpec]) carrying a `when` condition, a `loop` bound and
the artifacts they route. State is an in-memory `RunState` (airlie.flow's disk Store is retired with
the story pipeline); resume and bundles are a later part.

Scheduling (mirrors the old composer, held in memory):
- A forward line (no `loop`) FIRES when its source finishes and its `when` is absent-or-holds, else
  DIES. A node DIES when it has forward lines in and every one died; it emits node_skipped.
- A node is READY when every forward line into it has resolved and at least one fired (a node with none
  is a start node) and every declared input is satisfied. An input is satisfied when present, when it is
  carried loop feedback, or when every upstream producer of it has finished or died.
- A loop line (a `loop`) sends its target and what is downstream back to pending when its source finishes
  (and `when` holds, or it failed) and the loop's `max` is not spent, carrying `loop.carry` as feedback.
- Ready nodes run at once. A failed node with a loop re-runs; one with on_fail=block raises StoryBlocked;
  one with on_fail=continue finishes and its forward lines fire or die. When nothing is running and
  nothing is ready, the run stalls if any node is still pending, else it ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from airlie.config import Sampling
from airlie.engine.protocol import Engine
from airlie.engine.types import GenRequest, Message

from .conditions import Condition
from .conditions import parse as parse_condition
from .spec import (
    KIndependentTeamsNode,
    NodeKind,
    NodeSpec,
    RoundRobinNode,
    SupervisorWorkersNode,
    WireSpec,
    WorkflowSpec,
    WorkNode,
)

FAIL_KEY = "__fail__"
FEEDBACK = "feedback"


class Stalled(RuntimeError):
    """Nothing can run but some node is still pending."""


class EndRun(Exception):
    """A gate ended the run (reserved; gates end a node, not the whole run, in this part)."""


class StoryBlocked(RuntimeError):
    """A failed node with on_fail=block: the run stops here."""


# ---- in-memory run state (replaces airlie.flow.store for one run) ----


class RunState:
    """Values and done-names for one run, held in memory; the events the run emits."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self._done: set[str] = set()
        self.events: list[dict[str, Any]] = []

    def get(self, name: str) -> Any:
        if name not in self.values:
            raise KeyError(name)
        return self.values[name]

    def has(self, name: str) -> bool:
        return self.values.get(name) is not None

    def put(self, name: str, value: Any) -> None:
        self.values[name] = value

    def mark_done(self, name: str) -> None:
        self._done.add(name)

    def done(self, name: str) -> bool:
        return name in self._done

    def clear_done(self, name: str) -> None:
        self._done.discard(name)

    def emit(self, kind: str, **fields: Any) -> None:
        self.events.append({"kind": kind, **fields})


# ---- path lookup over values ----

_NONE = object()


def _field(value: Any, part: str) -> Any:
    """`part` of `value`: a mapping key, a pydantic model field, a dataclass field, else `_NONE`."""
    if part.startswith("_") or value is None:
        return _NONE
    if isinstance(value, Mapping):
        return value.get(part, _NONE)
    if isinstance(value, BaseModel):
        return getattr(value, part) if part in type(value).model_fields else _NONE
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        names = {f.name for f in dataclasses.fields(value)}
        return getattr(value, part) if part in names else _NONE
    return _NONE


def _descend(value: Any, parts: list[str], path: str) -> Any:
    for part in parts:
        value = _field(value, part)
        if value is _NONE:
            raise KeyError(path)
    if value is None:
        raise KeyError(path)
    return value


def resolve_count(cfg: Any, value: int | str) -> int:
    """An int as is; a str is a dotted attribute path on the config."""
    if isinstance(value, int):
        return value
    found: Any = cfg
    for part in value.split("."):
        found = _field(found, part)
        if found is _NONE:
            raise KeyError(f"count '{value}': config has no '{part}'")
    return int(found)


_BRACE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")


def _render(text: str, ctx: RunContext) -> str:
    """Substitute `{name}` with the value of `name` (unresolved placeholders are left alone)."""

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name == "item":
            return ctx.item or ""
        try:
            value = ctx.lookup(name)
        except KeyError:
            return m.group(0)
        return value if isinstance(value, str) else json.dumps(value)

    return _BRACE.sub(repl, text)


def _parse(text: str, fmt: str) -> Any:
    """The value a reply `text` stands for under an output's `format`."""
    if fmt in ("text", "markdown"):
        return text.strip()
    if fmt == "bool":
        return text.strip().strip(".!").lower() in ("yes", "true")
    if fmt == "list":
        try:
            value = json.loads(text.strip())
        except ValueError:
            return None
        return value if isinstance(value, list) else None
    if fmt == "json":
        m = re.search(r"```json\s*(.*?)```", text, re.S)
        for candidate in (m.group(1) if m else None, text):
            if candidate is None:
                continue
            try:
                value = json.loads(candidate)
                if isinstance(value, dict):
                    return value
            except ValueError:
                continue
        return None
    return text


def _gen_request(role: str, text: str) -> GenRequest:
    return GenRequest(
        role=role, messages=[Message(role="user", content=text)], sampling=Sampling(), max_tokens=4096
    )


async def _ask(ctx: RunContext, role: str, text: str) -> str:
    """One reply from `role`; the role may be a `{item}`-substituted seat name."""
    result = await ctx.engine.generate(_gen_request(role, text))
    return result.text


async def _run_shell(cmd: str, ctx: RunContext) -> int:
    """Run a shell command in the run's workdir; its exit code."""
    cwd = str(ctx.workdir) if ctx.workdir else None
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await proc.wait()
    return proc.returncode or 0


async def _gate(node: WorkNode, ctx: RunContext) -> tuple[bool, str]:
    """Whether the node's work is finished, by its gate evidence; (done, why-not)."""
    gate = node.gate
    if gate is None:
        return True, ""
    if gate.type == "script":
        code = await _run_shell(gate.cmd, ctx)
        return code == 0, (f"gate '{gate.cmd}' exited {code}" if code else "")
    if gate.type == "prompt":
        p_yes, _ = await ctx.engine.yes_no("judge", [Message(role="user", content=gate.prompt)])
        return p_yes > 0.5, ("" if p_yes > 0.5 else "the judge says it is not done")
    if gate.type == "steps":
        return True, ""  # a step bound is a stop condition, not a verdict
    return True, ""


@dataclass
class RunContext:
    """What one node's runner sees: the run's config, engine, state and its own inputs."""

    cfg: Any
    engine: Engine
    store: RunState
    workdir: Path
    node: str = ""
    item: str | None = None
    feedback: str | None = None
    inputs: list[str] = field(default_factory=list)
    held: contextlib.AsyncExitStack | None = None
    texts: dict[str, str] = field(default_factory=dict)
    formats: dict[str, str] = field(default_factory=dict)

    def lookup(self, path: str) -> Any:
        """The value of a path: `feedback` is the carried loop feedback; `config.<a>.<b>` on the config;
        anything else on the state (the longest name it holds first). Missing is a KeyError."""
        if path == FEEDBACK:
            if self.feedback is None:
                raise KeyError(path)
            return self.feedback
        parts = path.split(".")
        if any(part.startswith("_") for part in parts):
            raise KeyError(path)
        if parts[0] == "config":
            return _descend(self.cfg, parts[1:], path)
        for cut in range(len(parts), 0, -1):
            head = ".".join(parts[:cut])
            if self.store.has(head):
                try:
                    return _descend(self.store.get(head), parts[cut:], path)
                except KeyError:
                    continue
        raise KeyError(path)


State = Literal["pending", "running", "finished", "dead"]
LineId = tuple[str, int]  # (source node, index in its edges)


class _Run:
    """One run of one workflow over an in-memory RunState."""

    def __init__(
        self,
        spec: WorkflowSpec,
        *,
        cfg: Any,
        engine: Engine,
        store: RunState,
        workdir: Path,
        item: str | None = None,
        texts: dict[str, str] | None = None,
        held: contextlib.AsyncExitStack | None = None,
        until: str | None = None,
    ) -> None:
        self.spec, self.cfg, self.engine, self.store = spec, cfg, engine, store
        self.workdir, self.item, self.texts, self.held, self.until = workdir, item, texts or {}, held, until
        self.nodes = spec.nodes
        self.names = list(spec.nodes)
        self.formats: dict[str, str] = {
            o: s.format for n in self.nodes.values() for o, s in n.outputs.items()
        }
        self.producers: dict[str, set[str]] = {}
        self.into: dict[str, list[LineId]] = {n: [] for n in self.names}
        self.lines: dict[LineId, WireSpec] = {}
        for name, node in self.nodes.items():
            for out in node.outputs:
                self.producers.setdefault(self.name(out), set()).add(name)
            for i, w in enumerate(spec.edges.get(name, [])):
                self.lines[(name, i)] = w
                if w.loop is None and w.target in self.names:
                    self.into[w.target].append((name, i))
        self.state: dict[str, State] = dict.fromkeys(self.names, "pending")
        self.line_state: dict[LineId, Literal["fired", "dead"] | None] = dict.fromkeys(self.lines, None)
        self.forced: set[str] = set()
        self.running: dict[asyncio.Task[Any], str] = {}
        self.last: str | None = None
        self._below: dict[str, set[str]] = {}
        self._conditions: dict[str, Condition] = {}
        self.loops: dict[tuple[str, str | None, str, str], int] = {}

    def name(self, name: str) -> str:
        return name.replace("{item}", self.item) if self.item is not None else name

    def done_name(self, node: str) -> str:
        return node if self.item is None else f"{node}@{self.item}"

    def feedback_name(self, node: str) -> str:
        return f"{FEEDBACK}.{node}" if self.item is None else f"{FEEDBACK}.{node}.{self.item}"

    def downstream(self, name: str) -> set[str]:
        """Every node reached from `name` through forward lines (a cycle returns it)."""
        if name not in self._below:
            seen: set[str] = set()
            todo = [w.target for w in self.spec.edges.get(name, []) if w.loop is None]
            while todo:
                at = todo.pop()
                if at not in seen:
                    seen.add(at)
                    todo.extend(w.target for w in self.spec.edges.get(at, []) if w.loop is None)
            self._below[name] = seen
        return self._below[name]

    def present(self, name: str) -> bool:
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
        # A forward line must be resolved (fired or dead) before its target may run; a loop line is
        # cleared whenever its source re-runs, so it never gates readiness. A node with no forward
        # lines is a start node.
        if any(self.line_state[ln] is None for ln in self.into[name] if self.lines[ln].loop is None):
            return False
        return all(self.satisfied(name, x) for x in self.nodes[name].inputs)

    def ctx(self, name: str) -> RunContext:
        node = self.nodes[name]
        fb_name = self.feedback_name(name)
        return RunContext(
            cfg=self.cfg,
            engine=self.engine,
            store=self.store,
            workdir=self.workdir,
            node=name,
            item=self.item,
            feedback=self.store.get(fb_name) if self.store.has(fb_name) else None,
            inputs=list(node.inputs) + list(self.spec.inputs),
            held=self.held,
            texts=self.texts,
            formats=self.formats,
        )

    def holds(self, when: str | None, source: str) -> bool:
        if when is None:
            return True
        if when not in self._conditions:
            self._conditions[when] = parse_condition(when)
        lookup = self.ctx(source).lookup
        return self._conditions[when].evaluate(lambda path: lookup(self.name(path)))

    def fire(self, source: str) -> None:
        for i, w in enumerate(self.spec.edges.get(source, [])):
            if w.loop is None:
                self.line_state[(source, i)] = "fired" if self.holds(w.when, source) else "dead"

    def settle(self) -> str | None:
        """Kill every node whose forward lines into it all died, until stable; `until` if it dies."""
        changed = True
        while changed:
            changed = False
            for name in self.names:
                if self.state[name] != "pending":
                    continue
                lines = [self.line_state[ln] for ln in self.into[name]]
                if lines and all(s == "dead" for s in lines):
                    self.state[name] = "dead"
                    for i, w in enumerate(self.spec.edges.get(name, [])):
                        if w.loop is None:
                            self.line_state[(name, i)] = "dead"
                    self.store.emit("node_skipped", node=name)
                    changed = True
                    if name == self.until:
                        return name
        return None

    def stalled(self) -> Stalled:
        pending = [n for n in self.names if self.state[n] == "pending"]
        parts = []
        for n in pending:
            lacks = [x for x in self.nodes[n].inputs if not self.satisfied(n, x)]
            lacks += [f"the line from {src}" for src, i in self.into[n] if self.line_state[(src, i)] is None]
            parts.append(f"{n}: {', '.join(lacks) or 'no unsatisfied input'}")
        return Stalled(f"workflow {self.spec.title}: nothing can run; waiting: {'; '.join(parts)}")

    def start(self, name: str) -> None:
        self.state[name] = "running"
        self.store.emit("node_started", node=name)
        self.running[asyncio.create_task(self.step(name))] = name

    async def step(self, name: str) -> dict[str, Any]:
        node = self.nodes[name]
        return await _RUNNERS[node.kind](node, self.ctx(name))

    async def cancel(self, tasks: list[asyncio.Task[Any]]) -> None:
        for task in tasks:
            task.cancel()
            self.running.pop(task, None)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def go(self) -> str:
        stop: str | None = None
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
                order = {t: self.names.index(self.running[t]) for t in finished if self.running.get(t)}
                for task in sorted(finished, key=lambda t: order.get(t, 0)):
                    name = self.running.pop(task, "")
                    if not name or task.cancelled():
                        continue
                    try:
                        result = task.result()
                    except EndRun:
                        self.state[name] = "finished"
                        self.last = stop = name
                        continue
                    await self.after(name, result)
                    if name == self.until and self.state[name] == "finished":
                        stop = name
        except BaseException:
            await self.cancel(list(self.running))
            raise
        if stop is not None:
            return stop
        if any(s == "pending" for s in self.state.values()):
            raise self.stalled()
        return self.last or (self.names[0] if self.names else self.spec.title)

    def again(self, name: str, line: WireSpec, result: dict[str, Any]) -> bool:
        """Count one re-run if the loop bound allows it, carrying its feedback; False once spent."""
        assert line.loop is not None
        key = (self.spec.title, self.item, name, line.target)
        if self.loops.get(key, 0) >= resolve_count(self.cfg, line.loop.max):
            return False
        self.loops[key] = self.loops.get(key, 0) + 1
        if line.loop.carry is not None and line.loop.carry in result:
            self.store.put(self.feedback_name(line.target), result[line.loop.carry])
        return True

    async def send_back(self, names: set[str]) -> None:
        cancel = [t for t, n in list(self.running.items()) if n in names]
        for t in cancel:
            t.cancel()
            self.running.pop(t, None)
        if cancel:
            await asyncio.gather(*cancel, return_exceptions=True)
        for n in names:
            self.state[n] = "pending"
            self.forced.add(n)
            self.store.clear_done(self.done_name(n))
            for out in self.nodes[n].outputs:
                self.store.put(self.name(out), None)
            for i, w in enumerate(self.spec.edges.get(n, [])):
                if w.target in names:
                    self.line_state[(n, i)] = None

    async def after(self, name: str, result: dict[str, Any]) -> None:
        """Route a node that returned: store its outputs; a firing loop sends things back; else finish."""
        node = self.nodes[name]
        failed = FAIL_KEY in result
        self.last = name
        for out in node.outputs:
            self.store.put(self.name(out), result.get(out))
        for w in self.spec.edges.get(name, []):
            if w.loop is None:
                continue
            if (self.holds(w.when, name) if w.when is not None else failed) and self.again(name, w, result):
                again = {w.target} | self.downstream(w.target)
                if name not in again:
                    self.state[name] = "finished"
                    self.fire(name)
                await self.send_back(again)
                return
        if failed and node.on_fail == "block":
            self.store.emit("node_failed", node=name, reason=str(result[FAIL_KEY]))
            raise StoryBlocked(str(result[FAIL_KEY]))
        self.state[name] = "finished"
        self.fire(name)
        self.store.emit("node_finished", node=name, outcome="failed" if failed else "done")


async def _work(node: NodeSpec, ctx: RunContext) -> dict[str, Any]:
    assert isinstance(node, WorkNode)
    try:
        if node.run is not None:
            code = await _run_shell(node.run, ctx)
            done, why = (True, "") if code == 0 else (False, f"{node.run} exited {code}")
            if node.gate is not None:
                gate_done, gate_why = await _gate(node, ctx)
                if not gate_done:
                    done, why = False, gate_why or why
            if not done:
                return {FAIL_KEY: why}
            return {o: None for o in node.outputs}
        role = node.role or "coder"
        text = (
            node.prompt
            if node.prompt is not None
            else ctx.texts.get(node.prompt_id or "", node.prompt_id or "")
        )
        reply = await _ask(ctx, role, _render(text, ctx))
        out = {o: _parse(reply, fmt.format) for o, fmt in node.outputs.items()}
        if node.gate is not None:
            gate_done, gate_why = await _gate(node, ctx)
            if not gate_done:
                out[FAIL_KEY] = gate_why
        return out
    except Exception as e:  # a node's failure is data (FAIL_KEY), not a crash
        return {FAIL_KEY: str(e)}


async def _round_robin(node: NodeSpec, ctx: RunContext) -> dict[str, Any]:
    assert isinstance(node, RoundRobinNode)
    try:
        steps = resolve_count(ctx.cfg, node.steps) if isinstance(node.steps, str) else int(node.steps)
        transcript = _render(node.prompt, ctx)
        last = ""
        cond = parse_condition(node.agreement_when)

        def lookup(path: str) -> Any:
            if path.split(".")[0] in node.outputs:
                return transcript
            return ctx.lookup(path)

        for _ in range(steps):
            for role in node.roles:
                reply = await _ask(ctx, role, transcript)
                transcript = transcript + f"\n{role}: {reply}"
                last = reply
            if cond.evaluate(lookup):
                break
        return {o: _parse(last, fmt.format) for o, fmt in node.outputs.items()}
    except Exception as e:  # a node's failure is data (FAIL_KEY), not a crash
        return {FAIL_KEY: str(e)}


async def _supervisor_workers(node: NodeSpec, ctx: RunContext) -> dict[str, Any]:
    assert isinstance(node, SupervisorWorkersNode)
    try:
        brief = _render(node.prompt, ctx)
        assign = await _ask(ctx, node.supervisor, brief + "\nReply with a JSON list of worker tasks.")
        try:
            parsed = json.loads(assign)
        except ValueError:
            parsed = None
        tasks = [str(t) for t in (parsed if isinstance(parsed, list) else [assign])][: node.workers]
        results = await asyncio.gather(*[_ask(ctx, node.worker, f"{brief}\nYour task: {t}") for t in tasks])
        synthesis = await _ask(ctx, node.supervisor, "Synthesize these results:\n" + "\n".join(results))
        return {o: _parse(synthesis, fmt.format) for o, fmt in node.outputs.items()}
    except Exception as e:  # a node's failure is data (FAIL_KEY), not a crash
        return {FAIL_KEY: str(e)}


async def _k_independent(node: NodeSpec, ctx: RunContext) -> dict[str, Any]:
    assert isinstance(node, KIndependentTeamsNode)
    try:
        brief = _render(node.prompt, ctx)

        async def one_team() -> str:
            transcript = brief
            last = ""
            for role in node.roles:
                reply = await _ask(ctx, role, transcript)
                transcript = transcript + f"\n{role}: {reply}"
                last = reply
            return last

        results = await asyncio.gather(*[one_team() for _ in range(node.teams)])
        winner = results[0]  # test-score / llm aggregation across teams is a later part
        return {o: _parse(winner, fmt.format) for o, fmt in node.outputs.items()}
    except Exception as e:  # a node's failure is data (FAIL_KEY), not a crash
        return {FAIL_KEY: str(e)}


_RUNNERS: dict[NodeKind, Callable[[NodeSpec, RunContext], Awaitable[dict[str, Any]]]] = {
    "WORK": _work,
    "ROUND_ROBIN": _round_robin,
    "SUPERVISOR_WORKERS": _supervisor_workers,
    "K_INDEPENDENT_TEAMS": _k_independent,
}


@dataclass
class WorkflowResult:
    """What a run returns: its declared outputs, the events it emitted, and where it ended."""

    outputs: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    ended: str | None = None


async def run_workflow(
    spec: WorkflowSpec,
    *,
    cfg: Any,
    engine: Engine,
    workdir: Path,
    inputs: dict[str, Any] | None = None,
    item: str | None = None,
    texts: dict[str, str] | None = None,
    until: str | None = None,
) -> WorkflowResult:
    """Run `spec` to completion; returns its declared outputs, the events, and the last node."""
    store = RunState()
    for k, v in (inputs or {}).items():
        store.put(k, v)
    async with contextlib.AsyncExitStack() as held:
        run = _Run(
            spec,
            cfg=cfg,
            engine=engine,
            store=store,
            workdir=workdir,
            item=item,
            texts=texts,
            held=held,
            until=until,
        )
        ended = await run.go()
    outputs: dict[str, Any] = {}
    for o in spec.outputs:
        key = o.replace("{item}", item) if item is not None else o
        if store.has(key):
            outputs[o] = store.get(key)
    return WorkflowResult(outputs=outputs, events=store.events, ended=ended)
