"""The lists the web app reads from the Studio's state: recent runs (from each run directory's event log),
stories (from each story's state.json) and the repositories a job may be started in."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from airlie.config import AirlieConfig
from airlie.serve.jobs import read_events


def recent_runs(state_dir: Path, limit: int = 50) -> list[dict[str, Any]]:
    """The newest `limit` run directories under `state_dir/runs` (ids sort by time), each with what its
    `run_start` (goal, repo, started) and `run_done` (ok, wall_ms) events say: a run with no event log, or
    one still being written, has None where its events have not said yet. `kind` is "plan" for a studio
    plan, else "run"."""
    runs = state_dir / "runs"
    if not runs.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted((p for p in runs.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)[:limit]:
        row: dict[str, Any] = {
            "id": d.name,
            "goal": None,
            "repo": None,
            "kind": "run",
            "started": None,
            "ok": None,
            "wall_ms": None,
        }
        events, _ = read_events(d / "events.jsonl", 0)
        for e in events:
            if e.get("kind") == "run_start":
                row["goal"], row["repo"], row["started"] = e.get("goal"), e.get("repo"), e.get("ts")
                row["kind"] = "plan" if e.get("mode") == "plan" else "run"
            elif e.get("kind") == "run_done":
                row["ok"], row["wall_ms"] = e.get("ok"), e.get("wall_ms")
        out.append(row)
    return out


def stories(state_dir: Path) -> list[dict[str, Any]]:
    """Every story directory under `state_dir/stories`, by key, with its stage and blocked reason from
    state.json: a story without one yet has None there, a damaged one says so in `blocked`."""
    root = state_dir / "stories"
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        row: dict[str, Any] = {"key": d.name, "title": None, "repo": None, "stage": None, "blocked": None}
        f = d / "state.json"
        if f.is_file():
            try:
                st = json.loads(f.read_text())
                if not isinstance(st, dict):
                    raise ValueError("not an object")
            except (ValueError, OSError):
                row["blocked"] = "state.json is damaged"
            else:
                card: dict[str, Any] = st["card"] if isinstance(st.get("card"), dict) else {}
                row["title"] = card.get("title")
                row["repo"], row["stage"], row["blocked"] = st.get("repo"), st.get("stage"), st.get("blocked")
        out.append(row)
    return out


def repos(cfg: AirlieConfig) -> list[str]:
    """Where a job may be started: `story.repo` when set, then every git checkout directly under
    `story.repos_root`, by name (case-insensitively). Paths, since POST /runs takes a path on the Studio."""
    out: list[str] = []
    if cfg.story.repo is not None:
        out.append(str(cfg.story.repo))
    root = cfg.story.repos_root
    if root.is_dir():
        for d in sorted((p for p in root.iterdir() if (p / ".git").exists()), key=lambda p: p.name.lower()):
            if str(d) not in out:
                out.append(str(d))
    return out
