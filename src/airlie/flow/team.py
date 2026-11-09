"""The team node: a supervisor that splits the work, N workers that each do one chunk and hand it back, and
the supervisor assembling the chunks as they finish (nodes-are-config design, "The team node", D9, D22).

The protocol is fixed here; the node only configures it (its `role`'s supervisor and worker, its `prompt`,
`worker` text, `tools`, `steps` per worker and `workers` cap):

1. The supervisor is one held conversation, opened with the node's rendered prompt and the `team-assign`
   rule; it replies with one ```json block `{"assignments": [{"id", "task", "files"}]}`.
2. The first `workers` (default 5) assignments start at once; the rest are not started, and the supervisor
   is told so on its next turn. Each worker's brief is the node's `worker` text rendered with its inputs and
   `{assignment}`: a worker sees only its own chunk. A worker whose tools write is a write turn
   (`write_runner`) on a checkout of its own, detached at the team's base; any other is an ask.
3. As each worker finishes, in finish order and while the others still work, the supervisor gets one turn
   with the `team-handback` rule and the result. A writer's diff is applied to the supervisor's own checkout
   (`git apply --3way`) and committed there; a diff that does not apply is a failed hand-back. There is one
   round of assignments: a failed hand-back is reported, and the node still finishes.
4. The supervisor's reply to the last hand-back is the synthesis, parsed by the node's structured output.

Workers talk only to the supervisor, and only by handing back. The outputs: `results` (each hand-back's
`{id, ok, text}`), the synthesis under its output's name, and the `ref:branch` output naming a branch of
the supervisor's checkout when any worker's change was applied."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from airlie.flow.composer import RunContext
from airlie.flow.outputs import OutputSpec, describe, parse_output
from airlie.flow.runners import (
    Checkout,
    Seat,
    brief,
    emit_done,
    node_failed,
    step_count,
    structured,
    worktree_at,
    write_runner,
)
from airlie.flow.spec import ASSIGNMENT, NodeSpec, TeamRole
from airlie.flow.store import Store
from airlie.prompts import text
from airlie.story.asking import Asker, Recorder, Say, Writes
from airlie.story.model import StoryError

ASSIGNMENTS = OutputSpec(format="json")
"""The supervisor's assignment reply: one ```json block, its shape checked by `assignments`."""
CAP = 5
"""The workers a team runs when its node sets no cap (Cohere 2026-11-08: five was the useful ceiling)."""
TRIES = 3
"""How often the supervisor is asked for a reply that parses (its assignments, its synthesis)."""
NONE = "none"


@dataclass(frozen=True)
class Assignment:
    id: str
    task: str
    files: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "task": self.task, "files": list(self.files)}


@dataclass(frozen=True)
class Handback:
    """What one worker handed back: whether it finished (`ok`), its reply or why it failed (`text`), a
    writer's diff and, once applied, its `git apply --stat`."""

    id: str
    ok: bool
    text: str
    diff: str = ""
    stat: str = ""


def assignments(reply: str) -> list[Assignment]:
    """The assignments a supervisor's reply holds, in its order; `StoryError` naming what is wrong."""
    block = parse_output(reply, ASSIGNMENTS)
    items = block.get("assignments")
    if not isinstance(items, list) or not items:
        raise StoryError("field 'assignments' must be a list of at least one assignment")
    found: list[Assignment] = []
    for n, item in enumerate(items, 1):
        if not isinstance(item, Mapping):
            raise StoryError(f"assignment {n} must be an object with id, task and files")
        id, task, files = item.get("id"), item.get("task"), item.get("files", [])
        if not isinstance(id, str) or not id.strip() or not isinstance(task, str) or not task.strip():
            raise StoryError(f"assignment {n} needs an id and a task, each text")
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            raise StoryError(f"assignment {n}: files must be a list of paths")
        found.append(Assignment(id.strip(), task, tuple(files)))
    ids = [a.id for a in found]
    if len(set(ids)) != len(ids):
        raise StoryError("each assignment needs an id of its own")
    return found


class _Assigned:
    """The node's store with one more name, `assignment`: what a worker's brief and inputs read."""

    def __init__(self, store: Store, assignment: Assignment) -> None:
        self._store = store
        self._assignment = assignment.to_dict()

    def has(self, name: str) -> bool:
        return name == ASSIGNMENT or self._store.has(name)

    def get(self, name: str) -> Any:
        return self._assignment if name == ASSIGNMENT else self._store.get(name)

    def put(self, name: str, value: Any) -> None:
        self._store.put(name, value)

    def save(self) -> None:
        self._store.save()

    def emit(self, kind: str, **fields: Any) -> None:
        self._store.emit(kind, **fields)

    def done(self, name: str) -> bool:
        return self._store.done(name)

    def mark_done(self, name: str) -> None:
        self._store.mark_done(name)

    def clear_done(self, name: str) -> None:
        self._store.clear_done(name)


