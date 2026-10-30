"""NativeTeam: a ReAct loop over the Engine seam. Reference implementation and fallback for the AutoGen team.

Each turn resends the whole conversation (the engine's prefix cache makes that cheap); the tool call is the
action u_t, its output the observation o_t. Entering a tool call releases the generation slot (spec 04 §2).

Every nudge the loop sends is a steering file in the prompt hub (`airlie/prompts/steering/<id>.md`, its `when`
and `why` there), sent through `_steer`, which makes each one a `steer` event; the goal's closing sentences
are the rules `goal-tools`, `goal-read-tools` and `goal-no-tools`."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import re
import time
from dataclasses import asdict, replace
from pathlib import PurePosixPath
from typing import Any

from airlie.config import RoleSpec
from airlie.config.schema import MUTATING_TOOLS
from airlie.engine.protocol import Engine
from airlie.engine.types import (
    ContentPart,
    EngineError,
    GenRequest,
    GenResult,
    Message,
    ToolCall,
    image_part,
)
from airlie.prompts import entry, text
from airlie.skills import prompt_text
from airlie.supervisor.advice import WRONG_TEST, with_advice
from airlie.teams.call_repair import Repair, repair_call
from airlie.teams.protocol import (
    StepObserver,
    StepRecord,
    StopReason,
    StuckAdvisor,
    TeamContext,
    TeamResult,
    cut_off_text,
)
from airlie.tools.jsonargs import history_arguments
from airlie.tools.protocol import ToolResult


def _goal_message(
    goal: str,
    worktree_hint: str,
    team_id: str = "",
    preamble: str | None = None,
    *,
    tools: bool = True,
    writes: bool = True,
) -> str:
    who = f"You are {team_id}. " if team_id else ""
    head = f"{preamble.rstrip()}\n\n" if preamble else ""
    # the rules goal-tools, goal-read-tools and goal-no-tools: told to use tools it does not have, a model
    # invents them (2026-10-16: Read, list_files); told to change files it cannot, it argues with the brief
    # till its thinking is cut (2026-10-29, the architect)
    if not tools:
        rule = text("goal-no-tools")
    else:
        rule = text("goal-tools" if writes else "goal-read-tools", worktree_hint=worktree_hint)
    return f"{head}Goal:\n{goal}\n\n{who}{rule}"


def dump_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        d: dict[str, Any] = {
            "role": m.role,
            "content": m.content,
            "name": m.name,
            "tool_call_id": m.tool_call_id,
        }
        d["tool_calls"] = [asdict(c) for c in m.tool_calls] if m.tool_calls else None
        d["parts"] = [asdict(p) for p in m.parts] if m.parts else None
        out.append(d)
    return out


def load_messages(dumped: list[dict[str, Any]]) -> list[Message]:
    out: list[Message] = []
    for d in dumped:
        calls = tuple(ToolCall(**c) for c in d["tool_calls"]) if d.get("tool_calls") else None
        parts = tuple(ContentPart(**p) for p in d["parts"]) if d.get("parts") else None
        out.append(
            Message(
                role=d["role"],
                content=d["content"],
                name=d.get("name"),
                tool_calls=calls,
                tool_call_id=d.get("tool_call_id"),
                parts=parts,
            )
        )
    return out


UNPARSED_CALL = re.compile(r"<tool_call>|<function=")
"""A tool call the server left in the text: the model wrote one the parser could not read (HDS-5, 2026-10-16:
`<function=read_file>` with a `<file>` tag). It is not an answer, however the reply reads."""
MALFORMED_TEXT_CAP = 4000
"""Characters of a malformed reply kept in its `malformed_call` event."""

_TEST_DIRS = frozenset({"test", "tests"})
_TEST_NAME = re.compile(r"^(test_.+|.+_test\..+|.+\.(test|spec)\..+|conftest\.py)$")

WRITE_TOOLS_ONLY = frozenset({"write_file", "edit_file", "run_tests"})
"""The tools a role keeps once its reading is switched off."""
READS_UNDER_HOLD = 3
"""Reads a role may still make after a missed write deadline switches its reading off, each answered with how
many remain (read-left), before reads are refused (read-refused): a role that needs one fact gets it, a role
that reads forever still hits the wall (AIR-31 layer 02, 2026-10-19: two runs spent their last 25 steps on
refused greps, never wrote). The hold a repeat earns (repeat-write-only) grants none: the role was repeating
the read it would get."""
ENGINE_RETRIES = 1
"""How many times one step's generation is asked again after an EngineError, before the run stops. AIR-31
layer 02 (2026-10-19): vllm-mlx sent one chunk and then nothing for its 600 s inactivity guard; the run ended
as engine_error with the conversation lost, when the same request a moment later would have gone through."""
ENGINE_RETRY_S = 5.0
"""The pause before a retried generation: the engine's guard has just cancelled the request on its side."""
WRITE_ONLY_AFTER = 4
"""Steps after the write-now nudge by which a role that still has not written loses its read tools."""
RED_LIMIT = 6
"""Failed run_tests runs in a row after which a think_when_red role stops (red_limit) and its layer goes back
to the architect: two red streaks, the advisor asked after each (R1 of the performance plan, 2026-10-29: a
failed build spent ~31 minutes red before its layer was re-planned, and the re-plan is what fixed it)."""
RED_STREAK = 3
"""Failed run_tests runs in a row after which a think_when_red role's advisor (the supervisor) is asked for a
diagnosis, added to that red run's output; again after each further RED_STREAK (roman, 2026-10-28: 35
minutes red on one test, every edit different, so the repeat guard never fired and nobody looked)."""

