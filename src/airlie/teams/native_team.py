"""NativeTeam: a ReAct loop over the Engine seam. Reference implementation and fallback for the AutoGen team.

Each turn resends the whole conversation (the engine's prefix cache makes that cheap); the tool call is the
action u_t, its output the observation o_t. Entering a tool call releases the generation slot (spec 04 §2)."""

from __future__ import annotations

import re
import time
from dataclasses import asdict, replace
from typing import Any

from airlie.engine.protocol import Engine
from airlie.engine.types import ContentPart, EngineError, GenRequest, Message, ToolCall, image_part
from airlie.skills import prompt_text
from airlie.teams.call_repair import Repair, repair_call
from airlie.teams.protocol import StepObserver, StepRecord, StopReason, TeamContext, TeamResult, cut_off_text
from airlie.tools.jsonargs import history_arguments


def _goal_message(
    goal: str, worktree_hint: str, team_id: str = "", preamble: str | None = None, *, tools: bool = True
) -> str:
    who = f"You are {team_id}. " if team_id else ""
    head = f"{preamble.rstrip()}\n\n" if preamble else ""
    if not tools:  # told to use tools it does not have, a model invents them (2026-10-16: Read, list_files)
        return (
            f"{head}Goal:\n{goal}\n\n{who}"
            "You have no tools in this task: answer directly, in your reply, with the complete deliverable. "
            "When it is files (HTML, Markdown, code), give each one in full in a fenced block headed by its "
            "path."
        )
    return (
        f"{head}Goal:\n{goal}\n\n{who}"
        f"You are working inside a git checkout of the repository ({worktree_hint}). "
        "Use the tools to inspect and change it, run the tests, and stop with a short summary when they pass."
    )


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
MALFORMED = (
    "Your last reply had a tool call written as text, and it could not be run. Call the tool again in the "
    "proper tool-call format, or, if you are finished, give your answer with no tool-call markup. A call in "
    "the proper format looks like this, one <parameter=NAME> per argument: "
    "<tool_call><function=read_file><parameter=path>README.md</parameter></function></tool_call>"
)
"""What a role is told after one such reply; a second in a row ends the run (malformed_call). Only a reply
holding `<tool_call>` or `<function=` gets it, so the example is in the format the chat template teaches."""
MALFORMED_TEXT_CAP = 4000
"""Characters of a malformed reply kept in its `malformed_call` event."""

NO_TOOLS = (
    "You have no tools in this task, so nothing you call can run. Answer directly, in your reply, with the "
    "complete deliverable."
)
"""What a role without tools is told after one reply that calls a tool anyway; a second in a row ends the run
(no_tools)."""

LAST_STEP = (
    "You have used all {n} of your steps: no more tools. Give your final answer now, complete, from what "
    "you have already found."
)
"""What a role is told on the turn after its last allowed step, which offers it no tools."""

WRITE_NOW = (
    "You have used {n} of your {total} steps and have written nothing. Stop reading: you have what you need. "
    "Write the change now (a test file first, if the task asks for tests), then run run_tests."
)
"""What a role with write tools is told, once, when it is two-fifths through its steps without a write."""

WRITE_ONLY = (
    "You still have written nothing after {n} steps, so reading is switched off: only write_file, edit_file "
    "and run_tests are available until you write. Make the change from what you have read."
)
"""What a role is told, once, when the nudge did not make it write and its read tools are taken away."""

WRITE_TOOLS_ONLY = frozenset({"write_file", "edit_file", "run_tests"})
"""The tools a role keeps once its reading is switched off."""
WRITE_ONLY_AFTER = 4
"""Steps after the write-now nudge by which a role that still has not written loses its read tools."""

REPEATED = (
    "You have made the same call {n} times in a row with the same result ({tool}). Repeating it changes "
    "nothing. Do something different: act on what you know, or give your answer."
)
"""What a role (nudge_stuck) is told, once, when it repeats an action instead of being stopped."""

STILL_RED = (
    "Your last run_tests run failed, so you are not done. Fix what it reports and run it again. If something "
    "outside your task blocks it, say in your reply which test fails and why."
)
"""What a role (green_before_done) is told, once, when it answers with its last run_tests run failed."""

LAST_TURN_RETRY = (
    "No tools are available on this turn, so nothing you call can run. Write your answer now, complete, in "
    "your reply, from what you have already read."
)
"""What a role is told once when it calls a tool on the turn that offers none (a second time ends the run)."""

ONE_STEP_LEFT = (
    "You have one step left. Use it only if you must; then give your final answer, complete, from what you "
    "have found. Stop exploring and write the deliverable."
)
"""What a role is told after its second-to-last step, so it finishes while it still has a tool to spare."""


def _offered(ctx: TeamContext, reading_off: bool) -> list[Any]:
    """The tool schemas a turn offers: all of them, or (reading switched off) those that write or test."""
    schemas = ctx.tools.schemas()
    if not reading_off:
        return list(schemas)
    return [x for x in schemas if x.name in WRITE_TOOLS_ONLY]


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