def _routes(node: NodeSpec, ctx: RunContext) -> tuple[str | None, tuple[str, OutputSpec] | None, str | None]:
    """The node's `results` output, its synthesis (the structured output, else its one other output) and its
    `ref:branch` output; a team node with two outputs a synthesis could fill is refused (`StoryError`)."""
    results = "results" if "results" in node.outputs else None
    branch = next((n for n, s in node.outputs.items() if s.format == "ref:branch"), None)
    synthesis = structured(node)
    if synthesis is None:
        rest = [(n, s) for n, s in node.outputs.items() if n not in (results, branch)]
        if len(rest) > 1:
            raise StoryError(
                f"team node '{ctx.node}': outputs {', '.join(n for n, _ in rest)} could each be the "
                "synthesis; give the synthesis fields, or declare one output beside results and a branch"
            )
        synthesis = rest[0] if rest else None
    return results, synthesis, branch


def _writer(node: NodeSpec, ctx: RunContext, role: str) -> bool:
    """Whether the worker seat writes: the node's `tools`, else the role's own, include a write tool."""
    from airlie.topology.studio import WRITE_TOOLS  # the studio imports the config, which imports flow.spec

    if node.tools is not None:
        listed = node.tools
    else:
        spec = ctx.cfg.roles[role]
        listed = ctx.cfg.tools.builtin if spec.tools is None else spec.tools
    return any(t in WRITE_TOOLS for t in listed)


def _worker_node(node: NodeSpec, role: str, writer: bool) -> NodeSpec:
    """The node one worker runs as: the team's `worker` text as its prompt, its inputs and `assignment`,
    `steps` and `tools`; a writer's outputs are its diff and its account."""
    assert node.worker is not None  # spec.py: a team needs worker
    outputs = {"diff": OutputSpec(format="text"), "account": OutputSpec(format="text")} if writer else {}
    return NodeSpec(
        type="write" if writer else "ask",
        role=role,
        prompt=node.worker,
        inputs=[*node.inputs, ASSIGNMENT],
        outputs=outputs,
        steps=node.steps,
        tools=node.tools,
    )


async def _work(
    node: NodeSpec,
    ctx: RunContext,
    assignment: Assignment,
    *,
    asker: Asker,
    st: Recorder,
    seat: Seat,
    role: str,
    writer: bool,
) -> Handback:
    """One worker's turn on its assignment (`role` as the workflow names it), as its hand-back; whatever it
    raises is a failed one."""
    worker = _worker_node(node, role, writer)
    store = _Assigned(ctx.store, assignment)
    mine = dataclasses.replace(ctx, node=f"{ctx.node}.{assignment.id}", store=store)
    try:
        if writer:
            out = await write_runner(worker, mine, asker=asker, st=st, seat=seat)
            return Handback(assignment.id, True, str(out.get("account", "")), str(out.get("diff", "")))
        at = worktree_at(node, ctx)
        steps = step_count(worker, mine)
        reply = await asker.ask(st, mine.node, seat(role), brief(worker, mine), at=at, steps=steps)
        return Handback(assignment.id, True, reply)
    except Exception as e:
        return Handback(assignment.id, False, f"{type(e).__name__}: {e}")


def _apply(tree: Path, handback: Handback) -> Handback:
    """A writer's hand-back with its diff applied and committed on the supervisor's checkout `tree`; one
    whose diff does not apply comes back failed, with git's reason, and the checkout as it was. Blocking:
    run it in a thread (`asyncio.to_thread`), so the other workers go on meanwhile."""
    from airlie.teams.worktree import commit_wip, reset_to_head, run_git

    if not handback.ok or not handback.diff.strip():
        return handback
    applied = run_git(tree, "apply", "--3way", "--whitespace=nowarn", check=False, stdin=handback.diff)
    if applied.returncode != 0:
        reset_to_head(tree)
        reason = (applied.stderr or applied.stdout).strip()
        return Handback(handback.id, False, f"{handback.text}\n\n{reason}".strip(), handback.diff)
    commit_wip(tree, f"team: {handback.id}")
    stat = run_git(tree, "apply", "--stat", check=False, stdin=handback.diff).stdout.rstrip()
    return dataclasses.replace(handback, stat=stat)


async def _until_parsed[T](
    say: Say, nudge: str | None, again: str, parse: Callable[[str], T], what: str
) -> tuple[str, T]:
    """The supervisor's reply to `nudge` and its parse; one that does not parse goes back with the reason and
    `again` (the rule or the format it missed), `TRIES` times in all."""
    reason = ""
    for _ in range(TRIES):
        reply = await say(nudge)
        try:
            return reply, parse(reply)
        except StoryError as e:
            reason = str(e)
            nudge = f"{reason}\n\n{again}"
    raise StoryError(f"the supervisor did not produce usable {what}: {reason}")