MAX_CYCLE = 3
"""The longest cycle of calls the repeat guard looks for: a cycle of up to this many calls, gone round
repeat_limit times with the same results, is a repeat as one call repeated is."""
_VOLATILE = (
    (re.compile(r"^[.FEsxXp]+\s*(\[\s*\d+%\])?[ \t]*$", re.MULTILINE), ""),  # pytest's progress dots
    (re.compile(r"\b\d+(\.\d+)?s\b"), "#s"),  # a duration: "in 0.69s"
    (re.compile(r"pytest-\d+"), "pytest-#"),  # pytest's numbered tmp_path root
    (re.compile(r"\bgw\d+\b"), "gw#"),  # an xdist worker
    (re.compile(r"\b0x[0-9a-f]+\b"), "0x#"),  # an object's address in a repr
)
"""What changes between two runs of the same command with nothing else changed, blanked before observations
are compared for a repeat (AIR-9 layer 02, 2026-10-21: ten run_tests in a row with one failure each time
differed only in timings, tmp paths and xdist's dot order, so none was ever the same)."""


def _settled(output: str) -> str:
    """`output` with what differs between identical runs blanked (_VOLATILE), for the repeat guard."""
    for pattern, blank in _VOLATILE:
        output = pattern.sub(blank, output)
    return output


def _loop(recent: list[tuple[str, ...]], limit: int) -> tuple[str, ...] | None:
    """The calls `recent` (oldest first) ends going round `limit` times: one call repeated, or a cycle of up
    to MAX_CYCLE calls, each round with the same results. Their tool names, in order; None for no loop."""
    for p in range(1, MAX_CYCLE + 1):
        tail = recent[-p * limit :]
        if len(tail) == p * limit and tail == tail[-p:] * limit:
            return tuple(r[0] for r in tail[-p:])
    return None


def _carried_tail(role: RoleSpec, thinking: bool, res: GenResult, call: ToolCall) -> str:
    """The tail of a turn's reasoning to carry to the next: the turn thought, its completion reached the
    role's thinking_budget (vllm-mlx closes the think block there), and its action was a read rather than a
    write or run_tests; at most carried_reasoning_chars characters. "" for none."""
    budget, cap = role.thinking_budget, role.carried_reasoning_chars
    reasoning = res.reasoning.strip()
    if not (thinking and budget and cap and reasoning) or call.name in WRITE_TOOLS_ONLY:
        return ""
    return reasoning[-cap:] if res.completion_tokens >= budget else ""


_STALE_BUDGET = tuple(
    re.compile("^" + re.escape(t).replace(re.escape("{n}"), r"\d+") + "$", re.DOTALL)
    for t in (entry(i).body for i in ("last-step", "one-step-left", "last-turn-retry", "no-tools"))
)
"""The end-of-budget messages, from the hub's text, as a resumed run finds them in the last run's history."""


def _without_stale_budget(messages: list[Message]) -> tuple[list[Message], bool]:
    """`messages` without the user messages that told the last run its steps or tools were gone, and whether
    any were dropped: a resumed run has a fresh budget, and a history that says otherwise is obeyed."""
    kept = [
        m
        for m in messages
        if not (
            m.role == "user" and isinstance(m.content, str) and any(p.match(m.content) for p in _STALE_BUDGET)
        )
    ]
    return kept, len(kept) != len(messages)


def is_test_path(path: str) -> bool:
    """Whether a written file is a test: under a test/ or tests/ directory, or named like one (test_x.py,
    x_test.py, x.test.ts, x.spec.js, conftest.py)."""
    parts = PurePosixPath(path.replace("\\", "/")).parts
    return any(p in _TEST_DIRS for p in parts[:-1]) or bool(parts and _TEST_NAME.match(parts[-1]))


def _read_key(call: ToolCall) -> str | None:
    """What a read_file call reads: its path and range, so the same read can be told from another; None for
    another tool or unreadable arguments."""
    if call.name != "read_file":
        return None
    try:
        args = json.loads(call.arguments)
    except ValueError:
        return None
    if not isinstance(args, dict) or not isinstance(args.get("path"), str):
        return None
    shown = {**args, "path": args["path"].strip().removeprefix("./")}
    return json.dumps(shown, sort_keys=True)