class NativeTeam:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    async def run(self, ctx: TeamContext, observer: StepObserver) -> TeamResult:
        t0 = time.monotonic()
        system = prompt_text(ctx.role.prompt)
        toolless = not ctx.tools.names()
        goal_msg = _goal_message(ctx.goal, ctx.worktree.name, ctx.team_id, ctx.preamble, tools=not toolless)
        messages: list[Message]
        if ctx.initial_state is not None:
            messages = load_messages(ctx.initial_state["messages"])
            messages.append(Message(role="user", content=ctx.nudge or goal_msg))
        else:
            parts = None
            if ctx.images:
                parts = (ContentPart("text", text=goal_msg), *[image_part(p) for p in ctx.images])
            messages = [
                Message(role="system", content=system),
                Message(role="user", content=goal_msg, parts=parts),
            ]
        last_test_output = ""
        forked = False
        prompt_tokens = completion_tokens = 0
        max_prompt = 0
        steps = 0
        recent: list[tuple[str, str, str]] = []
        final_text = ""
        stop: StopReason = "done"
        ok = False
        corrected = False  # the current streak of unparsed tool calls has had its one correction
        last_corrected = False  # the tool-less last turn has had its one correction
        can_write = bool({"write_file", "edit_file"} & set(ctx.tools.names()))
        wrote = False  # a write_file or edit_file has run
        write_by = max(4, ctx.max_steps * 2 // 5)  # the step by which a role that can write has started to
        reading_off = False  # the role never wrote after the nudge: it is offered write tools only
        repeat_warned = False  # the first repeated action has been said to the role
        red_warned = False  # the role has been told once that it answered with run_tests failing
        last_tests_ok: bool | None = None  # the result of the last run_tests call (None: none yet)
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
                tools=None if last_turn or toolless else _offered(ctx, reading_off),
                thinking=ctx.role.thinking and (ctx.role.last_turn_thinking or not last_turn),
                draft_budget=ctx.draft_budget(),
            )
            g0 = time.monotonic()
            try:
                async with ctx.scheduler.generation(ctx.team_id):
                    res = await self.engine.generate(req)
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
            if toolless and res.finish_reason != "length" and (calls or UNPARSED_CALL.search(res.text)):
                if corrected:
                    final_text = "stopped: two replies in a row called tools, and this role has none"
                    stop = "no_tools"
                    break
                corrected = True
                messages.append(Message(role="assistant", content=res.text))
                messages.append(Message(role="user", content=NO_TOOLS))
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
                    if (
                        ctx.role.green_before_done
                        and has_run_tests
                        and last_tests_ok is False
                        and not red_warned
                        and not last_turn
                    ):
                        red_warned = True
                        messages.append(Message(role="assistant", content=res.text))
                        messages.append(Message(role="user", content=STILL_RED))
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
                        messages.append(Message(role="user", content=LAST_TURN_RETRY))
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
                    messages.append(Message(role="user", content=MALFORMED))
                    continue
            corrected = False
            if last_turn:  # it called a tool it was not offered: told once, then there is no answer
                if not last_corrected:
                    last_corrected = True
                    messages.append(Message(role="assistant", content=res.text))
                    messages.append(Message(role="user", content=LAST_TURN_RETRY))
                    continue
                final_text = f"stopped: reached max_steps={ctx.max_steps}"
                stop = "max_steps"
                break
            replay = tuple(replace(c, arguments=history_arguments(c.arguments)) for c in calls)
            messages.append(Message(role="assistant", content=content, tool_calls=replay))
            for call in calls:
                steps += 1
                async with ctx.scheduler.tool_stall(ctx.team_id):
                    t1 = time.monotonic()
                    result = await ctx.tools.call(call)
                    tool_ms = (time.monotonic() - t1) * 1000.0
                messages.append(
                    Message(role="tool", content=result.output, tool_call_id=call.id, name=call.name)
                )
                step_record = StepRecord(
                    team_id=ctx.team_id,
                    step_num=steps,
                    u_t=call,
                    o_t=result.output,
                    ok=result.ok,
                    assistant_text=res.text,
                    prompt_tokens=res.prompt_tokens,
                    completion_tokens=res.completion_tokens,
                    gen_ms=gen_ms,
                    prefill_ms=prefill_ms,
                    tool_ms=tool_ms,
                    accepted_draft_tokens=res.accepted_draft_tokens,
                    draft_tokens=res.draft_tokens,
                )
                await observer.after_step(step_record)
                if call.name in ("write_file", "edit_file"):
                    wrote = True
                if call.name == "run_tests":
                    last_test_output = result.output
                    last_tests_ok = result.ok
                recent.append((call.name, call.arguments, result.output))
                recent = recent[-ctx.repeat_limit :]
                if ctx.stop_when is not None and ctx.stop_when(step_record):
                    forked = True
                    break
            if forked:
                final_text = "fork point: stuck, handing over to branches"
                stop = "fork_point"
                break
            if len(recent) == ctx.repeat_limit and len(set(recent)) == 1:
                if (
                    ctx.role.nudge_stuck and not repeat_warned
                ):  # said once; a second run of repeats ends the run
                    repeat_warned = True
                    messages.append(
                        Message(role="user", content=REPEATED.format(n=ctx.repeat_limit, tool=recent[0][0]))
                    )
                    recent = []
                else:
                    final_text = (
                        f"stopped: repeated the same action {ctx.repeat_limit} times ({recent[0][0]})"
                    )
                    stop = "repeated_action"
                    break
            if (
                max_prompt + completion_tokens >= ctx.token_budget
            ):  # context size + generated, not resent history
                final_text = f"stopped: token budget {ctx.token_budget} exhausted"
                stop = "token_budget"
                break
            if can_write and not wrote and steps == write_by:
                messages.append(Message(role="user", content=WRITE_NOW.format(n=steps, total=ctx.max_steps)))
            if can_write and not wrote and steps == write_by + WRITE_ONLY_AFTER and steps < ctx.max_steps:
                reading_off = True
                messages.append(Message(role="user", content=WRITE_ONLY.format(n=steps)))
            if steps >= ctx.max_steps:
                last_turn = True
                messages.append(Message(role="user", content=LAST_STEP.format(n=ctx.max_steps)))
                continue
            if steps == ctx.max_steps - 1:
                messages.append(Message(role="user", content=ONE_STEP_LEFT))
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