def _result(handback: Handback, writer: bool, cap: int) -> str:
    """What the supervisor is shown of a hand-back: the worker's reply (or why it failed) and, for a change
    that was applied, its `--stat` and its diff cut to `cap` characters (the tools' output cap, as the gate's
    tail is), with how much was cut: the whole change is in the supervisor's checkout."""
    if not (writer and handback.ok and handback.diff.strip()):
        return handback.text
    diff = handback.diff.rstrip()
    shown = diff[:cap] + (f"\n… [truncated {len(diff) - cap} chars]" if len(diff) > cap else "")
    return f"{handback.text}\n\n```\n{handback.stat}\n```\n\n```diff\n{shown}\n```"


async def team_runner(
    node: NodeSpec,
    ctx: RunContext,
    *,
    asker: Asker,
    st: Recorder,
    seat: Seat = str,
) -> Mapping[str, Any]:
    """A supervisor, `workers` workers and the protocol of this module's docstring. The supervisor's stage
    is the node's name, each worker's `<node>.<id>`; a node whose supervisor fails (no usable assignments or
    synthesis, a turn that does not finish) fails by its `on_fail`, the workers' still running cancelled."""
    assert isinstance(node.role, TeamRole) and node.prompt is not None  # spec.py: a team's fields
    supervisor = seat(node.role.supervisor)
    results_name, synthesis, branch_name = _routes(node, ctx)
    worker = node.role.worker
    writer = _writer(node, ctx, seat(worker))
    cap = node.workers or CAP
    checkout = Checkout()
    assign = text("team-assign", workers=cap, format=describe(ASSIGNMENTS))
    finish = describe(synthesis[1]) if synthesis is not None else ""
    opening = f"{brief(node, ctx)}\n\n{assign}"
    tasks: list[asyncio.Task[Handback]] = []
    handed: list[Handback] = []
    value: Any = None
    branch: str | None = None
    try:
        talk = asker.conversation(
            st,
            ctx.node,
            supervisor,
            opening,
            steps=None,
            at=worktree_at(node, ctx),
            writes=Writes(prepare=checkout.prepare),  # no tools: only so its checkout is known
        )
        async with talk as say:
            _, planned = await _until_parsed(say, None, assign, assignments, "assignments")
            if writer and checkout.tree is None:
                raise StoryError(f"team node '{ctx.node}': the supervisor's checkout never opened")
            running, left = planned[:cap], [a.id for a in planned[cap:]]
            tasks = [
                asyncio.create_task(
                    _work(node, ctx, a, asker=asker, st=st, seat=seat, role=worker, writer=writer)
                )
                for a in running
            ]
            waiting = [a.id for a in running]
            reply = ""
            for done in asyncio.as_completed(tasks):
                back = await done
                if writer and checkout.tree is not None:
                    back = await asyncio.to_thread(_apply, checkout.tree, back)
                handed.append(back)
                waiting.remove(back.id)
                status = ("applied" if writer and back.diff.strip() else "answered") if back.ok else "failed"
                nudge = text(
                    "team-handback",
                    id=back.id,
                    status=status,
                    waiting=", ".join(waiting) or NONE,
                    not_started=", ".join(left) or NONE,
                    format=finish,
                    result=_result(back, writer, ctx.cfg.tools.output_cap_chars),
                )
                left = []  # named once, on the first hand-back
                if waiting or synthesis is None:
                    reply = await say(nudge)
                else:
                    parse = functools.partial(parse_output, spec=synthesis[1])
                    reply, value = await _until_parsed(say, nudge, finish, parse, synthesis[0])
            if writer and checkout.tree is not None and any(h.ok and h.diff.strip() for h in handed):
                branch = await asyncio.to_thread(_branch, checkout.tree, ctx)
    except Exception as e:
        return node_failed(node, ctx, st, e)
    finally:
        for task in tasks:
            task.cancel()  # a supervisor that failed leaves no worker running
        await asyncio.gather(*tasks, return_exceptions=True)
    emit_done(node, st, reply)
    out: dict[str, Any] = {}
    if results_name is not None:
        out[results_name] = [{"id": h.id, "ok": h.ok, "text": h.text} for h in handed]
    if synthesis is not None and value is not None:
        out[synthesis[0]] = value
    if branch_name is not None and branch is not None:
        out[branch_name] = branch
    return out


def _branch(tree: Path, ctx: RunContext) -> str:
    """A branch at the supervisor's checkout's HEAD (it outlives the checkout): the assembled work."""
    from airlie.teams.worktree import head_sha, run_git

    head = head_sha(tree)
    where = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{ctx.node}-{ctx.item}" if ctx.item else ctx.node)
    name = f"airlie/team/{where}-{head[:10]}"
    run_git(tree, "branch", "-f", name, head)
    return name