def _read_path(call: ToolCall) -> str | None:
    """The path a read_file call names, as a write names it (no leading "./"), or None."""
    if call.name != "read_file":
        return None
    try:
        args = json.loads(call.arguments)
    except ValueError:
        return None
    path = args.get("path") if isinstance(args, dict) else None
    return path.strip().removeprefix("./") if isinstance(path, str) else None


async def _red_advised(
    advisor: StuckAdvisor, ctx: TeamContext, told: str, messages: list[Message]
) -> tuple[str, bool]:
    """`told` with the advisor's red-streak section after it, and whether it was asked (so is owed an
    outcome). An advisor that fails leaves the fixed text."""
    try:
        advice = await advisor.red(ctx.team_id, ctx.role_name, told, list(messages))
    except Exception:  # noqa: BLE001 - any advisor failure means the fixed text, never a stopped run
        return told, False
    return with_advice(told, advice), True


def _settled_red(advisor: StuckAdvisor | None, team_id: str, helped: bool) -> None:
    """Tell the advisor what came of its red-streak advice; a failure there is its own."""
    if advisor is not None:
        with contextlib.suppress(Exception):
            advisor.red_settled(team_id, helped)


def _written_path(call: ToolCall) -> str | None:
    """The path a write_file or edit_file call names, or None for another tool or unreadable arguments."""
    if call.name not in ("write_file", "edit_file"):
        return None
    try:
        args = json.loads(call.arguments)
    except ValueError:
        return None
    path = args.get("path") if isinstance(args, dict) else None
    return path if isinstance(path, str) else None


def _offered(ctx: TeamContext) -> list[Any]:
    """The tool schemas a turn offers: all of them, every turn. The list is rendered at the top of the prompt,
    so a turn that offered fewer would be a prompt the engine's prefix cache has never seen (AIR-28: one such
    turn re-prefilled 56,717 tokens). Write-only is enforced where a call runs (read-refused), not here."""
    return list(ctx.tools.schemas())


def _steer(ctx: TeamContext, step: int, id: str, /, **fields: object) -> str:
    """The hub's steering text `id` with `fields` filled in, and a `steer` event saying it was sent: its id,
    the team, the role and the step it was sent after (for a reply that took no step, the step that reply
    was for, as in `malformed_call`; 0 before the first). Every steering text the loop sends goes through
    here, so the events count how often each fires (spec 2026-10-22-prompt-hub, "Steering events")."""
    said = text(id, **fields)
    if ctx.emit is not None:
        ctx.emit("steer", id=id, team_id=ctx.team_id, role=ctx.role_name, step=step)
    return said


def _log_malformed(ctx: TeamContext, step: int, text: str, repair: Repair | None) -> None:
    """A `malformed_call` event: the reply as the role wrote it (capped), and what the repair made of it."""
    if ctx.emit is None:
        return
    ctx.emit(
        "malformed_call",
        team_id=ctx.team_id,
        role=ctx.role_name,
        step=step,
        text=text[:MALFORMED_TEXT_CAP],
        chars=len(text),
        repair_tried=repair is not None,
        repaired=repair is not None and repair.call is not None,
        repair_note=repair.note if repair is not None else "last turn: no tools offered",
    )


async def _advised(
    advisor: StuckAdvisor, ctx: TeamContext, told: str, messages: list[Message]
) -> tuple[str, bool]:
    """`told` with the advisor's section after it, and whether the advisor was asked (so is owed an outcome).
    An advisor that fails leaves the fixed text: the repeat guard never waits on it or stops for it."""
    try:
        advice = await advisor.stuck(ctx.team_id, ctx.role_name, told, list(messages))
    except Exception:  # noqa: BLE001 - any advisor failure means the fixed nudge, never a stopped run
        return told, False
    return with_advice(told, advice), True


def _settled_stuck(advisor: StuckAdvisor | None, team_id: str, helped: bool) -> None:
    """Tell the advisor what came of its nudge; a failure there is its own, and does not touch the run."""
    if advisor is not None:
        with contextlib.suppress(Exception):
            advisor.stuck_settled(team_id, helped)


