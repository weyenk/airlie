"""What Muse reads: the tail of the role's trace, the layer's events, and the one triage message that
carries them with the findings and the earlier interventions.

Everything here is cut keeping the END: the last steps and the last lines are what show how a role got
stuck, and the start of a long trace is the part least likely to matter."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from airlie.supervisor.ledger import Row

_BRIEF_CAP = 8_000
_DIFF_CAP = 20_000
_RECALL_ROW_CAP = 600
_EARLIER_CAP = 4_000
_REASONING_TAIL = 1_500
_OBSERVATION_HEAD = 300
_STEP_FILE = re.compile(r"step-(\d+)\.json$")
_ROUND_DIR = re.compile(r"round-(\d+)$")


def _tail(text: str, chars: int) -> str:
    return text if len(text) <= chars else text[-chars:]


def _head(text: str, chars: int) -> str:
    return text if len(text) <= chars else text[:chars]


def _newest_step_folder(subtask_dir: Path) -> Path | None:
    """The folder of the latest attempt: `<subtask>/` is round 0, `round-N/` under it is attempt N."""
    candidates: list[tuple[int, Path]] = []
    if any(subtask_dir.glob("step-*.json")):
        candidates.append((0, subtask_dir))
    for d in subtask_dir.glob("round-*"):
        m = _ROUND_DIR.fullmatch(d.name)
        if m and d.is_dir() and any(d.glob("step-*.json")):
            candidates.append((int(m.group(1)), d))
    return max(candidates, key=lambda c: c[0])[1] if candidates else None


def _render_step(n: int, data: dict[str, object]) -> str:
    lines = [f"step {n}"]
    reasoning = str(data.get("reasoning") or "")
    if reasoning:
        lines.append(f"reasoning: {_tail(reasoning, _REASONING_TAIL)}")
    text = str(data.get("assistant_text") or "")
    if text:
        lines.append(f"said: {text}")
    call = data.get("u_t")
    if isinstance(call, dict) and call.get("name"):
        args = json.dumps(call.get("arguments"), ensure_ascii=False, sort_keys=True)
        lines.append(f"tool: {call['name']} {args}")
        lines.append(f"observed: {_head(str(data.get('o_t') or ''), _OBSERVATION_HEAD)}")
    return "\n".join(lines)


def trace_tail(
    trace_dir: Path, subtask: str, steps: int, chars: int = 12_000, *, round_: int | None = None
) -> str:
    """The last `steps` steps of the role's newest attempt on `subtask`, or "" when it left no trace.
    `round_` reads that attempt instead (0 is the first run): a replay asks what Muse read at the time, before
    the retries that came after it."""
    if round_ is None:
        folder = _newest_step_folder(trace_dir / subtask)
    else:
        folder = trace_dir / subtask if round_ == 0 else trace_dir / subtask / f"round-{round_}"
    if folder is None or not folder.is_dir():
        return ""
    files = sorted(
        (int(m.group(1)), f) for f in folder.glob("step-*.json") if (m := _STEP_FILE.fullmatch(f.name))
    )[-steps:]
    parts: list[str] = []
    for n, f in files:
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            parts.append(_render_step(n, data))
    return _tail("\n\n".join(parts), chars)


def layer_events(events: Path, layer: str, chars: int = 6_000) -> str:
    """The `events.jsonl` lines that belong to `layer`, one compact JSON per line."""
    try:
        raw = events.read_text().splitlines()
    except OSError:
        return ""
    kept: list[str] = []
    for line in raw:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("layer") == layer:
            kept.append(json.dumps(ev, ensure_ascii=False, separators=(",", ":")))
    return _tail("\n".join(kept), chars)


@dataclass(frozen=True)
class TriageInput:
    trigger: str
    role: str
    subtask: str
    told: str  # today's message
    evidence: str  # findings, or the gate's tail
    kinds: tuple[str, ...]
    brief: str
    trace: str
    diff: str
    events: str
    earlier: tuple[Row, ...]
    recall: tuple[Row, ...]


def _block(r: Row) -> str:
    what = f"fallback:{r.fallback}" if r.fallback else (r.category or "none")
    return (
        f"- {r.trigger} | {what} | owner {r.owner or 'none'} | diagnosis: {r.diagnosis or ''} | "
        f"message: {r.message or ''} | outcome: {r.outcome or 'unknown'}"
    )


def _earlier(rows: tuple[Row, ...]) -> str:
    """Oldest first, the newest kept when the section is over its cap."""
    blocks = [_block(r) for r in sorted(rows, key=lambda r: r.id)]
    kept: list[str] = []
    size = 0
    for b in reversed(blocks):
        size += len(b) + 1
        if kept and size > _EARLIER_CAP:
            break
        kept.append(b)
    return _tail("\n".join(reversed(kept)), _EARLIER_CAP)


def triage_message(inp: TriageInput) -> str:
    """The single user message Muse is asked about; empty sections are left out."""
    trigger = f"{inp.trigger} (role: {inp.role}, subtask: {inp.subtask}"
    if inp.kinds:
        trigger += f", kinds: {', '.join(inp.kinds)}"
    trigger += ")"
    sections = [
        ("Trigger", trigger),
        ("What the role is about to be told", inp.told),
        ("Findings", inp.evidence),
        ("The layer's brief", _head(inp.brief, _BRIEF_CAP)),
        ("The role's recent steps", inp.trace),
        ("The diff", _head(inp.diff, _DIFF_CAP)),
        ("This layer's events", inp.events),
        ("Earlier on this layer", _earlier(inp.earlier)),
        (
            "Fixes that helped before",
            "\n".join(_head(_block(r), _RECALL_ROW_CAP) for r in inp.recall),
        ),
    ]
    return "\n\n".join(f"## {title}\n{body}" for title, body in sections if body.strip())
