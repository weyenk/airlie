"""The evidence builder: the trace tail comes from the newest attempt, long text is cut keeping its end, and
the triage message has its sections in order with the empty ones left out. Step files are synthetic, in the
shape LoggingObserver.after_step writes."""

from __future__ import annotations

import json
from pathlib import Path

from airlie.supervisor.evidence import TriageInput, layer_events, trace_tail, triage_message
from airlie.supervisor.ledger import Row


def write_step(
    folder: Path, n: int, reasoning: str = "", text: str = "", tool: str | None = None, obs: str = ""
) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"step-{n:03d}.json").write_text(
        json.dumps(
            {
                "u_t": {"name": tool or "", "arguments": {"path": "a.py"} if tool else {}},
                "o_t": obs,
                "ok": True,
                "assistant_text": text,
                "reasoning": reasoning,
            }
        )
    )


def row(i: int, **kw: object) -> Row:
    base: dict[str, object] = dict(
        id=i, at=0.0, story="S", layer="01", studio_run="r", subtask="tests", role="tester",
        trigger="precode_retry", kinds=("k",), category="missing_import", owner="tester",
        diagnosis=f"diag{i}", evidence=(), message=f"msg{i}", would_route_to=None, fix_if_attempt=None,
        fallback=None, model="m", seconds=1.0, tokens=1, outcome="helped", verdict=None,
    )  # fmt: skip
    base.update(kw)
    return Row(**base)  # type: ignore[arg-type]


def inp(**kw: object) -> TriageInput:
    base: dict[str, object] = dict(
        trigger="precode_retry", role="tester", subtask="tests", told="TOLD", evidence="FINDINGS",
        kinds=("passes_before_code",), brief="BRIEF", trace="", diff="DIFF", events="", earlier=(), recall=(),
    )  # fmt: skip
    base.update(kw)
    return TriageInput(**base)  # type: ignore[arg-type]


def test_trace_tail_reads_the_latest_round_and_its_last_steps(tmp_path: Path) -> None:
    for n in range(1, 4):
        write_step(tmp_path / "tests", n, reasoning=f"first-run {n}")
    for n in range(1, 9):
        write_step(tmp_path / "tests" / "round-1", n, reasoning=f"retry {n}", tool="read_file")
    tail = trace_tail(tmp_path, "tests", steps=3)
    assert "retry 8" in tail and "retry 6" in tail
    assert "retry 5" not in tail and "first-run" not in tail


def test_trace_tail_reads_a_given_round_when_asked(tmp_path: Path) -> None:
    """`airlie supervisor try` replays the first retry: what Muse read then is the first run, not the retry
    that came after it."""
    write_step(tmp_path / "tests", 1, reasoning="first-run 1")
    write_step(tmp_path / "tests" / "round-1", 1, reasoning="retry 1")
    assert "first-run 1" in trace_tail(tmp_path, "tests", steps=3, round_=0)
    assert "retry 1" in trace_tail(tmp_path, "tests", steps=3, round_=1)
    assert trace_tail(tmp_path, "tests", steps=3, round_=2) == ""


def test_trace_tail_picks_the_highest_round_not_the_last_by_name(tmp_path: Path) -> None:
    write_step(tmp_path / "tests" / "round-2", 1, reasoning="round two")
    write_step(tmp_path / "tests" / "round-10", 1, reasoning="round ten")
    write_step(tmp_path / "tests" / "round-3", 1, reasoning="round three")
    assert "round ten" in trace_tail(tmp_path, "tests", steps=1)


def test_trace_tail_shows_the_tool_call_and_a_short_observation(tmp_path: Path) -> None:
    write_step(tmp_path / "tests", 1, text="looking", tool="read_file", obs="o" * 400 + "TAIL")
    tail = trace_tail(tmp_path, "tests", steps=1)
    assert "read_file" in tail and "looking" in tail and "TAIL" not in tail and "o" * 300 in tail


def test_trace_tail_keeps_the_end_of_long_reasoning(tmp_path: Path) -> None:
    write_step(tmp_path / "tests", 1, reasoning="x" * 5000 + "THE END")
    assert "THE END" in trace_tail(tmp_path, "tests", steps=1)


def test_trace_tail_is_cut_keeping_the_end(tmp_path: Path) -> None:
    for n in range(1, 6):
        write_step(tmp_path / "tests", n, reasoning="y" * 100 + f"END{n}")
    tail = trace_tail(tmp_path, "tests", steps=5, chars=200)
    assert len(tail) == 200 and "END5" in tail and "END1" not in tail


