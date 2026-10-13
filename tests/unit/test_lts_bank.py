"""Spec 03 §1: MemoryEntry and SharedMemoryBank, verbatim names, cross-team access counting, persistence."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from airlie.lts import MemoryEntry, SharedMemoryBank


def entry(i: int, team: str = "team-1") -> MemoryEntry:
    return MemoryEntry(
        id=f"mem-{i}", team_id=team, step_num=i, summary_key=f"summary {i}", raw_value=f"value {i}"
    )


def test_add_and_get_summaries_in_insertion_order() -> None:
    bank = SharedMemoryBank()
    bank.add_entry(entry(1))
    bank.add_entry(entry(2, "team-2"))
    assert bank.get_summaries() == [("mem-1", "summary 1"), ("mem-2", "summary 2")]


def test_retrieve_value_counts_only_cross_team_access() -> None:
    bank = SharedMemoryBank()
    bank.add_entry(entry(1, "team-1"))
    assert bank.retrieve_value("mem-1", "team-1") == "value 1"
    assert bank.entries()[0].access_count == 0
    assert bank.retrieve_value("mem-1", "team-2") == "value 1"
    assert bank.retrieve_value("mem-1", "team-3") == "value 1"
    assert bank.entries()[0].access_count == 2


def test_retrieve_unknown_id() -> None:
    assert SharedMemoryBank().retrieve_value("nope", "team-1") is None


def test_entry_defaults_timestamp_and_access_count() -> None:
    e = entry(1)
    assert e.access_count == 0 and e.timestamp > 0


def test_persistence_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "memory.jsonl"
    bank = SharedMemoryBank(persist_path=path)
    bank.add_entry(entry(1))
    bank.retrieve_value("mem-1", "team-2")
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert lines[0]["kind"] == "add" and lines[0]["entry"]["summary_key"] == "summary 1"
    assert lines[1] == {
        "kind": "retrieve",
        "entry_id": "mem-1",
        "requesting_team_id": "team-2",
        "ts": lines[1]["ts"],
    }
    loaded = SharedMemoryBank.load(path)
    assert loaded.get_summaries() == [("mem-1", "summary 1")]
    assert loaded.entries()[0].access_count == 1


async def test_bank_concurrent_adds() -> None:
    bank = SharedMemoryBank()

    async def add(team: str) -> None:
        for i in range(50):
            bank.add_entry(
                MemoryEntry(id=bank.new_id(team, i), team_id=team, step_num=i, summary_key="s", raw_value="v")
            )
            await asyncio.sleep(0)

    await asyncio.gather(add("team-1"), add("team-2"), add("team-3"))
    ids = [e.id for e in bank.entries()]
    assert len(ids) == 150 and len(set(ids)) == 150
