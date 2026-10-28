"""The numbers that judge whether stories go through unattended (planning-line P5b, D9, 2026-10-25).

`line_stats` reads what the line already records: the board's `moves` (who changed each card's status) and the
cards' comments, the diagnoses blocks wrote, the line's own log, the epics' estimates and every card's events.
It computes:

- the **unattended rate**: of the stories that reached review, how many went from their first approval to
  review with no person's move or comment between (the line's own moves and notes are not a person's);
- **blocks** by class and kind, how many fix cards the line filed, how many stories resumed after a fix;
- **hours**: each epic's estimate against what it took;
- the **amigos**' objections by role and part, and how many were borne out by a later check;
- the **budgets**' warnings and overruns by stage.

`since` is a time (epoch seconds): only what happened from then on counts."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from airlie.board import BoardStore, Item
from airlie.story.board import NOTE_PREFIX
from airlie.story.costs import read_estimate
from airlie.story.diagnosis import read_diagnosis

LINE = "line"
"""Who the tick and the pipeline are on the board's `moves`."""
FIX = "fix"


def _events(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def _since(events: list[dict[str, Any]], since: float | None) -> list[dict[str, Any]]:
    if since is None:
        return events
    return [e for e in events if float(e.get("ts") or 0) >= since]


def _is_person(author: str, text: str, roles: frozenset[str]) -> bool:
    """A comment of a person's: not airlie's, not a role's, no airlie note."""
    return author != "airlie" and author not in roles and not text.startswith(NOTE_PREFIX)


def _unattended(store: BoardStore, since: float | None, roles: frozenset[str]) -> dict[str, Any]:
    stories = 0
    clean = 0
    for item in store.list(level="story"):
        moves = store.moves(item.key)
        reviews = [m for m in moves if m.to_status == "review"]
        if not reviews:
            continue
        review = reviews[-1]
        if since is not None and review.at < since:
            continue
        approvals = [m for m in moves if m.to_status == "approved" and m.at <= review.at]
        # from the first approval: a person's re-approval after a stop is a person's act, and counts
        start = approvals[0].at if approvals else moves[0].at
        stories += 1
        touched = any(m.by != LINE and start < m.at < review.at for m in moves) or any(
            _is_person(c.author, c.text, roles) and start < c.at < review.at for c in item.comments
        )
        clean += 0 if touched else 1
    return {"stories": stories, "unattended": clean, "rate": clean / stories if stories else None}


def _blocks(state_dir: Path, store: BoardStore, since: float | None) -> dict[str, Any]:
    by_class: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    stories = state_dir / "stories"
    for folder in sorted(stories.glob("*")) if stories.is_dir() else []:
        for path in sorted((folder / "diagnosis").glob("*.json")) if (folder / "diagnosis").is_dir() else []:
            if not path.stem.isdigit():
                continue
            if since is not None and path.stat().st_mtime < since:
                continue
            d = read_diagnosis(state_dir, folder.name, int(path.stem))
            if d is not None:
                by_class[d.classified.class_] += 1
                by_kind[d.classified.kind] += 1
    fixes = [
        i for i in store.list(level="story") if FIX in i.labels and (since is None or i.created_at >= since)
    ]
    resumed = [
        e
        for e in _since(_events(state_dir / "line" / "events.jsonl"), since)
        if e.get("kind") == "resumed_after_fix"
    ]
    return {
        "by_class": dict(by_class.most_common()),
        "by_kind": dict(by_kind.most_common()),
        "fix_cards_filed": len(fixes),
        "resumed_after_fix": len(resumed),
    }


def _hours(state_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    epics = state_dir / "epics"
    for folder in sorted(epics.glob("*")) if epics.is_dir() else []:
        estimate = read_estimate(state_dir, folder.name)
        if estimate is not None:
            out.append({"key": folder.name, "estimate": estimate.hours, "actual": estimate.actual})
    return out


def _cards(state_dir: Path) -> list[Path]:
    found: list[Path] = []
    for kind in ("stories", "epics", "initiatives"):
        root = state_dir / kind
        if root.is_dir():
            found += sorted(p for p in root.glob("*") if (p / "events.jsonl").is_file())
    return found


def _events_of_cards(state_dir: Path, since: float | None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for folder in _cards(state_dir):
        events += _since(_events(folder / "events.jsonl"), since)
    return events


def _amigos(events: list[dict[str, Any]]) -> dict[str, Any]:
    objections: Counter[str] = Counter()
    borne = 0
    for e in events:
        if e.get("kind") == "amigos_objection":
            objections[f"{e.get('role')}/{e.get('part')}"] += 1
        elif e.get("kind") == "amigos_borne_out":
            borne += 1
    total = sum(objections.values())
    return {
        "objections": dict(objections.most_common()),
        "borne_out": borne,
        "share_borne_out": borne / total if total else None,
    }


def _budgets(events: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for e in events:
        kind = e.get("kind")
        if kind in ("budget_warning", "budget_exceeded") and isinstance(e.get("stage"), str):
            row = out.setdefault(e["stage"], {"warnings": 0, "exceeded": 0})
            row["warnings" if kind == "budget_warning" else "exceeded"] += 1
    return out


def line_stats(
    state_dir: Path, store: BoardStore, *, since: float | None = None, roles: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Everything `task line:stats` prints, as data. `roles` are the configured role names, whose comments
    are the line's notes and not a person's."""
    events = _events_of_cards(state_dir, since)
    return {
        "unattended": _unattended(store, since, roles),
        "blocks": _blocks(state_dir, store, since),
        "hours": _hours(state_dir),
        "amigos": _amigos(events),
        "budgets": _budgets(events),
    }


def _hours_text(actual: float | None) -> str:
    return "-" if actual is None else f"{actual:.1f}"


def parse_since(text: str) -> float:
    """A time from a person's `--since`: epoch seconds, or an ISO date or datetime (`2026-10-18`). ValueError
    in words for anything else."""
    from datetime import datetime

    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        raise ValueError(f"since must be a date like 2026-10-18 (or epoch seconds), not {text!r}") from None


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{round(x * 100)}%"


def render_stats(stats: dict[str, Any]) -> str:
    """The stats as plain tables, for the terminal and the log."""
    u, b, a = stats["unattended"], stats["blocks"], stats["amigos"]
    lines = [
        "UNATTENDED",
        f"  {u['unattended']} of {u['stories']} stories went from approval to review untouched "
        f"({_pct(u['rate'])})",
        "",
        "BLOCKS",
    ]
    if b["by_class"]:
        lines += [f"  class {k:<12} {v}" for k, v in b["by_class"].items()]
        lines += [f"  kind  {k:<24} {v}" for k, v in b["by_kind"].items()]
        lines.append(
            f"  fix cards filed {b['fix_cards_filed']}, stories resumed after a fix {b['resumed_after_fix']}"
        )
    else:
        lines.append("  no blocks")
    lines += ["", "HOURS (estimate / actual)"]
    lines += [
        f"  {h['key']:<8} {h['estimate']:>6.1f} / {_hours_text(h['actual'])}" for h in stats["hours"]
    ] or ["  no epic has an estimate"]
    lines += ["", "AMIGOS"]
    if a["objections"]:
        lines += [f"  {k:<22} {v}" for k, v in a["objections"].items()]
        lines.append(f"  borne out {a['borne_out']} ({_pct(a['share_borne_out'])} of the objections)")
    else:
        lines.append("  no objections")
    lines += ["", "BUDGETS (warnings / exceeded)"]
    lines += [f"  {k:<10} {v['warnings']} / {v['exceeded']}" for k, v in stats["budgets"].items()] or [
        "  no stage ran over"
    ]
    return "\n".join(lines)


__all__ = ["Item", "line_stats", "parse_since", "render_stats"]
