"""EdgeAgent scheduling (spec 04 §1–2): HAL budgets, agent states, slot accounting, tool-stall yield."""

from __future__ import annotations

import asyncio

import pytest

from airlie.scheduler import AgentState, HALSpeculativeScheduler, SlotScheduler


def test_hal_scheduler() -> None:
    """Spec 04 §4: softmax allocation is bounded by L_total=16 and never below L_min."""
    s = HALSpeculativeScheduler(L_total=16, L_min=1, gamma=0.3, tau=1.5)
    agents = ["t1", "t2", "t3"]
    alloc = s.allocate_budgets(agents)
    assert set(alloc) == set(agents)
    assert sum(alloc.values()) <= 16
    assert all(v >= 1 for v in alloc.values())
    # equal priors → equal shares: 1 + floor(13/3) = 5 each
    assert alloc == {"t1": 5, "t2": 5, "t3": 5}


def test_hal_update_is_ema_with_prior_4() -> None:
    s = HALSpeculativeScheduler()
    s.update_hal("a", 10)
    assert s.hal_history["a"] == pytest.approx(0.3 * 10 + 0.7 * 4.0)
    s.update_hal("a", 0)
    assert s.hal_history["a"] == pytest.approx(0.7 * (0.3 * 10 + 0.7 * 4.0))


def test_hal_rewards_predictable_agents() -> None:
    s = HALSpeculativeScheduler()
    for _ in range(5):
        s.update_hal("easy", 12)
        s.update_hal("hard", 0)
    alloc = s.allocate_budgets(["easy", "hard"])
    assert alloc["easy"] > alloc["hard"] >= 1
    assert alloc["easy"] + alloc["hard"] <= 16


def test_hal_degenerate_cases() -> None:
    s = HALSpeculativeScheduler(L_total=2, L_min=1)
    assert s.allocate_budgets([]) == {}
    assert s.allocate_budgets(["a", "b", "c"]) == {"a": 1, "b": 1, "c": 1}  # residual <= 0 → L_min each


def test_agent_states_are_the_spec_names() -> None:
    assert AgentState.ACTIVE.value == "ACTIVE"
    assert AgentState.SUSPENDED_TOOL_STALL.value == "SUSPENDED_TOOL_STALL"


async def test_slots_released_on_tool_call() -> None:
    sched = SlotScheduler(max_active=1)
    order: list[str] = []

    async def team(name: str, stall: float) -> None:
        async with sched.generation(name):  # ACTIVE: holds a slot
            order.append(f"{name}:gen")
            await asyncio.sleep(0.01)
        async with sched.tool_stall(name):  # SUSPENDED_TOOL_STALL: slot released
            order.append(f"{name}:tool")
            await asyncio.sleep(stall)
        async with sched.generation(name):
            order.append(f"{name}:gen2")

    await asyncio.gather(team("a", 0.2), team("b", 0.0))
    # b's generation ran while a was stalled in its tool call
    assert order.index("b:gen") < order.index("a:gen2")
    m = sched.metrics()
    assert m.yield_count == 2
    assert m.max_concurrent == 1
    assert sched.state("a") == AgentState.ACTIVE


async def test_slots_never_exceed_max_active() -> None:
    sched = SlotScheduler(max_active=2)
    peak = 0
    active = 0

    async def gen(name: str) -> None:
        nonlocal peak, active
        async with sched.generation(name):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(gen(f"t{i}") for i in range(6)))
    assert peak == 2
    assert sched.metrics().slot_wait_ms_total > 0
