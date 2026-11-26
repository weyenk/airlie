"""The update poller's shape and its two promises: it starts by checking, and it never dies.

The plan's Step 2 asks for the shape comparison (``checker_coro``, ``interval_s``, plus
``start``/``stop``). ``LineTicker`` is no longer a class in the tree — the timer ticker was
retired from serve, which now pumps from board events — so the shape is asserted directly against
the signature the plan names rather than against a class that does not exist.
"""

from __future__ import annotations

import asyncio
import inspect

from airlie.update.ticker import UpdateTicker


def _calls():
    """A checker that counts passes and a signal that the first one happened."""
    seen: list[int] = []
    first = asyncio.Event()

    async def check() -> None:
        seen.append(len(seen))
        first.set()

    return seen, first, check


def test_signature_matches_the_shape_the_plan_names() -> None:
    params = list(inspect.signature(UpdateTicker.__init__).parameters)
    assert params == ["self", "checker_coro", "interval_s"]
    assert callable(UpdateTicker.start)
    assert inspect.iscoroutinefunction(UpdateTicker.stop)


def test_start_checks_immediately_then_waits() -> None:
    """A boot answers "what is available" without waiting out a day-long interval."""

    async def scenario() -> None:
        seen, first, check = _calls()
        ticker = UpdateTicker(check, 3600.0)
        ticker.start()
        await asyncio.wait_for(first.wait(), timeout=1.0)
        assert len(seen) == 1
        await ticker.stop()

    asyncio.run(scenario())


def test_a_failing_check_does_not_end_the_loop() -> None:
    """A down feed is a ledger row, not an outage: the poller keeps going and serve keeps serving."""

    async def scenario() -> None:
        passes: list[int] = []

        async def check() -> None:
            passes.append(1)
            raise RuntimeError("feed down")

        ticker = UpdateTicker(check, 0.01)
        ticker.start()
        await asyncio.sleep(0.1)
        assert len(passes) > 1, "a raised check must not stop the poller"
        assert ticker.running
        await ticker.stop()
        assert not ticker.running

    asyncio.run(scenario())


def test_stop_does_not_wait_out_the_interval() -> None:
    """With interval_s at a day, a sleep-based stop would hang shutdown for a day."""

    async def scenario() -> None:
        async def check() -> None:
            return None

        ticker = UpdateTicker(check, 3600.0)
        ticker.start()
        await asyncio.sleep(0.01)
        await asyncio.wait_for(ticker.stop(), timeout=1.0)
        assert not ticker.running

    asyncio.run(scenario())


def test_double_start_is_one_task() -> None:
    """Startup paths overlap (a restart, a re-created app); a second task would double the fetches."""

    async def scenario() -> None:
        seen, first, check = _calls()
        ticker = UpdateTicker(check, 3600.0)
        ticker.start()
        ticker.start()
        await asyncio.wait_for(first.wait(), timeout=1.0)
        await asyncio.sleep(0.02)
        assert len(seen) == 1, "the second start must not open a second poller"
        await ticker.stop()
        await ticker.stop()  # stopping twice is not an error
        assert not ticker.running

    asyncio.run(scenario())
