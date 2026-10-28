"""The lists the web app reads from the Studio's state: recent runs (from each run directory's event log),
stories (from each story's state.json) and the repositories a job may be started in."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from airlie.board import BoardStore
from airlie.config import AirlieConfig
from airlie.serve.jobs import read_events
from airlie.story.epic import epic_of
from airlie.story.initiative_state import waiting_on
from airlie.story.model import StoryError
from airlie.story.state import STAGES
from airlie.subproc import NO_HOOKS_ARGS, agent_env

_SAFE = re.compile(r"[A-Za-z0-9._-]+")


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


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:  # not there (yet, or any more)
        return None


def _last_activity(state_dir: Path, story: Path, runs: object) -> float:
    """The newest mtime of the story's events.jsonl and state.json and of the events.jsonl of the last run
    in `runs` (the story's run ids, oldest first): the files a story writes as it works. Files that are
    not there are skipped; a story with none of them has 0.0 (the epoch), so it sorts as the stalest. A run
    id comes from state.json, so it is joined into a path only as one safe name (as flow.py does): any other
    is no run, and its log is not looked for."""
    files = [story / "events.jsonl", story / "state.json"]
    if isinstance(runs, list) and runs:
        last = runs[-1]
        if isinstance(last, str) and _SAFE.fullmatch(last) and last not in (".", ".."):
            files.append(state_dir / "runs" / last / "events.jsonl")
    return max((t for t in map(_mtime, files) if t is not None), default=0.0)


def stories(state_dir: Path) -> list[dict[str, Any]]:
    """Every story directory under `state_dir/stories`, by key, with its stage and blocked reason from
    state.json: a story without one yet has None there, a damaged one says so in `blocked`. `last_activity`
    (seconds since the epoch) is when the story or its last run last wrote (see _last_activity)."""
    root = state_dir / "stories"
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        row: dict[str, Any] = {"key": d.name, "title": None, "repo": None, "stage": None, "blocked": None}
        runs: object = None
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
                runs = st.get("runs")
        row["last_activity"] = _last_activity(state_dir, d, runs)
        out.append(row)
    return out


def epics(state_dir: Path) -> list[dict[str, Any]]:
    """Every epic directory under `state_dir/epics`, by key, with its stage, blocked reason, repository, the
    number of stories made and of re-plans, from epic state.json: a damaged one says so in `blocked`.
    `last_activity` is as for a story (the newer of state.json and events.jsonl)."""
    root = state_dir / "epics"
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        row: dict[str, Any] = {
            "key": d.name,
            "title": None,
            "repo": None,
            "stage": None,
            "blocked": None,
            "stories": 0,
            "replans": 0,
        }
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
                row["stories"] = len(st["stories"]) if isinstance(st.get("stories"), dict) else 0
                row["replans"] = len(st["replans"]) if isinstance(st.get("replans"), list) else 0
        times = [t for t in (_mtime(d / "state.json"), _mtime(d / "events.jsonl")) if t is not None]
        row["last_activity"] = max(times, default=0.0)
        out.append(row)
    return out


def initiatives(state_dir: Path, store: BoardStore) -> list[dict[str, Any]]:
    """Every initiative the specs phase has a record for, by key: its title, stage, how many epics it made,
    their hours together (the cards' own prices, read from the board) and what it waits on. A damaged record
    says so in `blocked`."""
    root = state_dir / "initiatives"
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        row: dict[str, Any] = {
            "key": d.name,
            "title": None,
            "stage": None,
            "blocked": None,
            "epics": 0,
            "hours": 0.0,
            "waiting_on": None,
        }
        f = d / "state.json"
        if f.is_file():
            try:
                st = json.loads(f.read_text())
                if not isinstance(st, dict):
                    raise ValueError("not an object")
                qa = [q for q in st.get("qa") or () if isinstance(q, dict)]
                pending = bool(qa) and qa[-1].get("a") is None
                row["title"] = (st.get("card") or {}).get("title")
                row["stage"], row["blocked"] = st.get("stage"), st.get("blocked")
                row["waiting_on"] = waiting_on(str(st.get("stage")), pending)
                keys = [e["key"] for e in st.get("epics") or () if isinstance(e, dict) and "key" in e]
                row["epics"] = len(keys)
                row["hours"] = round(sum(_hours_of(store, k) for k in keys), 1)
            except (ValueError, OSError, AttributeError, TypeError):
                row["blocked"] = "state.json is damaged"
        times = [t for t in (_mtime(d / "state.json"), _mtime(d / "events.jsonl")) if t is not None]
        row["last_activity"] = max(times, default=0.0)
        out.append(row)
    return out


def _hours_of(store: BoardStore, key: str) -> float:
    """The latest price in epic `key`'s card (revised, else first); 0 for a card with none or gone."""
    try:
        spec = epic_of(store.get(key).description)
    except (KeyError, StoryError):
        return 0.0
    return spec.estimate.latest if spec is not None and spec.estimate is not None else 0.0


def _natural(key: str) -> list[str | int]:
    """`key` as the pieces to compare it by: text (any case) and numbers, so AIR-9 comes before AIR-17. The
    pieces alternate text, number, text, ...: two keys always compare text with text, number with number."""
    return [int(p) if i % 2 else p for i, p in enumerate(re.split(r"(\d+)", key.casefold()))]


def _stage_rank(stage: object) -> int:
    """Where a story's stage sorts: none yet first, then STAGES order, then any stage airlie does not know."""
    if stage is None:
        return -1
    return STAGES.index(stage) if stage in STAGES else len(STAGES)


def story_page(
    rows: list[dict[str, Any]],
    *,
    page: int = 1,
    size: int | None = None,
    stage: str | None = None,
    repo: str | None = None,
    blocked: bool | None = None,
    q: str | None = None,
    sort: Literal["activity", "key", "stage"] = "activity",
    active_from: float | None = None,
    active_to: float | None = None,
) -> dict[str, Any]:
    """The rows of `stories()` the web app asks for: those with this `stage` and `repo`, `blocked` (True: a
    blocked reason is set, False: none is) and with `q` in their key or title (case-insensitively), sorted
    by `sort` (activity: newest first; key: AIR-9 before AIR-17; stage: STAGES order, a story with no stage
    yet first, then newest first; ties fall to the key), then the 1-based `page` of `size` of them. Without
    a `size` it is one page of every row, whatever `page` says. `total` counts the rows after filtering.
    `active_from` and `active_to` (epoch seconds) keep the rows whose `last_activity` is in
    [active_from, active_to)."""
    if page < 1 or (size is not None and size < 1):
        raise ValueError("page and size are at least 1")
    needle = q.casefold() if q else None
    chosen = [
        r
        for r in rows
        if (stage is None or r["stage"] == stage)
        and (repo is None or r["repo"] == repo)
        and (blocked is None or bool(r["blocked"]) is blocked)
        and (active_from is None or r["last_activity"] >= active_from)
        and (active_to is None or r["last_activity"] < active_to)
        and (needle is None or needle in r["key"].casefold() or needle in str(r["title"] or "").casefold())
    ]
    if sort == "key":
        chosen.sort(key=lambda r: _natural(r["key"]))
    elif sort == "stage":
        chosen.sort(key=lambda r: (_stage_rank(r["stage"]), -r["last_activity"], _natural(r["key"])))
    else:
        chosen.sort(key=lambda r: (-r["last_activity"], _natural(r["key"])))
    shown = chosen if size is None else chosen[(page - 1) * size : page * size]
    return {"stories": shown, "total": len(chosen), "page": page, "size": size}


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


_GITHUB_ORIGIN = re.compile(
    r"(?:git@github\.com:|https?://github\.com/|ssh://git@github\.com/)([\w.-]+)/([\w.-]+?)(?:\.git)?/?",
    re.IGNORECASE,
)


def _origin_name(checkout: Path) -> str | None:
    """`owner/name` of the checkout's `origin` remote when it is on GitHub; None for no origin, another host,
    a timeout (5 s) or no git."""
    try:
        done = subprocess.run(
            ["git", *NO_HOOKS_ARGS, "-C", str(checkout), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=agent_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = _GITHUB_ORIGIN.fullmatch(done.stdout.strip()) if done.returncode == 0 else None
    return f"{m.group(1)}/{m.group(2)}" if m else None


def repo_names(cfg: AirlieConfig, labels: Iterable[str]) -> list[str]:
    """The `owner/name` of every repository a story may name: the `origin` remote (GitHub, in its git@,
    https and ssh:// forms) of `story.repo` and of each git checkout directly under `story.repos_root`, and
    every `repo:<owner>/<name>` label in `labels`. Sorted, unique; a checkout with no origin or a
    non-GitHub one is skipped."""
    names = {n for path in repos(cfg) if (n := _origin_name(Path(path))) is not None}
    for label in labels:
        if label.startswith("repo:") and re.fullmatch(r"[\w.-]+/[\w.-]+", label[5:]):
            names.add(label[5:])
    return sorted(names)