def test_a_missing_trace_is_empty(tmp_path: Path) -> None:
    assert trace_tail(tmp_path, "build", steps=3) == ""


def test_layer_events_keep_only_this_layers_lines(tmp_path: Path) -> None:
    ev = tmp_path / "events.jsonl"
    ev.write_text('{"kind":"a","layer":"01"}\n{"kind":"b","layer":"02"}\n{"kind":"c"}\n')
    assert '"b"' in layer_events(ev, "02") and '"a"' not in layer_events(ev, "02")


def test_layer_events_leave_out_steer_events(tmp_path: Path) -> None:
    """A `steer` event counts a hub text the pipeline sent (prompt hub, 2026-10-22); it is not evidence, and
    Muse reads the layer's events as it read them before those events existed."""
    ev = tmp_path / "events.jsonl"
    ev.write_text(
        '{"kind":"test_checks_retry","layer":"01","findings":1}\n'
        '{"kind":"steer","id":"checks-found","role":"tester","layer":"01"}\n'
        '{"kind":"locked_tests_fail","layer":"01","tests":["t::a"]}\n'
    )
    assert layer_events(ev, "01") == (
        '{"kind":"test_checks_retry","layer":"01","findings":1}\n'
        '{"kind":"locked_tests_fail","layer":"01","tests":["t::a"]}'
    )


def test_layer_events_are_cut_keeping_the_end(tmp_path: Path) -> None:
    ev = tmp_path / "events.jsonl"
    ev.write_text("".join(f'{{"kind":"k{n}","layer":"01"}}\n' for n in range(100)))
    out = layer_events(ev, "01", chars=100)
    assert len(out) == 100 and "k99" in out and "k0," not in out


def test_missing_events_are_empty(tmp_path: Path) -> None:
    assert layer_events(tmp_path / "nope.jsonl", "01") == ""


def test_the_message_orders_its_sections_and_drops_empty_ones() -> None:
    msg = triage_message(inp())
    assert msg.index("TOLD") < msg.index("FINDINGS") < msg.index("BRIEF") < msg.index("DIFF")
    assert "recent steps" not in msg and "Earlier on this layer" not in msg


def test_all_sections_appear_in_the_specified_order() -> None:
    msg = triage_message(inp(trace="TRACE", events="EVENTS", earlier=(row(1),), recall=(row(2),)))
    heads = [
        "## Trigger", "## What the role is about to be told", "## Findings", "## The layer's brief",
        "## The role's recent steps", "## The diff", "## This layer's events", "## Earlier on this layer",
        "## Fixes that helped before",
    ]  # fmt: skip
    idx = [msg.index(h) for h in heads]
    assert idx == sorted(idx)


def test_brief_and_diff_are_capped() -> None:
    msg = triage_message(inp(brief="b" * 9_000, diff="d" * 21_000))
    assert msg.count("b") < 8_100 and msg.count("d") < 20_100


def test_a_recalled_row_is_one_block_capped_at_600() -> None:
    msg = triage_message(inp(recall=(row(1, diagnosis="d" * 2_000, message="m" * 2_000),)))
    section = msg.split("## Fixes that helped before\n")[1]
    assert len(section) <= 600 and "precode_retry" in section and "missing_import" in section


def test_a_long_recalled_message_still_leaves_its_owner_and_outcome() -> None:
    """The cap shortens what Muse said, never the fields that say what kind of fix it was and how it went."""
    msg = triage_message(inp(recall=(row(1, diagnosis="d" * 2_000, message="m" * 2_000),)))
    section = msg.split("## Fixes that helped before\n")[1]
    assert section.endswith("| outcome: helped") and "owner tester" in section
    assert "d" * 200 in section and "m" * 200 in section


def test_a_fallback_row_shows_its_reason() -> None:
    r = row(1, category=None, fallback="timeout", outcome=None)
    assert "fallback:timeout" in triage_message(inp(earlier=(r,)))


def test_earlier_section_is_capped_keeping_the_newest() -> None:
    rows = tuple(row(i, message="z" * 500) for i in range(1, 21))
    msg = triage_message(inp(earlier=rows))
    section = msg.split("## Earlier on this layer\n")[1]
    assert len(section) <= 4_000 and "diag20" in section and "diag1 " not in section
