"""`airlie controller dataset|train|eval` wiring with a tiny backbone (no downloads)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from airlie import cli
from airlie.lts.controller.learned import tiny_test_backbone

runner = CliRunner()


def setup(tmp_path: Path) -> Path:
    for r in ("coder", "summarizer", "judge", "aggregator"):
        (tmp_path / f"{r}.md").write_text("x")
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://x/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": {
            r: {"prompt": f"{r}.md", "model": "base"} for r in ("coder", "summarizer", "judge", "aggregator")
        },
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    for run in ("r1", "r2", "r3", "r4"):
        rd = tmp_path / "state" / "runs" / run
        (rd / "lts").mkdir(parents=True)
        rows = []
        for team, reward in (("team-1", 1.0), ("team-2", 0.0)):
            for step in (1, 2):
                rows.append(
                    {
                        "run_id": run,
                        "team_id": team,
                        "step": step,
                        "query": "goal",
                        "memory_keys": [],
                        "u_t": {"name": "shell", "arguments": "{}"},
                        "s_t": f"s{step}",
                        "o_t": "out",
                        "decision": step == 1,
                        "p_yes": 0.7,
                        "reason": "r",
                        "entry_id": "m" if step == 1 else None,
                        "controller": "heuristic",
                        "used_later": team == "team-1" and step == 1,
                        "episode_reward": reward,
                        "ts": 1.0,
                    }
                )
        (rd / "lts" / "admission.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        events = [
            {"kind": "run_start", "goal": "goal"},
            {"kind": "team_done", "team_id": "team-1", "ok": True, "ts": 1.0},
            {"kind": "team_done", "team_id": "team-2", "ok": False, "ts": 2.0},
            {"kind": "aggregate", "winner": "team-1", "scores": {"team-1": 1.0, "team-2": 0.0}},
        ]
        (rd / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    return tmp_path / "airlie.yaml"


def test_dataset_train_eval(tmp_path: Path, monkeypatch) -> None:
    cfg = setup(tmp_path)
    monkeypatch.setattr(cli, "_controller_backbone", lambda name, device: tiny_test_backbone())
    r = runner.invoke(cli.app, ["controller", "dataset", "--config", str(cfg)])
    assert r.exit_code == 0, r.output
    ds = tmp_path / "state" / "controller" / "dataset.jsonl"
    assert ds.is_file() and len(ds.read_text().splitlines()) == 16
    r = runner.invoke(
        cli.app,
        ["controller", "train", "--config", str(cfg), "--epochs", "1", "--lora-r", "4", "--device", "cpu"],
    )
    assert r.exit_code == 0, r.output
    ckpt = tmp_path / "state" / "controller" / "latest"
    assert (ckpt / "controller.json").is_file() and "epoch_loss" in r.output
    r = runner.invoke(cli.app, ["controller", "eval", "--config", str(cfg), "--device", "cpu"])
    assert r.exit_code == 0, r.output
    assert "useful_precision" in r.output and "logged" in r.output
