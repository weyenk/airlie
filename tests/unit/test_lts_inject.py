"""Injection block (spec 04 §3 format), retrieve_value tool, and the admission/retrieval logs M5 consumes."""

from __future__ import annotations

import json
from pathlib import Path

from airlie.engine import ToolCall
from airlie.lts import MemoryEntry, SharedMemoryBank
from airlie.lts.inject import render_keys
from airlie.lts.log import AdmissionLog
from airlie.lts.memory_tool import RetrieveValue
from airlie.tools import ToolContext, ToolRegistry


def bank_with(n: int) -> SharedMemoryBank:
    b = SharedMemoryBank()
    for i in range(1, n + 1):
        b.add_entry(
            MemoryEntry(
                id=f"mem-{i}", team_id="team-1", step_num=i, summary_key=f"key {i}", raw_value=f"v{i}"
            )
        )
    return b


def test_render_keys_uses_the_spec_format() -> None:
    text = render_keys(bank_with(2).get_summaries(), max_keys=32)
    assert (
        text == "\n[Shared Memory Keys Available]:\n- Key [mem-2]: key 2\n- Key [mem-1]: key 1"
    )  # newest first


def test_render_keys_empty_is_empty() -> None:
    assert render_keys([], max_keys=32) == ""


def test_inject_bounded_by_max_keys_newest_first() -> None:
    text = render_keys(bank_with(5).get_summaries(), max_keys=2)
    assert text.count("- Key [") == 2
    assert "[mem-5]" in text and "[mem-4]" in text and "[mem-1]" not in text


async def test_retrieve_value_tool_returns_value_and_counts_cross_team(tmp_path: Path) -> None:
    bank = bank_with(1)
    reg = ToolRegistry.builtin([], ToolContext(worktree=tmp_path))
    reg.add(RetrieveValue(bank, team_id="team-2"))
    assert reg.names() == ["retrieve_value"]
    r = await reg.call(ToolCall("1", "retrieve_value", '{"entry_id": "mem-1"}'))
    assert r.ok and r.output == "v1"
    assert bank.entries()[0].access_count == 1


async def test_retrieve_unknown_id(tmp_path: Path) -> None:
    reg = ToolRegistry.builtin([], ToolContext(worktree=tmp_path))
    reg.add(RetrieveValue(bank_with(1), team_id="team-2"))
    r = await reg.call(ToolCall("1", "retrieve_value", '{"entry_id": "nope"}'))
    assert not r.ok and "no memory entry 'nope'" in r.output and "mem-1" in r.output


def test_admission_log_schema(tmp_path: Path) -> None:
    log = AdmissionLog(tmp_path / "lts")
    log.admission(
        run_id="r1",
        team_id="team-1",
        step=3,
        query="q",
        memory_keys=["k1"],
        u_t=ToolCall("c", "shell", "{}"),
        s_t="summary",
        o_t="out",
        decision=True,
        p_yes=0.8,
        reason="tool_class",
        entry_id="mem-1",
        controller="heuristic",
    )
    log.retrieval(run_id="r1", team_id="team-2", step=5, entry_id="mem-1", found=True)
    adm = [json.loads(x) for x in (tmp_path / "lts" / "admission.jsonl").read_text().splitlines()]
    ret = [json.loads(x) for x in (tmp_path / "lts" / "retrieval.jsonl").read_text().splitlines()]
    assert set(adm[0]) >= {
        "run_id",
        "team_id",
        "step",
        "query",
        "memory_keys",
        "u_t",
        "s_t",
        "o_t",
        "decision",
        "p_yes",
        "reason",
        "entry_id",
        "controller",
        "used_later",
        "episode_reward",
        "ts",
    }
    assert adm[0]["u_t"] == {"name": "shell", "arguments": "{}"} and adm[0]["used_later"] is None
    assert ret[0]["entry_id"] == "mem-1" and ret[0]["team_id"] == "team-2"


