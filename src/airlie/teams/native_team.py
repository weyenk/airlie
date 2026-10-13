"""NativeTeam: a ReAct loop over the Engine seam. Reference implementation and fallback for the AutoGen team.

Each turn resends the whole conversation (the engine's prefix cache makes that cheap); the tool call is the
action u_t, its output the observation o_t. Entering a tool call releases the generation slot (spec 04 §2)."""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from airlie.engine.protocol import Engine
from airlie.engine.types import ContentPart, EngineError, GenRequest, Message, ToolCall
from airlie.teams.protocol import StepObserver, StepRecord, StopReason, TeamContext, TeamResult


def _goal_message(goal: str, worktree_hint: str, team_id: str = "", preamble: str | None = None) -> str:
    who = f"You are {team_id}. " if team_id else ""
    head = f"{preamble.rstrip()}\n\n" if preamble else ""
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


class NativeTeam:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    async def run(self, ctx: TeamContext, observer: StepObserver) -> TeamResult:
        t0 = time.monotonic()
        system = ctx.role.prompt.read_text()
        goal_msg = _goal_message(ctx.goal, ctx.worktree.name, ctx.team_id, ctx.preamble)
        messages: list[Message]
        if ctx.initial_state is not None:
            messages = load_messages(ctx.initial_state["messages"])
            messages.append(Message(role="user", content=ctx.nudge or goal_msg))
        else:
            messages = [Message(role="system", content=system), Message(role="user", content=goal_msg)]
        last_test_output = ""
        forked = False
        prompt_tokens = completion_tokens = 0
        max_prompt = 0
        steps = 0
        recent: list[tuple[str, str, str]] = []
        final_text = ""
        stop: StopReason = "done"
        ok = False
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
                tools=ctx.tools.schemas(),
                thinking=ctx.role.thinking,
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
            gen_ms = (time.monotonic() - g0) * 1000.0
            prompt_tokens += res.prompt_tokens
            max_prompt = max(max_prompt, res.prompt_tokens)
            completion_tokens += res.completion_tokens
            if not res.tool_calls:
                final_text = res.text
                ok = True
                stop = "done"
                break
            calls = res.tool_calls[:1]  # one action per step: later calls in a "plan" are often argument-less
            messages.append(Message(role="assistant", content=res.text, tool_calls=tuple(calls)))
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
                    tool_ms=tool_ms,
                    accepted_draft_tokens=res.accepted_draft_tokens,
                    draft_tokens=res.draft_tokens,
                )
                await observer.after_step(step_record)
                if call.name == "run_tests":
                    last_test_output = result.output
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
                final_text = f"stopped: repeated the same action {ctx.repeat_limit} times ({recent[0][0]})"
                stop = "repeated_action"
                break
            if steps >= ctx.max_steps:
                final_text = f"stopped: reached max_steps={ctx.max_steps}"
                stop = "max_steps"
                break
            if (
                max_prompt + completion_tokens >= ctx.token_budget
            ):  # context size + generated, not resent history
                final_text = f"stopped: token budget {ctx.token_budget} exhausted"
                stop = "token_budget"
                break
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
