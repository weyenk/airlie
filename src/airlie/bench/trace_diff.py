"""`airlie bench trace-diff A B`: what differs between two story runs' events, prompts and ledger rows.

A and B are bench state dirs (`observability.state_dir`: `stories/<key>/events.jsonl`,
`runs/<run>/events.jsonl`, `runs/<run>/trace/**/prompt.json` and `supervisor.sqlite`). Volatile fields
(times, ids, run ids, paths, shas, pytest's clock and colours) are spelled the same on both sides, and the
node events the composer adds (`node_started`, `node_finished`, `node_skipped`) are dropped, so a driver run
and a composer run of the same story compare clean when they did the same thing (generic nodes Part 2,
"Running the same")."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from airlie.supervisor.ledger import Ledger

VOLATILE = frozenset({"ts", "build", "run_id", "studio_run", "ms", "elapsed_s", "wall_s", "wall_ms"})
"""Times, ids and the build's commit: different on every run."""

TIMING = frozenset(
    {"prompt_tokens", "completion_tokens", "prefill_ms", "gen_ms", "tool_ms", "span_id", "parent_id"}
)
"""A studio run's step and reply events also carry the engine's measured tokens and times, and trace ids."""

NODE_EVENTS = frozenset({"node_started", "node_finished", "node_skipped"})
"""What the composer adds to a run's events; the driver writes none."""


def same(text: str) -> str:
    """`text` with what a re-run changes spelled one way: run ids, commits, pytest's clock and colours."""
    text = re.sub(r"\\u001b\[[0-9;]*m|\x1b\[[0-9;]*m", "", text)
    text = re.sub(r"\b\d{8}-\d{6}-[0-9a-f]{4}\b", "<run>", text)
    text = re.sub(r" in \d+\.\d+s\b", " in <s>", text)
    return re.sub(r"\b[0-9a-f]{40}\b", "<sha>", text)


def _neutral_event(e: Mapping[str, Any]) -> dict[str, Any]:
    kept = {k: v for k, v in e.items() if k not in VOLATILE | TIMING}
    out: dict[str, Any] = json.loads(same(json.dumps(kept)))
    return out


def _first_field(a: Mapping[str, Any], b: Mapping[str, Any]) -> str:
    for k in sorted(set(a) | set(b)):
        if a.get(k, "<absent>") != b.get(k, "<absent>"):
            return f"{k}: {a.get(k, '<absent>')!r} != {b.get(k, '<absent>')!r}"
    return "equal"  # unreachable for unequal events


def _more(what: str, n: int, a: int, b: int) -> str:
    return f"{what} {n}: only {'A' if a > b else 'B'} has {abs(a - b)} more"


def diff_events(a: Sequence[Mapping[str, Any]], b: Sequence[Mapping[str, Any]]) -> list[str]:
    """One line per event that differs (its first differing field) and one for events only one side has;
    node events are dropped from both sides first."""
    xs = [_neutral_event(e) for e in a if e.get("kind") not in NODE_EVENTS]
    ys = [_neutral_event(e) for e in b if e.get("kind") not in NODE_EVENTS]
    lines = []
    for i, (x, y) in enumerate(zip(xs, ys, strict=False)):
        if x == y:
            continue
        if x.get("kind") != y.get("kind"):
            lines.append(f"event {i}: kind {x.get('kind')!r} != {y.get('kind')!r}")
        else:
            lines.append(f"event {i} ({x.get('kind')}): {_first_field(x, y)}")
    if len(xs) != len(ys):
        lines.append(_more("event", min(len(xs), len(ys)), len(xs), len(ys)))
    return lines


def diff_prompts(a: Sequence[str], b: Sequence[str], *, cap: int = 40) -> list[str]:
    """One line per prompt that differs (its first differing line), one for prompts only one side has, at most
    `cap` lines in all."""
    lines: list[str] = []
    for i, (x, y) in enumerate(zip(a, b, strict=False)):
        xl, yl = same(x).splitlines(), same(y).splitlines()
        if xl == yl:
            continue
        n = next((n for n, (p, q) in enumerate(zip(xl, yl, strict=False)) if p != q), min(len(xl), len(yl)))
        p = xl[n] if n < len(xl) else "<end>"
        q = yl[n] if n < len(yl) else "<end>"
        lines.append(f"prompt {i} line {n + 1}: {p[:120]!r} != {q[:120]!r}")
    if len(a) != len(b):
        lines.append(_more("prompt", min(len(a), len(b)), len(a), len(b)))
    if len(lines) > cap:
        lines = [*lines[:cap], f"... {len(lines) - cap} more prompt lines"]
    return lines


def diff_ledger(a: Sequence[tuple[str, str, str]], b: Sequence[tuple[str, str, str]]) -> list[str]:
    """One line per ledger row (trigger, category, outcome) that differs, one for rows only one side has."""
    lines = [f"ledger row {i}: {x} != {y}" for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y]
    if len(a) != len(b):
        lines.append(_more("ledger row", min(len(a), len(b)), len(a), len(b)))
    return lines


@dataclass
class TraceDiff:
    events: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    ledger: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.events or self.prompts or self.ledger)

    def lines(self) -> list[str]:
        return self.events + self.prompts + self.ledger


def _rooted(text: str, root: Path) -> str:
    return text.replace(str(root.resolve()), "<root>").replace(str(root), "<root>")


def _jsonl(path: Path, root: Path) -> list[dict[str, Any]]:
    text = _rooted(path.read_text(), root) if path.exists() else ""
    return [json.loads(x) for x in text.splitlines() if x.strip()]


def _story_dir(state: Path) -> Path:
    stories = state / "stories"
    found = sorted(p for p in stories.iterdir() if p.is_dir()) if stories.is_dir() else []
    if len(found) != 1:
        raise ValueError(f"{state}: expected one story under stories/, found {len(found)}")
    return found[0]


def _events(state: Path) -> list[dict[str, Any]]:
    """The story's events, then each studio run's in order (run ids start with their start time)."""
    out = _jsonl(_story_dir(state) / "events.jsonl", state)
    for f in sorted((state / "runs").glob("*/events.jsonl")):
        out += _jsonl(f, state)
    return out


def _prompts(state: Path) -> list[str]:
    out = []
    for run in sorted((state / "runs").glob("*")):
        for f in sorted((run / "trace").rglob("prompt.json")):
            messages = json.loads(f.read_text()).get("messages", [])
            out.append(_rooted("\n".join(f"[{m.get('role')}] {m.get('content')}" for m in messages), state))
    return out


def _ledger(state: Path) -> list[tuple[str, str, str]]:
    path = state / "supervisor.sqlite"
    if not path.exists():
        return []
    key = _story_dir(state).name
    ledger = Ledger(path)
    try:
        rows = reversed(ledger.recent(limit=1_000_000))  # oldest first
        return [(r.trigger, r.category or "", r.outcome or "") for r in rows if r.story == key]
    finally:
        ledger.close()


def diff_story_runs(a: Path, b: Path, *, prompts: bool = True) -> TraceDiff:
    """Compare two bench state dirs: events (the story's, then each studio run's, paired by order), the studio
    prompts, and the story's ledger rows."""
    return TraceDiff(
        events=diff_events(_events(a), _events(b)),
        prompts=diff_prompts(_prompts(a), _prompts(b)) if prompts else [],
        ledger=diff_ledger(_ledger(a), _ledger(b)),
    )