def test_finalize_marks_used_later_and_reward(tmp_path: Path) -> None:
    log = AdmissionLog(tmp_path / "lts")
    for i, eid in enumerate(("mem-1", "mem-2"), 1):
        log.admission(
            run_id="r1",
            team_id="team-1",
            step=i,
            query="q",
            memory_keys=[],
            u_t=ToolCall("c", "shell", "{}"),
            s_t="s",
            o_t="o",
            decision=True,
            p_yes=0.9,
            reason="r",
            entry_id=eid,
            controller="heuristic",
        )
    log.retrieval(run_id="r1", team_id="team-2", step=4, entry_id="mem-2", found=True)
    log.finalize(episode_reward={"team-1": 1.0, "team-2": 0.0})
    adm = [json.loads(x) for x in (tmp_path / "lts" / "admission.jsonl").read_text().splitlines()]
    assert [a["used_later"] for a in adm] == [False, True]
    assert all(a["episode_reward"] == 1.0 for a in adm)


# ---- relevance-ranked injection with inlined values (post-M6 bench follow-up) ----


def bank_mixed() -> SharedMemoryBank:
    b = SharedMemoryBank()
    rows = [
        ("mem-1", "team-1", "Listed repository files: inventory/report.py, inventory/cli.py", "report.py"),
        (
            "mem-2",
            "team-2",
            "run_tests: FAILED test_total_cost expected 1500 got 15",
            "FAILED test_total_cost",
        ),
        ("mem-3", "team-1", "Edited README wording", "README"),
        (
            "mem-4",
            "team-3",
            "Root cause: report.py adds qty + unit_cost_cents instead of multiplying",
            "x" * 5000,
        ),
    ]
    for i, (eid, team, key, val) in enumerate(rows, 1):
        b.add_entry(MemoryEntry(id=eid, team_id=team, step_num=i, summary_key=key, raw_value=val))
    return b


def test_rank_entries_prefers_overlap_with_context_and_excludes_own_team() -> None:
    from airlie.lts.inject import rank_entries

    ranked = rank_entries(
        bank_mixed().entries(),
        context="fix total cost in report.py: test_total_cost fails",
        max_keys=3,
        exclude_team="team-1",
    )
    ids = [r.entry.id for r in ranked]
    assert "mem-1" not in ids and "mem-3" not in ids  # own team never injected
    assert ids[0] in {"mem-2", "mem-4"} and ids[1] in {"mem-2", "mem-4"}
    assert ranked[0].score >= ranked[1].score > 0


def test_rank_entries_falls_back_to_recency_without_overlap() -> None:
    from airlie.lts.inject import rank_entries

    ranked = rank_entries(bank_mixed().entries(), context="zzz qqq", max_keys=2, exclude_team=None)
    assert [r.entry.id for r in ranked] == ["mem-4", "mem-3"]  # newest first
    assert all(r.score == 0 for r in ranked)


def test_rank_entries_recency_mode_ignores_context() -> None:
    from airlie.lts.inject import rank_entries

    ranked = rank_entries(
        bank_mixed().entries(), context="report.py", max_keys=2, exclude_team=None, ranking="recency"
    )
    assert [r.entry.id for r in ranked] == ["mem-4", "mem-3"]


def test_render_entries_keeps_spec_lines_and_inlines_chosen_values() -> None:
    from airlie.lts.inject import rank_entries, render_entries

    ranked = rank_entries(bank_mixed().entries(), context="report.py total", max_keys=3, exclude_team=None)
    text = render_entries(ranked, inline_ids={"mem-4"}, inline_chars=100)
    assert text.startswith("\n[Shared Memory Keys Available]:\n- Key [")
    assert "- Key [mem-4]: Root cause: report.py adds qty + unit_cost_cents instead of multiplying" in text
    assert "x" * 100 in text and "x" * 101 not in text  # inlined, capped
    assert "[truncated" in text
    assert text.count("Value") == 1  # only the chosen id is inlined


# ---- boundaries and exact rendering (mutation-testing follow-up) ----


def entry(i: int, key: str, value: str = "v", team: str = "team-1") -> MemoryEntry:
    return MemoryEntry(id=f"mem-{i}", team_id=team, step_num=i, summary_key=key, raw_value=value)


def test_render_keys_max_keys_boundaries() -> None:
    summaries = [("mem-1", "one"), ("mem-2", "two")]
    assert render_keys(summaries, max_keys=0) == ""
    assert render_keys(summaries, max_keys=-1) == ""
    assert render_keys(summaries, max_keys=1) == "\n[Shared Memory Keys Available]:\n- Key [mem-2]: two"


