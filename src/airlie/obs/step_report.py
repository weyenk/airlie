"""The steps each story role spends before its first write, read back from a story's run traces: how many
went to reading, how many of those paged through a file or read one again, and how many read a file the
layer's brief already names. It measures what giving the roles their context up front saves."""

from __future__ import annotations

import ast
import json
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from airlie.story.context import named_paths
from airlie.teams.worktree import find_file

READ_TOOLS = frozenset({"read_file", "list_dir", "grep"})
WRITE_TOOLS = frozenset({"write_file", "edit_file"})
ROUND_MARK = "/round-"
"""A trace folder with this in its name is a fix round; the subtask's own folder is its first conversation."""


@dataclass(frozen=True)
class Segment:
    """One conversation of one role: a trace folder of one run."""

    story: str
    run: str
    layer: str | None
    seg: str
    role: str
    steps: int
    first_write: int | None
    reads_before_write: int
    pages: int
    repeats: int
    brief_file_reads: int
    max_steps_hit: bool


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _call(step: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """The tool name and arguments of a step; `u_t` is the repr of a dict whose `arguments` is JSON."""
    try:
        call = ast.literal_eval(step["u_t"]) if isinstance(step["u_t"], str) else step["u_t"]
        name = str(call.get("name", ""))
        raw = call.get("arguments")
    except (ValueError, SyntaxError, KeyError, AttributeError):
        return "", {}
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return name, {}
    return name, args if isinstance(args, dict) else {}


def _offset(args: Mapping[str, Any]) -> int:
    try:
        return int(args.get("offset") or 0)
    except (TypeError, ValueError):
        return 0


def _step_files(folder: Path) -> list[Path]:
    return sorted(folder.glob("step-*.json"), key=lambda p: int(p.stem.removeprefix("step-")))


def _exists_at(repo: Path, sha: str | None, path: str) -> bool:
    return bool(sha) and find_file(repo, str(sha), path)[0] != "missing"


def _segment(
    story: str,
    run: str,
    layer: str | None,
    seg: str,
    role: str,
    calls: list[tuple[str, dict[str, Any]]],
    brief_paths: set[str],
    cap: int | None,
) -> Segment:
    first_write = next((i for i, (name, _) in enumerate(calls) if name in WRITE_TOOLS), None)
    before = calls if first_write is None else calls[:first_write]
    offsets: dict[str, list[int]] = defaultdict(list)
    pages = repeats = brief_reads = 0
    for name, args in before:
        if name != "read_file":
            continue
        path = str(args.get("path") or "").lstrip("./")
        offset = _offset(args)
        seen = offsets[path]
        if seen and offset > max(seen):
            pages += 1
        elif seen:
            repeats += 1
        seen.append(offset)
        if path in brief_paths:
            brief_reads += 1
    return Segment(
        story=story,
        run=run,
        layer=layer,
        seg=seg,
        role=role,
        steps=len(calls),
        first_write=None if first_write is None else first_write + 1,
        reads_before_write=sum(1 for name, _ in before if name in READ_TOOLS),
        pages=pages,
        repeats=repeats,
        brief_file_reads=brief_reads if layer else 0,
        max_steps_hit=cap is not None and len(calls) >= cap,
    )


def story_segments(state_dir: Path, story: str, repo: Path, max_steps: Mapping[str, int]) -> list[Segment]:
    """Every conversation of `story`'s runs, from `state_dir`. `max_steps` maps a role to its step cap; a
    role not in it never counts as at the cap. A run with no directory is skipped."""
    story_dir = state_dir / "stories" / story
    state = json.loads((story_dir / "state.json").read_text())
    layers: list[dict[str, Any]] = state.get("layers") or []
    starts = {
        e["studio_run"]: e
        for e in _jsonl(story_dir / "events.jsonl")
        if e.get("kind") == "layer_start" and e.get("studio_run")
    }
    out: list[Segment] = []
    for run in state.get("runs") or []:
        run_dir = state_dir / "runs" / run
        if not (run_dir / "events.jsonl").exists():
            continue
        roles = {
            e["subtask"]: e["role"]
            for e in _jsonl(run_dir / "events.jsonl")
            if e.get("kind") == "subtask_start"
        }
        start = starts.get(run)
        layer = str(start["layer"]) if start else None
        base = start.get("base_sha") if start else None
        index = int(layer) - 1 if layer else -1
        brief_paths: set[str] = set()
        if 0 <= index < len(layers):
            text = f"{layers[index].get('scope', '')}\n{layers[index].get('brief', '')}"
            brief_paths = {p for p in named_paths(text) if _exists_at(repo, base, p)}
        trace = run_dir / "trace"
        folders = sorted(p for p in trace.rglob("*") if p.is_dir()) if trace.exists() else []
        for folder in folders:
            files = _step_files(folder)
            if not files:
                continue
            seg = folder.relative_to(trace).as_posix()
            subtask = seg.split("/")[0]
            role = roles.get(subtask, subtask)
            calls = [_call(json.loads(f.read_text())) for f in files]
            out.append(_segment(story, run, layer, seg, role, calls, brief_paths, max_steps.get(role)))
    return out


def _median(values: Sequence[int]) -> str:
    return f"{statistics.median(values):g}" if values else "-"


def render(segments: Sequence[Segment]) -> str:
    """A Markdown table with a row per role for its first conversations and one for its fix rounds."""
    header = (
        "| role | conversations | n | median first write | at step cap | median reads before write "
        "| pages | repeats | brief-file reads |\n|---|---|---|---|---|---|---|---|---|\n"
    )
    groups: dict[tuple[str, str], list[Segment]] = defaultdict(list)
    for s in segments:
        groups[(s.role, "fix rounds" if ROUND_MARK in s.seg else "first")].append(s)
    rows = []
    for (role, kind), segs in sorted(groups.items()):
        writes = [s.first_write for s in segs if s.first_write is not None]
        rows.append(
            f"| {role} | {kind} | {len(segs)} | {_median(writes)} | {sum(s.max_steps_hit for s in segs)} "
            f"| {_median([s.reads_before_write for s in segs])} | {sum(s.pages for s in segs)} "
            f"| {sum(s.repeats for s in segs)} | {sum(s.brief_file_reads for s in segs)} |"
        )
    return header + "\n".join(rows) + "\n"
