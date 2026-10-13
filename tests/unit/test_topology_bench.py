"""bench:topology — single vs teams vs fork; verdict against the single-agent baseline (design §9)."""

from __future__ import annotations

from pathlib import Path

import yaml

from airlie.bench.topology_bench import TopoRow, apply_topology, render, summarize, verdict
from airlie.config import load_config


def cfg(tmp_path: Path):  # type: ignore[no-untyped-def]
    for r in ("coder", "summarizer", "judge", "aggregator"):
        (tmp_path / f"{r}.md").write_text("x")
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://x/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": {
            r: {"prompt": f"{r}.md", "model": "base"} for r in ("coder", "summarizer", "judge", "aggregator")
        },
        "teams": {"k": 3},
    }
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


def test_apply_topology(tmp_path: Path) -> None:
    base = cfg(tmp_path)
    s = apply_topology(base, "single")
    assert (s.topology.kind, s.teams.k, s.lts.enabled) == ("single", 1, False)
    t = apply_topology(base, "teams")
    assert (t.topology.kind, t.teams.k, t.lts.enabled) == ("teams", 3, True)
    f = apply_topology(base, "fork")
    assert f.topology.kind == "fork"
    assert base.teams.k == 3 and base.topology.kind == "teams"  # untouched


def row(topo: str, rep: int, wall: float, passed: bool, forks: int = 0) -> TopoRow:
    return TopoRow("t", topo, rep, wall, 10, 100, 50, forks, forks, passed, passed, 1 + 2 * forks, f"r{rep}")


def test_summary_and_verdict() -> None:
    rows = [
        row("single", 0, 100, True),
        row("single", 1, 300, False),
        row("fork", 0, 60, True, 1),
        row("fork", 1, 200, True, 2),
    ]
    s = summarize(rows)
    assert s["single"]["mean_wall_s"] == 200 and s["single"]["mean_wall_solved_s"] == 100
    assert s["single"]["pass_rate"] == 0.5
    assert s["fork"]["mean_forks"] == 1.5 and s["fork"]["pass_rate"] == 1.0
    v = verdict(s, baseline="single", candidate="fork", target_pct=25.0)
    assert v.met and "MET" in v.text
    text = render(s, v)
    assert "| topology |" in text and "fork" in text


def test_apply_topology_single_sets_the_single_kind(tmp_path: Path) -> None:
    s = apply_topology(cfg(tmp_path), "single")
    assert (s.topology.kind, s.teams.k, s.lts.enabled) == ("single", 1, False)


def test_apply_topology_studio_and_review_counts(tmp_path: Path) -> None:
    import json

    from airlie.bench.topology_bench import events_counts

    s = apply_topology(cfg(tmp_path), "studio")
    assert s.topology.kind == "studio"
    ev = tmp_path / "events.jsonl"
    rows = [
        {"kind": "review", "verdict": "request_changes", "blocking": 2},
        {"kind": "fix_round"},
        {"kind": "review", "verdict": "approve", "blocking": 0},
        {"kind": "step"},
    ]
    ev.write_text("\n".join(json.dumps(e) for e in rows))
    c = events_counts(ev)
    assert c["reviews"] == 2 and c["blocking"] == 2 and c["fix_rounds"] == 1 and c["steps"] == 1
