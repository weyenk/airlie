"""What the flow builders share: finding a story's files, reading its studio runs, and the job and card
shapes the web workflow screen draws."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from airlie.serve.jobs import read_events

STAGE_ROLES = {
    "refine": "product",
    "survey": "architect",
    "layers": "architect",
    "package": "architect",
    "meeting": "amigos",
}
"""The role a stage job runs as, when its steps do not say (a run that has not stepped yet)."""

_SAFE = re.compile(r"[A-Za-z0-9._-]+")


class StoryNotFound(LookupError):
    """No story of that key, or no such node or job in it."""


def safe(value: str) -> bool:
    """Whether `value` is one path component that stays inside its parent directory."""
    return _SAFE.fullmatch(value) is not None and value not in (".", "..")


def name(value: str, what: str) -> str:
    """`value` as one path component, or StoryNotFound: it names a directory and must not leave its parent."""
    if not safe(value):
        raise StoryNotFound(f"no {what} '{value}'")
    return value


def load(state_dir: Path, key: str) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    d = state_dir / "stories" / name(key, "story")
    f = d / "state.json"
    if not f.is_file():
        raise StoryNotFound(f"no story '{key}'")
    try:
        st = json.loads(f.read_text())
    except (ValueError, OSError) as e:
        raise StoryNotFound(f"story '{key}' has a damaged state.json: {e}") from e
    if not isinstance(st, dict):
        raise StoryNotFound(f"story '{key}' has a damaged state.json")
    events, _ = read_events(d / "events.jsonl", 0)
    return d, st, events


def run_events(state_dir: Path, run: str | None) -> list[dict[str, Any]]:
    if not run or not safe(run):
        return []
    return read_events(state_dir / "runs" / run / "events.jsonl", 0)[0]


def tokens(events: list[dict[str, Any]], team: str) -> dict[str, Any] | None:
    """What the steps of one subtask used, and the speed they generated at (completion tokens per second of
    generation); None when it took no step."""
    steps = [e for e in events if e.get("kind") == "step" and e.get("team_id") == team]
    if not steps:
        return None
    prompt = sum(int(e.get("prompt_tokens") or 0) for e in steps)
    completion = sum(int(e.get("completion_tokens") or 0) for e in steps)
    gen_s = sum(float(e.get("gen_ms") or 0) for e in steps) / 1000
    prefill_s = sum(float(e.get("prefill_ms") or 0) for e in steps) / 1000
    tool_s = sum(float(e.get("tool_ms") or 0) for e in steps) / 1000
    return {
        "steps": len(steps),
        "prompt": prompt,
        "completion": completion,
        "gen_s": round(gen_s, 1),
        "prefill_s": round(prefill_s, 1),
        "tool_s": round(tool_s, 1),
        "tps": round(completion / gen_s, 1) if gen_s else None,
        "effective_tps": round(completion / (gen_s + prefill_s), 1) if gen_s + prefill_s else None,
        "prefill_tps": round(prompt / prefill_s, 1) if prefill_s else None,
        "role": next((e["role"] for e in steps if e.get("role")), None),
        "model": next((e["model"] for e in steps if e.get("model")), None),
    }


def detail(e: dict[str, Any]) -> str:
    for k in ("error", "why", "reason"):
        if e.get(k):
            return str(e[k])
    if "findings" in e:
        return f"{e['findings']} finding(s)"
    if "attempt" in e:
        return f"attempt {e['attempt']}"
    return ""


def job(
    id: str,
    label: str,
    role: str | None,
    status: str,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "id": id,
        "label": label,
        "role": role,
        "status": status,
        "attempts": 1 if status != "pending" else 0,
        "fix_rounds": 0,
        "retries": [],
        "studio_run": None,
        "started": None,
        "ended": None,
        "duration_s": None,
        "tokens": None,
        **fields,
    }


def status_of(start: dict[str, Any] | None, done: dict[str, Any] | None) -> str:
    if done is not None:
        return "passed" if done.get("ok") else "failed"
    return "running" if start is not None else "pending"


def timing(start: dict[str, Any] | None, done: dict[str, Any] | None) -> dict[str, Any]:
    a, b = (start or {}).get("ts"), (done or {}).get("ts")
    took = round(b - a, 1) if isinstance(a, float | int) and isinstance(b, float | int) else None
    return {"started": a, "ended": b, "duration_s": took}


Shape = Literal["code", "model", "milestone"]
"""How a box is drawn: a step that asks a model, a step airlie takes itself, or a moment."""


def shaped(
    box: dict[str, Any],
    shape: Shape,
    *,
    live: str | None = None,
    tags: Sequence[dict[str, Any]] = (),
    card: dict[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """`box` (a job) with the fields the workflow screen draws a box by: its shape, the live inner step while
    it runs, its warn tags and its hover card."""
    box.update(shape=shape, live=live, tags=list(tags), card=card, **fields)
    return box


Tone = Literal["warn", "info", "ok", "danger", "chip"]


def tag(text: str, tone: Tone) -> dict[str, Any]:
    """A small tag on a box or in its card."""
    return {"text": text, "tone": tone}


def card(
    title: str, rows: list[dict[str, Any]], *, tags: Sequence[dict[str, Any]] = (), foot: str | None = None
) -> dict[str, Any]:
    """The hover card of a box: a title, its rows, its tags and a foot line (who ran it, for how long)."""
    return {"title": title, "rows": rows, "tags": list(tags), "foot": foot}


def squares(label: str, marks: Sequence[str], text: str | None = None) -> dict[str, Any]:
    """A row of small squares, one per round or turn. A mark is a status, `"warn"`, or `"code-passed"` for
    a turn airlie's own checker took."""
    return {"kind": "squares", "label": label, "marks": list(marks), "text": text}


def step_row(status: str, text: str, seconds: float | None = None) -> dict[str, Any]:
    """One inner step in a card: its status, what it was and how long it took."""
    return {"kind": "step", "status": status, "text": text, "seconds": seconds}


def text_row(label: str, text: str) -> dict[str, Any]:
    """A labeled line of plain text in a card."""
    return {"kind": "text", "label": label, "text": text}


def votes(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Each role's last vote on requirements, changes and proof; a row is {role, requirements, changes,
    proof}."""
    return {"kind": "votes", "votes": list(rows)}