class NativeTeam:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    async def _generate(self, ctx: TeamContext, req: GenRequest, steps: int) -> GenResult:
        """One step's generation, asked again up to ENGINE_RETRIES times after an EngineError (a stalled or
        dropped request) with the conversation intact, after ENGINE_RETRY_S; the last error is raised."""
        for attempt in range(ENGINE_RETRIES + 1):
            try:
                async with ctx.scheduler.generation(ctx.team_id):
                    return await self.engine.generate(req)
            except EngineError as e:
                if attempt == ENGINE_RETRIES:
                    raise
                if ctx.emit is not None:
                    ctx.emit(
                        "engine_retry",
                        team_id=ctx.team_id,
                        role=ctx.role_name,
                        step=steps + 1,
                        attempt=attempt + 1,
                        error=str(e)[:300],
                    )
                await asyncio.sleep(ENGINE_RETRY_S)
        raise AssertionError("unreachable: the last attempt raises or returns")

    async def run(self, ctx: TeamContext, observer: StepObserver) -> TeamResult:
        t0 = time.monotonic()
        system = prompt_text(ctx.role.prompt)
        toolless = not ctx.tools.names()
        writes = bool(MUTATING_TOOLS & set(ctx.tools.names()))
        goal_msg = _goal_message(
            ctx.goal, ctx.worktree.name, ctx.team_id, ctx.preamble, tools=not toolless, writes=writes
        )
        messages: list[Message]
        if ctx.initial_state is not None:
            messages, stale = _without_stale_budget(load_messages(ctx.initial_state["messages"]))
            first = ctx.nudge or goal_msg
            if stale:  # sent before any step: step 0
                first = f"{first}\n\n{_steer(ctx, 0, 'fresh-run', n=ctx.max_steps)}"
            messages.append(Message(role="user", content=first))
        else:
            parts = None
            if ctx.images:
                parts = (ContentPart("text", text=goal_msg), *[image_part(p) for p in ctx.images])
            messages = [
                Message(role="system", content=system),
                Message(role="user", content=goal_msg, parts=parts),
            ]
        if ctx.on_prompt is not None:
            sent = dump_messages(messages[-1:] if ctx.initial_state is not None else messages)
            for m in sent:  # a picture is a data URL of the whole file: the trace says how many, not what
                if m["parts"]:
                    m["parts"] = [
                        {**p, "url": f"<image, {len(p['url'])} chars>"} if p["type"] == "image_url" else p
                        for p in m["parts"]
                    ]
            ctx.on_prompt(sent)
        last_test_output = ""
        forked = False
        prompt_tokens = completion_tokens = 0
        max_prompt = 0
        steps = 0
        # (tool, observation), plus the arguments for a write: the same answer thrice is a repeat whatever
        # was asked, except that a write answers "edited x" whatever it wrote (AIR-7 layer 02: three edits)
        recent: list[tuple[str, ...]] = []
        final_text = ""
        stop: StopReason = "done"
        ok = False
        corrected = False  # the current streak of unparsed tool calls has had its one correction
        last_corrected = False  # the tool-less last turn has had its one correction
        can_write = bool({"write_file", "edit_file"} & set(ctx.tools.names()))
        wrote = bool(ctx.already_written)  # a write_file or edit_file has run (here, or in an earlier run)
        write_by = max(4, ctx.max_steps * 2 // 5)  # the step by which a role that can write has started to
        reading_off = False  # the role never wrote after the nudge: it is offered write tools only
        reads_left = 0  # reads still served under that hold (READS_UNDER_HOLD when it starts)
        looped = False  # a writer repeated after the repeat nudge: only write tools till it writes
        context_warned = False  # whether we've already warned about context at 75% for this run
        # a write schedule (the coder): a test write by one deadline, a code write by a later one; each is
        # a nudge at the deadline, then write-only tools WRITE_ONLY_AFTER steps later, until that write lands
        schedule = ctx.role.write_schedule if can_write else None
        # a continuation (a fix round, a follow-up) has the earlier run's tests and code; what it owes is a
        # change of its own, by the earlier deadline, and any write this round is that change (AIR-7 layer 02
        # attempt 5, 2026-10-18: a fix round probed for 28 steps and changed nothing, its deadlines both met)
        continuing = bool(ctx.already_written)
        deadlines: dict[str, int] = {}
        if schedule is not None and continuing:
            deadlines = {"code": max(2, math.ceil(ctx.max_steps * schedule.tests))}
        elif schedule is not None:
            deadlines = {
                "tests": max(2, math.ceil(ctx.max_steps * schedule.tests)),
                "code": max(2, math.ceil(ctx.max_steps * schedule.code)),
            }
        written = {  # a test file, a code file: here, or in the run this one continues (its change: here)
            "tests": any(is_test_path(p) for p in ctx.already_written),
            "code": False if continuing else any(not is_test_path(p) for p in ctx.already_written),
        }
        missed: set[str] = set()  # deadlines passed without their write
        escalated: set[str] = set()  # missed deadlines whose grace ran out too: reading is off for them
        repeat_warned = False  # the first repeated action has been said to the role
        advised = False  # on_stuck was asked about that nudge, and has not yet heard what came of it
        red_warned = False  # the role has been told once that it answered with run_tests failing
        act_warned = False  # the role has been told once that its text-only reply changed nothing
        changed = False  # a write_file or edit_file of this run has landed (a failed one has not)
        last_tests_ok: bool | None = None  # the result of the last run_tests call (None: none yet)
        red_runs = 0  # failed run_tests runs in a row (think_when_red: a streak asks the advisor)
        test_wrong = False  # the advisor diagnosed the red streak as a wrong locked test
        red_out = False  # RED_LIMIT failed runs in a row: the layer goes back to the architect
        red_advised = False  # the advisor was asked about a red streak and has not heard what came of it
        wrote_paths: set[str] = set()  # the files this run wrote or edited: not read again while red
        reads_seen: dict[str, tuple[str, int]] = {}  # a read's key -> (its path, the step that read it)
        has_run_tests = "run_tests" in ctx.tools.names()
        last_turn = False  # max_steps reached: one more turn, with no tools, for the final answer
        while True:
            step_num = steps + 1
            injected = await observer.before_step(ctx.team_id, step_num)
            if injected:
                messages.append(Message(role="user", content=injected))
            req = GenRequest(
                role=ctx.role_name,
                messages=list(messages),
                sampling=ctx.role.sampling,
                max_tokens=ctx.role.max_tokens,
                stop=list(ctx.role.stop),
                tools=None if last_turn or toolless else _offered(ctx),
                # the tool-less last turn is cheap by default; its one retry, after a tool call, thinks
                thinking=(
                    ctx.role.thinking and (ctx.role.last_turn_thinking or not last_turn or last_corrected)
                )
                # a fast role thinks once red, until a run passes (think_when_red)
                or (ctx.role.think_when_red and last_tests_ok is False and not last_turn),
                draft_budget=ctx.draft_budget(),
            )
            g0 = time.monotonic()
            try:
                res = await self._generate(ctx, req, steps)
            except EngineError as e:
                final_text = f"engine error: {e}"
                stop = "engine_error"
                break
            total_ms = (time.monotonic() - g0) * 1000.0
            prefill_ms = res.ttft_ms  # the engine's time to first token: prompt processing, not generation
            gen_ms = max(0.0, total_ms - res.ttft_ms)  # generation only (a slow client can outlast the ttft)
            prompt_tokens += res.prompt_tokens
            max_prompt = max(max_prompt, res.prompt_tokens)
            completion_tokens += res.completion_tokens
            calls = res.tool_calls[:1]  # one action per step: later calls in a "plan" are often argument-less
            content = res.text
            if not calls and ctx.emit is not None:  # a `step` is a call that ran a tool: this one ran none
                ctx.emit(
                    "reply",
                    team_id=ctx.team_id,
                    role=ctx.role_name,
                    model=ctx.role.model,
                    prompt_tokens=res.prompt_tokens,
                    completion_tokens=res.completion_tokens,
                    reasoning_chars=len(res.reasoning),
                    answer_chars=len(res.text),
                    prefill_ms=round(prefill_ms, 1),
                    gen_ms=round(gen_ms, 1),
                )
            if toolless and res.finish_reason != "length" and (calls or UNPARSED_CALL.search(res.text)):
                if corrected:
                    final_text = "stopped: two replies in a row called tools, and this role has none"
                    stop = "no_tools"
                    break
                corrected = True
                messages.append(Message(role="assistant", content=res.text))
                messages.append(Message(role="user", content=_steer(ctx, step_num, "no-tools")))
                continue
            if not calls:
                # a reply that max_tokens ended is not a finished answer, whatever text it holds
                if res.finish_reason == "length":
                    thinking = bool(res.reasoning.strip())
                    final_text = res.text
                    if not final_text.strip():
                        final_text = cut_off_text(ctx.role_name, ctx.role, thinking=thinking)
                    stop = "cut_off"
                    break
                if not UNPARSED_CALL.search(res.text):
                    if ctx.must_act and can_write and not changed and not act_warned and not last_turn:
                        act_warned = True
                        if ctx.emit is not None:
                            ctx.emit(
                                "text_before_action",
                                team_id=ctx.team_id,
                                role=ctx.role_name,
                                chars=len(res.text),
                            )
                        messages.append(Message(role="assistant", content=res.text))
                        messages.append(Message(role="user", content=_steer(ctx, step_num, "act-first")))
                        continue
                    if (
                        ctx.role.green_before_done
                        and has_run_tests
                        and last_tests_ok is False
                        and not red_warned
                        and not last_turn
                    ):
                        red_warned = True
                        messages.append(Message(role="assistant", content=res.text))
                        messages.append(Message(role="user", content=_steer(ctx, step_num, "still-red")))
                        continue
                    final_text = res.text
                    ok = True
                    stop = "done"
                    break
                # on the last turn no tools were offered, so there is nothing to repair it into
                repair = None if last_turn else repair_call(res.text, ctx.tools.schemas())
                _log_malformed(ctx, step_num, res.text, repair)
                if repair is not None and repair.call is not None:
                    calls, content = [repair.call], repair.prose  # runs below as an ordinary step
                elif last_turn:  # it tried a tool it was not offered: told once, then there is no answer
                    if not last_corrected:
                        last_corrected = True
                        messages.append(Message(role="assistant", content=res.text))
                        retry = _steer(ctx, step_num, "last-turn-retry")
                        messages.append(Message(role="user", content=retry))
                        continue
                    final_text = f"stopped: reached max_steps={ctx.max_steps}"
                    stop = "max_steps"
                    break
                elif corrected:
                    final_text = "stopped: two replies in a row held a tool call that could not be parsed"
                    stop = "malformed_call"
                    break
                else:
                    corrected = True
                    messages.append(Message(role="assistant", content=res.text))
                    messages.append(Message(role="user", content=_steer(ctx, step_num, "malformed-call")))
                    continue
            corrected = False
            if last_turn:  # it called a tool it was not offered: told once, then there is no answer
                if ctx.emit is not None:
                    ctx.emit(
                        "last_turn_tool_call",
                        team_id=ctx.team_id,
                        role=ctx.role_name,
                        tool=calls[0].name,
                        ends_run=last_corrected,
                    )
                if not last_corrected:
                    last_corrected = True
                    messages.append(Message(role="assistant", content=res.text))
                    retry = _steer(ctx, step_num, "last-turn-retry")
                    messages.append(Message(role="user", content=retry))
                    continue
                final_text = f"stopped: reached max_steps={ctx.max_steps}"
                stop = "max_steps"
                break
            replay = tuple(replace(c, arguments=history_arguments(c.arguments)) for c in calls)
            messages.append(Message(role="assistant", content=content, tool_calls=replay))
            for call in calls:
                steps += 1
                # while red, a think_when_red role does not read back a file it wrote: its writes are the
                # file (roman, 2026-10-28). Other reads and a shell probe still run: refusing all of them
                # left it unable to learn which input a looped test failed on, for 35 minutes
                reread = (
                    ctx.role.think_when_red and last_tests_ok is False and _read_path(call) in wrote_paths
                )
                held = ((reading_off or looped) and call.name not in WRITE_TOOLS_ONLY) or reread
                key = _read_key(call)
                seen_at = reads_seen.get(key) if key is not None else None
                if seen_at is not None and not held:
                    # the same read again, nothing written to it since: its text is in the conversation
                    # already (roman A/B run 2, 2026-10-29: the architect read one test file six times in a
                    # row, each step thinking to its budget, until the repeat guard stopped the drafting)
                    result = ToolResult(ok=True, output=_steer(ctx, steps, "already-read", step=seen_at[1]))
                    tool_ms = 0.0
                    if ctx.emit is not None:
                        ctx.emit("read_repeated", team_id=ctx.team_id, role=ctx.role_name, step=steps)
                elif held and (looped or reread or reads_left == 0):
                    refused = "red-read-refused" if reread else "read-refused"
                    result = ToolResult(ok=False, output=_steer(ctx, steps, refused))
                    tool_ms = 0.0
                    if ctx.emit is not None:
                        ctx.emit(
                            "read_refused",
                            team_id=ctx.team_id,
                            role=ctx.role_name,
                            step=steps,
                            tool=call.name,
                        )
                else:
                    async with ctx.scheduler.tool_stall(ctx.team_id):
                        t1 = time.monotonic()
                        result = await ctx.tools.call(call)
                        tool_ms = (time.monotonic() - t1) * 1000.0
                    if key is not None and result.ok:
                        reads_seen[key] = (_read_path(call) or "", steps)
                    if held:  # one of the few reads the hold still serves, and it says how many are left
                        reads_left -= 1
                        left = _steer(ctx, steps, "read-left", k=reads_left)
                        result = replace(result, output=f"{result.output}\n\n{left}")
                    if ctx.role.think_when_red and call.name == "run_tests":
                        if result.ok:
                            red_runs = 0
                            if red_advised:  # green after the advice
                                _settled_red(ctx.on_stuck, ctx.team_id, True)
                                red_advised = False
                        else:
                            red_runs += 1
                            red_out = red_runs >= RED_LIMIT  # R1: the architect's turn, not more red
                            fix = _steer(ctx, steps, "red-fix")  # what to do next, exactly: one edit
                            if ctx.on_stuck is not None and red_runs % RED_STREAK == 0:
                                if red_advised:  # still red a whole streak after the last advice
                                    _settled_red(ctx.on_stuck, ctx.team_id, False)
                                shown = [*messages, Message(role="tool", content=result.output)]
                                fix, red_advised = await _red_advised(ctx.on_stuck, ctx, fix, shown)
                                # a test it may not change is wrong: no code fixes that, so it stops here
                                # and the layer goes back to the architect (roman, 2026-10-28: 31 minutes
                                # of fix round against 'MMMDCCCLXXXIX' for 3999)
                                test_wrong = red_advised and ctx.on_stuck.last_category == WRONG_TEST
                            result = replace(result, output=f"{result.output}\n\n{fix}")
                messages.append(
                    Message(role="tool", content=result.output, tool_call_id=call.id, name=call.name)
                )
                tail = _carried_tail(ctx.role, bool(req.thinking), res, call)
                if tail:  # after the observation, before any nudge; the earlier carries stay: removing one
                    # mid-history misses the engine's prefix cache and re-prefills the whole conversation
                    carried = _steer(ctx, steps, "reasoning-carried", tail=tail)
                    messages.append(Message(role="user", content=carried))
                    if ctx.emit is not None:
                        ctx.emit(
                            "reasoning_carried",
                            team_id=ctx.team_id,
                            role=ctx.role_name,
                            step=steps,
                            chars=len(tail),
                        )
                step_record = StepRecord(
                    team_id=ctx.team_id,
                    step_num=steps,
                    u_t=call,
                    o_t=result.output,
                    ok=result.ok,
                    assistant_text=res.text,
                    reasoning=res.reasoning,
                    attempt=ctx.attempt,
                    prompt_tokens=res.prompt_tokens,
                    completion_tokens=res.completion_tokens,
                    gen_ms=gen_ms,
                    prefill_ms=prefill_ms,
                    tool_ms=tool_ms,
                    accepted_draft_tokens=res.accepted_draft_tokens,
                    draft_tokens=res.draft_tokens,
                    model=ctx.role.model,
                    role=ctx.role_name,
                )
                await observer.after_step(step_record)

                # Check context limits after each step
                if ctx.role.max_context > 0 and not context_warned:
                    # Check if we've crossed the 75% threshold
                    context_threshold = ctx.role.max_context * 0.75
                    if max_prompt >= context_threshold:
                        # Only warn once per run
                        context_warned = True
                        # Add the context nudge message (steering context-pressure)
                        pressure = _steer(
                            ctx,
                            steps,
                            "context-pressure",
                            max_prompt=max_prompt,
                            max_context=ctx.role.max_context,
                        )
                        messages.append(Message(role="user", content=pressure))
                        # Emit context event
                        if ctx.emit is not None:
                            ctx.emit(
                                "context",
                                team_id=ctx.team_id,
                                role=ctx.role_name,
                                step=steps,
                                prompt_tokens=max_prompt,
                                max_context=ctx.role.max_context,
                            )

                # Check if we've reached the maximum context limit
                if ctx.role.max_context > 0 and max_prompt >= ctx.role.max_context:
                    if ctx.role.on_context_full == "stop":
                        # Stop the run with context_full reason
                        final_text = f"stopped: context full at {ctx.role.max_context} tokens"
                        stop = "context_full"
                        break
                    elif ctx.role.on_context_full == "trim":
                        # Trim the oldest tool observations to make room
                        if ctx.emit is not None:
                            # Find all tool messages and remove the oldest ones until we're under limit
                            # We need to identify which messages are tool messages (role="tool")
                            tool_indices = [i for i, msg in enumerate(messages) if msg.role == "tool"]
                            if tool_indices:
                                # Count how many we need to drop
                                chars_dropped = 0
                                dropped_count = 0

                                # Remove oldest tool messages until we're under the limit or no more to remove
                                while tool_indices and max_prompt >= ctx.role.max_context:
                                    # Remove the oldest tool message
                                    oldest_idx = tool_indices.pop(0)
                                    msg = messages[oldest_idx]
                                    chars_dropped += len(msg.content) if msg.content else 0
                                    dropped_count += 1

                                    # Replace with placeholder text
                                    chars_from = len(msg.content) if msg.content else 0
                                    placeholder = f"[observation dropped: {chars_from} chars from {msg.name}]"
                                    messages[oldest_idx] = Message(
                                        role="tool",
                                        content=placeholder,
                                        tool_call_id=msg.tool_call_id,
                                        name=msg.name,
                                    )

                                # Emit context_trimmed event
                                ctx.emit(
                                    "context_trimmed",
                                    team_id=ctx.team_id,
                                    role=ctx.role_name,
                                    step=steps,
                                    dropped=dropped_count,
                                    chars=chars_dropped,
                                )
                        # Continue with the run after trimming

                if call.name in ("write_file", "edit_file"):
                    wrote = True
                    changed = changed or result.ok
                    looped = False  # the loop is broken: every tool is back
                    path = _written_path(call)
                    if path is not None:
                        written["tests" if is_test_path(path) else "code"] = True
                        wrote_paths.add(path.strip().removeprefix("./"))
                        changed_path = path.strip().removeprefix("./")
                        reads_seen = {k: v for k, v in reads_seen.items() if v[0] != changed_path}
                    if continuing:
                        written["code"] = True  # any write is the change this round owes
                if call.name == "run_tests":
                    last_test_output = result.output
                    last_tests_ok = result.ok
                seen = _settled(result.output)
                recent.append(
                    (call.name, call.arguments, seen)
                    if call.name in ("write_file", "edit_file")
                    else (call.name, seen)
                )
                recent = recent[-ctx.repeat_limit * MAX_CYCLE :]
                if ctx.stop_when is not None and ctx.stop_when(step_record):
                    forked = True
                    break
            if forked:
                final_text = "fork point: stuck, handing over to branches"
                stop = "fork_point"
                break
            if red_out and not test_wrong:
                said = str(messages[-1].content) if messages else ""
                final_text = f"stopped: the tests failed {red_runs} runs in a row.\n{said[-3000:]}"
                stop = "red_limit"
                break
            if test_wrong:
                # the last observation is the failing run with the supervisor's diagnosis after it
                said = str(messages[-1].content) if messages else ""
                final_text = f"stopped: a test I may not change is wrong.\n{said[-3000:]}"
                stop = "test_wrong"
                break
            cycle = _loop(recent, ctx.repeat_limit)
            if cycle is not None:
                tool = ", ".join(dict.fromkeys(cycle))
                if ctx.role.nudge_stuck and not repeat_warned:  # said once
                    repeat_warned = True
                    told = (  # the event names the hub text; the advice below may add to it
                        _steer(ctx, steps, "repeated", n=ctx.repeat_limit, tool=tool)
                        if len(cycle) == 1
                        else _steer(ctx, steps, "cycled", p=len(cycle), n=ctx.repeat_limit, tool=tool)
                    )
                    if ctx.on_stuck is not None:
                        told, advised = await _advised(ctx.on_stuck, ctx, told, messages)
                    messages.append(Message(role="user", content=told))
                    recent = []
                elif (
                    ctx.role.nudge_stuck
                    and can_write
                    and not looped
                    and any(t not in WRITE_TOOLS_ONLY for t in cycle)
                ):
                    # told, and repeating still: a writer loses the tool it loops on, not its run, so the
                    # change it owes is still possible (AIR-7 layer 02 attempts 4-6, 2026-10-18: every fix
                    # round ended here, probing, with its change unmade); a streak of refusals ends the run
                    looped = True
                    held_off = _steer(ctx, steps, "repeat-write-only", tool=tool)
                    messages.append(Message(role="user", content=held_off))
                    recent = []
                    if advised:  # told, and repeating still: the advice did not help
                        _settled_stuck(ctx.on_stuck, ctx.team_id, False)
                        advised = False
                else:
                    final_text = f"stopped: repeated the same action {ctx.repeat_limit} times ({tool})"
                    stop = "repeated_action"
                    break
            if (
                max_prompt + completion_tokens >= ctx.token_budget
            ):  # context size + generated, not resent history
                final_text = f"stopped: token budget {ctx.token_budget} exhausted"
                stop = "token_budget"
                break
            if schedule is not None:
                for kind, at in deadlines.items():
                    if steps == at:
                        if ctx.emit is not None:
                            ctx.emit(
                                "write_deadline",
                                team_id=ctx.team_id,
                                role=ctx.role_name,
                                kind_of_write=kind,
                                step=steps,
                                met=written[kind],
                            )
                        if not written[kind]:
                            missed.add(kind)
                            due = (
                                "change-now" if continuing else "tests-now" if kind == "tests" else "code-now"
                            )
                            said = _steer(ctx, steps, due, n=steps, total=ctx.max_steps)
                            messages.append(Message(role="user", content=said))
                    if (
                        kind in missed
                        and not written[kind]
                        and steps == at + WRITE_ONLY_AFTER
                        and steps < ctx.max_steps
                    ):
                        reading_off = True
                        reads_left = READS_UNDER_HOLD
                        escalated.add(kind)
                        what = (
                            "nothing this round"
                            if continuing
                            else "no test"
                            if kind == "tests"
                            else "no code"
                        )
                        said = _steer(ctx, steps, "write-only", what=what, n=steps, k=READS_UNDER_HOLD)
                        messages.append(Message(role="user", content=said))
                if reading_off and all(written[k] for k in escalated):
                    reading_off = False  # the write reading was switched off for has landed: it is back
            elif can_write and not (changed if continuing else wrote) and steps == write_by:
                if continuing:
                    said = _steer(ctx, steps, "write-now-continuing", n=steps, total=ctx.max_steps)
                else:
                    said = _steer(ctx, steps, "write-now", n=steps, total=ctx.max_steps)
                messages.append(Message(role="user", content=said))
            elif (
                can_write
                and not (changed if continuing else wrote)
                and steps == write_by + WRITE_ONLY_AFTER
                and steps < ctx.max_steps
            ):
                reading_off = True
                reads_left = READS_UNDER_HOLD
                what = "nothing this round" if continuing else "nothing"
                said = _steer(ctx, steps, "write-only", what=what, n=steps, k=READS_UNDER_HOLD)
                messages.append(Message(role="user", content=said))
            if schedule is None and reading_off and (changed if continuing else wrote):
                reading_off = False  # the write the hold waited for has landed: reading is back
            if steps >= ctx.max_steps:
                last_turn = True
                said = _steer(ctx, steps, "last-step", n=ctx.max_steps)
                messages.append(Message(role="user", content=said))
                continue
            if steps == ctx.max_steps - 1:
                messages.append(Message(role="user", content=_steer(ctx, steps, "one-step-left")))
        if advised:  # the advised nudge was not settled by a repeat after it: the run's result settles it
            _settled_stuck(ctx.on_stuck, ctx.team_id, ok)
        if red_advised:  # the run ended while the red-streak advice was open: green or not
            _settled_red(ctx.on_stuck, ctx.team_id, last_tests_ok is True)
        return TeamResult(
            team_id=ctx.team_id,
            ok=ok,
            stop_reason=stop,
            final_text=final_text,
            steps=steps,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            wall_ms=(time.monotonic() - t0) * 1000.0,
            state={"messages": dump_messages(messages)} if ctx.stop_when is not None else None,
            last_test_output=last_test_output,
        )


__all__ = ["NativeTeam", "ToolCall"]
