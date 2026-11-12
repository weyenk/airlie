"""The chat route's record of turns in flight: one per thread, and none that outlives a shutdown."""

from airlie.chat.api import Busy


def test_a_thread_takes_one_turn_at_a_time() -> None:
    busy = Busy()
    first = busy.take("t1")

    assert first is not None and "t1" in busy
    assert busy.take("t1") is None
    busy.release("t1")
    assert busy.take("t1") is not None


def test_stop_all_stops_the_turns_in_flight() -> None:
    busy = Busy()
    a, b = busy.take("t1"), busy.take("t2")
    assert a is not None and b is not None

    busy.stop_all()

    assert a.handle.stopped and b.handle.stopped


def test_a_turn_taken_after_stop_all_is_already_stopped() -> None:
    """One accepted in the gap while the server shuts down must not run to its end."""
    busy = Busy()
    busy.stop_all()

    late = busy.take("t1")

    assert late is not None and late.handle.stopped
