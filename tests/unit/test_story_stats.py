"""The numbers that judge whether stories go through unattended (planning-line P5b, D9, 2026-10-25): the
unattended rate, blocks by class and kind, hours against estimates, the amigos' objections and how many were
borne out, and the budgets' warnings."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from airlie.board import BoardStore
from airlie.story.diagnosis import Classified, Diagnosis, Pack, Paths, write_diagnosis
from airlie.story.stats import line_stats, render_stats


def make_story(store: BoardStore, title: str = "s") -> str:
    return store.create(level="story", title=title).key


def go(store: BoardStore, key: str, moves: list[tuple[str, str, float]]) -> None:
    """Moves as (status, by, time), in order, and comments as ("comment", author, time)."""
    for status, by, stamp in moves:
        if status == "comment":
            store.comment(key, by, "a word", at=stamp)
        else:
            store.move(key, status, by=by)  # type: ignore[arg-type]
            with sqlite3.connect(store.path) as db:
                db.execute("UPDATE moves SET at = ? WHERE id = (SELECT MAX(id) FROM moves)", (stamp,))


def test_the_unattended_rate_counts_only_stories_a_person_never_touched_between_approval_and_review(
    tmp_path: Path,
) -> None:
    store = BoardStore(tmp_path / "board.sqlite")
    clean, parked, spoke, still = (make_story(store, t) for t in ("clean", "parked", "spoke", "still"))
    go(store, clean, [("approved", "noah", 10), ("doing", "line", 11), ("review", "line", 50)])
    go(store, parked, [("approved", "noah", 10), ("doing", "line", 11), ("todo", "noah", 20),
                       ("approved", "noah", 30), ("doing", "line", 31), ("review", "line", 60)])  # fmt: skip
    go(
        store,
        spoke,
        [("approved", "noah", 10), ("doing", "line", 11), ("comment", "noah", 20), ("review", "line", 70)],
    )
    go(store, still, [("approved", "noah", 10), ("doing", "line", 11)])  # not in review yet: not counted
    stats = line_stats(tmp_path, store)
    # `clean` is the one: `parked` had a person move the card, `spoke` a person's comment, after the first
    # approval and before the review (a person's re-approval is a person's act)
    assert stats["unattended"] == {"stories": 3, "unattended": 1, "rate": 1 / 3}
    # the line's own notes are not a person's comment
    store.comment(clean, "airlie", "**airlie** · picked this up", at=30)
    store.comment(clean, "product", "a story note", at=31)
    assert line_stats(tmp_path, store, roles=frozenset({"product"}))["unattended"]["unattended"] == 1
    # since: only reviews from then on
    assert line_stats(tmp_path, store, since=55)["unattended"] == {"stories": 2, "unattended": 0, "rate": 0.0}
    assert line_stats(tmp_path, store, since=1000)["unattended"] == {
        "stories": 0,
        "unattended": 0,
        "rate": None,
    }


def diagnose(state: Path, key: str, class_: str, kind: str) -> None:
    pack = Pack(
        key=key, n=1, reason="x", stage="layers", layer="01", attempt=1, run_id=None, subtask=None,
        role=None, stop_reason=None, refused_calls=0, holds=(), repeats=0, continuations=0, steps=0,
        tokens=0, minutes=0.0, gate_tail="", tracebacks=(), failures=(), base_gate_failed=None,
        base_failing=(), diff_stat="", paths=Paths(state / "e", None, None, ()),
    )  # fmt: skip
    write_diagnosis(
        state,
        Diagnosis(pack, Classified(class_, kind, "layers", f"{class_}:{kind}:x", (kind,), kind), None, "n"),
    )  # type: ignore[arg-type]


def write_events(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"ts": time.time(), **e}) + "\n" for e in events))


def test_blocks_group_by_class_and_kind_with_fixes_filed_and_resumed(tmp_path: Path) -> None:
    store = BoardStore(tmp_path / "board.sqlite")
    blocked = [
        ("AIR-1", "process", "hold_trap"),
        ("AIR-2", "process", "hold_trap"),
        ("AIR-3", "content", "gate_failed"),
        ("AIR-4", "transient", "fetch_failed"),
    ]
    for key, class_, kind in blocked:
        diagnose(tmp_path, key, class_, kind)
    fix = store.create(level="story", title="Line: AIR-1 blocked", labels=["fix", "repo:o/airlie"])
    store.create(level="story", title="not a fix")
    write_events(
        tmp_path / "line" / "events.jsonl",
        [
            {"kind": "fix_filed", "story": "AIR-1", "fix": fix.key},
            {"kind": "resumed_after_fix", "story": "AIR-1"},
        ],
    )
    blocks = line_stats(tmp_path, store)["blocks"]
    assert blocks["by_class"] == {"process": 2, "content": 1, "transient": 1}
    assert blocks["by_kind"] == {"hold_trap": 2, "gate_failed": 1, "fetch_failed": 1}
    assert blocks["fix_cards_filed"] == 1 and blocks["resumed_after_fix"] == 1


def test_amigos_objections_count_by_role_and_part_with_the_share_borne_out(tmp_path: Path) -> None:
    store = BoardStore(tmp_path / "board.sqlite")
    write_events(
        tmp_path / "stories" / "AIR-1" / "events.jsonl",
        [
            {"kind": "amigos_objection", "role": "tester", "part": "changes", "about": "x", "round": 1},
            {"kind": "amigos_objection", "role": "tester", "part": "changes", "about": "y", "round": 2},
            {"kind": "amigos_objection", "role": "architect", "part": "proof", "about": "z", "round": 1},
            {
                "kind": "amigos_borne_out",
                "layer": "01",
                "test": "t",
                "finding": "vacuous_test",
                "role": "tester",
            },
        ],
    )
    amigos = line_stats(tmp_path, store)["amigos"]
    assert amigos["objections"] == {"tester/changes": 2, "architect/proof": 1}
    assert amigos["borne_out"] == 1 and amigos["share_borne_out"] == 1 / 3


def test_hours_and_budgets_are_read_from_the_cards_records(tmp_path: Path) -> None:
    store = BoardStore(tmp_path / "board.sqlite")
    epic = tmp_path / "epics" / "AIR-5"
    epic.mkdir(parents=True)
    (epic / "state.json").write_text(
        json.dumps({"estimate": {"hours": 5.0, "basis": "b", "at": "2026-10-25T00:00:00Z", "actual": 7.5}})
    )
    other = tmp_path / "epics" / "AIR-6"
    other.mkdir(parents=True)
    (other / "state.json").write_text(json.dumps({"estimate": {"hours": 2.0, "basis": "b", "at": "t"}}))
    write_events(
        tmp_path / "stories" / "AIR-1" / "events.jsonl",
        [
            {"kind": "budget_warning", "stage": "layer"},
            {"kind": "budget_exceeded", "stage": "layer"},
            {"kind": "budget_warning", "stage": "refine"},
        ],  # fmt: skip
    )
    stats = line_stats(tmp_path, store)
    assert stats["hours"] == [
        {"key": "AIR-5", "estimate": 5.0, "actual": 7.5},
        {"key": "AIR-6", "estimate": 2.0, "actual": None},
    ]
    assert stats["budgets"] == {
        "layer": {"warnings": 1, "exceeded": 1},
        "refine": {"warnings": 1, "exceeded": 0},
    }


def test_the_stats_print_as_tables(tmp_path: Path) -> None:
    store = BoardStore(tmp_path / "board.sqlite")
    key = make_story(store)
    go(store, key, [("approved", "noah", 10), ("doing", "line", 11), ("review", "line", 50)])
    text = render_stats(line_stats(tmp_path, store))
    assert "UNATTENDED" in text and "1 of 1" in text and "100%" in text
    assert "no blocks" in text and "no objections" in text


def test_the_stats_route(tmp_path: Path) -> None:
    import httpx

    from airlie.serve.app import create_app
    from tests.unit.serve_helpers import TestClient
    from tests.unit.test_serve import FakeUpstream
    from tests.unit.test_serve import cfg as serve_cfg

    c = serve_cfg(tmp_path)
    store = BoardStore(c.observability.state_dir / "board.sqlite")
    key = make_story(store)
    go(store, key, [("approved", "noah", 10), ("doing", "line", 11), ("review", "line", 50)])
    app = create_app(c, transport=httpx.MockTransport(FakeUpstream()), api_key="k")
    auth = {"Authorization": "Bearer k"}
    with TestClient(app) as client:
        body = client.get("/v1/airlie/line/stats", headers=auth).json()
        assert set(body) == {"unattended", "blocks", "hours", "amigos", "budgets"}
        assert body["unattended"] == {"stories": 1, "unattended": 1, "rate": 1.0}
        later = client.get("/v1/airlie/line/stats", params={"since": "2026-12-01"}, headers=auth).json()
        assert later["unattended"]["stories"] == 0  # the review was at epoch 50
        assert (
            client.get("/v1/airlie/line/stats", params={"since": "yesterday"}, headers=auth).status_code
            == 400
        )
        assert client.get("/v1/airlie/line/stats").status_code == 401


def test_line_stats_prints_the_tables_and_since_must_be_a_date(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from airlie import cli
    from tests.unit.test_serve import cfg as serve_cfg

    c = serve_cfg(tmp_path)
    assert c.source_path is not None
    store = BoardStore(c.observability.state_dir / "board.sqlite")
    key = make_story(store)
    go(store, key, [("approved", "noah", 10), ("doing", "line", 11), ("review", "line", 50)])
    runner = CliRunner()
    ok = runner.invoke(cli.app, ["line", "stats", "--config", str(c.source_path)])
    assert ok.exit_code == 0 and "UNATTENDED" in ok.output and "1 of 1" in ok.output
    bad = runner.invoke(cli.app, ["line", "stats", "--since", "soon", "--config", str(c.source_path)])
    assert bad.exit_code == 1 and "since must be a date" in bad.output