def test_rank_entries_max_keys_boundaries() -> None:
    from airlie.lts.inject import rank_entries

    entries = [entry(1, "alpha"), entry(2, "beta")]
    assert rank_entries(entries, context="alpha", max_keys=0, exclude_team=None) == []
    assert rank_entries(entries, context="alpha", max_keys=-1, exclude_team=None) == []
    one = rank_entries(entries, context="alpha", max_keys=1, exclude_team=None)
    assert [r.entry.id for r in one] == ["mem-1"]


def test_rank_entries_defaults_to_relevance_over_recency() -> None:
    from airlie.lts.inject import rank_entries

    entries = [entry(1, "parser handles quoted fields"), entry(2, "unrelated gardening note")]
    ranked = rank_entries(entries, context="the quoted parser fields", max_keys=2, exclude_team=None)
    assert [r.entry.id for r in ranked] == ["mem-1", "mem-2"]  # relevance beats newest-first
    assert [r.score for r in ranked] == [3.0, 0.0]


def test_recency_scores_are_float_zero() -> None:
    from airlie.lts.inject import rank_entries

    ranked = rank_entries(
        [entry(1, "a"), entry(2, "b")], context="x", max_keys=2, exclude_team=None, ranking="recency"
    )
    assert [r.score for r in ranked] == [0.0, 0.0] and all(isinstance(r.score, float) for r in ranked)


def test_token_length_boundary_counts_three_letter_words_but_not_two() -> None:
    from airlie.lts.inject import rank_entries

    entries = [entry(1, "abc abcd xy")]
    ranked = rank_entries(entries, context="abc abcd xy", max_keys=1, exclude_team=None)
    assert ranked[0].score == 2.0  # "abc" (3) and "abcd" (4) count; "xy" (2) does not


def test_render_entries_exact_text_with_and_without_inlined_value() -> None:
    from airlie.lts.inject import RankedKey, render_entries

    ranked = [RankedKey(entry(1, "first", "VALUE"), 1.0), RankedKey(entry(2, "second", "other"), 0.0)]
    text = render_entries(ranked, inline_ids={"mem-1"}, inline_chars=100)
    assert text == (
        "\n[Shared Memory Keys Available]:\n"
        "- Key [mem-1]: first\n"
        "  Value:\n  ```\nVALUE\n  ```\n"
        "- Key [mem-2]: second"
    )
    assert render_entries([], inline_ids=set(), inline_chars=100) == ""


def test_render_entries_inline_chars_boundaries() -> None:
    from airlie.lts.inject import RankedKey, render_entries

    ranked = [RankedKey(entry(1, "first", "ABCDE"), 1.0)]
    assert "Value" not in render_entries(ranked, inline_ids={"mem-1"}, inline_chars=0)
    assert "Value" not in render_entries(ranked, inline_ids={"mem-1"}, inline_chars=-5)
    one = render_entries(ranked, inline_ids={"mem-1"}, inline_chars=1)
    assert one.endswith("  Value:\n  ```\nA\n  ```\n  [truncated: 4 more chars; retrieve_value for the rest]")


def test_render_entries_truncation_notice_only_when_something_was_cut() -> None:
    from airlie.lts.inject import RankedKey, render_entries

    ranked = [RankedKey(entry(1, "first", "ABCDE"), 1.0)]
    exact = render_entries(ranked, inline_ids={"mem-1"}, inline_chars=5)
    assert exact.endswith("  Value:\n  ```\nABCDE\n  ```") and "truncated" not in exact
    longer = render_entries(ranked, inline_ids={"mem-1"}, inline_chars=50)
    assert longer.endswith("ABCDE\n  ```") and "truncated" not in longer
    cut_by_one = render_entries(ranked, inline_ids={"mem-1"}, inline_chars=4)
    assert cut_by_one.endswith("ABCD\n  ```\n  [truncated: 1 more chars; retrieve_value for the rest]")
    cut = render_entries(ranked, inline_ids={"mem-1"}, inline_chars=3)
    assert "[truncated: 2 more chars;" in cut
