"""What the build lane is doing, for the planning lane to read (planning-line P5b, D2 and N5), and the pause
switch.

The two lanes may be different processes (the serve ticker, the launchd tick, `task story`), so the build
lane writes its phase to a file, `stories/build.phase`: `decoding` while a role's turn is in flight, `gap`
during the gate, the review, the publish and between layers, `idle` with no story building. The planning
lane's long turns (product's and the architect's turns, the meeting's) start only in a `gap` or when
`idle`; a turn that has started runs to its end. Its short asks (consultations, surveys, reads, renders,
the checker) ride beside the build's decode, and how many at once is governed by the decode rate the
build's last steps showed (`fanout`).

A file is stale when no one has touched it for `stale_s`: a build that died mid-decode must not hold the
planning lane forever, and a live build refreshes the file while it decodes (`decoding`).

This module imports nothing from the factory or the pipelines: the asker and the pipeline use it, and the
line (`line.py`) re-exports it."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

Phase = Literal["decoding", "gap", "idle"]
Emit = Callable[..., None]

PHASE_FILE = "build.phase"
HEARTBEAT_S = 60.0
"""A decoding build touches its phase file this often, so a stale file means a build that is gone."""
STALE_S = 600.0
WINDOW = 5
"""The build run's last steps the decode rate is read from."""

PAUSE_FILE = "line.paused"


def phase_path(stories_dir: Path) -> Path:
    return stories_dir / PHASE_FILE


def write_phase(
    stories_dir: Path, phase: Phase, *, run_dir: Path | None = None, now: float | None = None
) -> None:
    """Record the build lane's phase: `{"phase", "ts", "run_dir"}`, written whole and moved into place so a
    reader never sees half of it. Never raises: a phase file that cannot be written leaves the planning lane
    reading `idle`, which is the old behaviour."""
    body = {
        "phase": phase,
        "ts": time.time() if now is None else now,
        "run_dir": str(run_dir) if run_dir else None,
    }
    with contextlib.suppress(OSError):
        stories_dir.mkdir(parents=True, exist_ok=True)
        tmp = stories_dir / f"{PHASE_FILE}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(body))
        tmp.replace(phase_path(stories_dir))


def _read(stories_dir: Path) -> dict[str, object] | None:
    try:
        data = json.loads(phase_path(stories_dir).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_phase(
    stories_dir: Path, *, stale_s: float = STALE_S, now: float | None = None
) -> tuple[Phase, float]:
    """The build lane's phase and how old that record is, in seconds. `("idle", age)` when the file is
    missing, unreadable or stale (older than `stale_s`): a build that wrote `decoding` and died is not one."""
    data = _read(stories_dir)
    if data is None:
        return "idle", 0.0
    phase = data.get("phase")
    ts = data.get("ts")
    if phase not in ("decoding", "gap", "idle") or not isinstance(ts, int | float):
        return "idle", 0.0
    age = max(0.0, (time.time() if now is None else now) - float(ts))
    if age > stale_s:
        return "idle", age
    return phase, age


async def wait_for_gap(
    stories_dir: Path,
    *,
    poll_s: float = 2.0,
    emit: Emit,
    stale_s: float = STALE_S,
) -> float:
    """Wait until the build lane is in a `gap` or `idle`, polling its phase file every `poll_s`; the seconds
    waited. A wait that began emits `lane_yield` at its start, and `lane_wait` (the seconds) and
    `lane_resume` when it ends. Nothing waits when the
    lane is already in a gap or idle."""
    started = time.monotonic()
    waited = False
    while read_phase(stories_dir, stale_s=stale_s)[0] == "decoding":
        if not waited:
            emit("lane_yield")  # the planning lane gives way to the build's decode
        waited = True
        await asyncio.sleep(poll_s)
    seconds = time.monotonic() - started
    if waited:
        emit("lane_wait", seconds=round(seconds, 1))
        emit("lane_resume")
    return seconds


def decode_state(stories_dir: Path, *, stale_s: float = STALE_S) -> dict[str, float | int] | None:
    """How the build lane is decoding right now: `{"step", "tps"}` from the last `WINDOW` `step` events of the
    studio run it named in the phase file (the step number of the latest, the completion tokens over the
    generation seconds of the window). None when the build is not decoding, names no run, or the run has no
    step yet."""
    phase, _ = read_phase(stories_dir, stale_s=stale_s)
    if phase != "decoding":
        return None
    run_dir = (_read(stories_dir) or {}).get("run_dir")
    if not isinstance(run_dir, str):
        return None
    try:
        lines = (Path(run_dir) / "events.jsonl").read_text().splitlines()
    except OSError:
        return None
    steps = []
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("kind") == "step":
            steps.append(event)
            if len(steps) == WINDOW:
                break
    tokens = sum(float(e.get("completion_tokens") or 0) for e in steps)
    gen_ms = sum(float(e.get("gen_ms") or 0) for e in steps)
    if not steps or gen_ms <= 0:
        return None
    return {"step": int(steps[0].get("step") or 0), "tps": round(tokens / (gen_ms / 1000.0), 1)}


def fanout(stories_dir: Path, *, width: int, floor_tps: float, stale_s: float = STALE_S) -> int:
    """How many short asks may run at once: `width` while the build lane is idle or in a gap, or while its
    last five steps decoded at `floor_tps` tokens a second or better; 1 under the floor (the riders would
    slow the build). A run with no steps yet, or none that can be read, is `width`."""
    state = decode_state(stories_dir, stale_s=stale_s)
    if state is None:
        return width
    return 1 if state["tps"] < floor_tps else width


@asynccontextmanager
async def decoding(stories_dir: Path, run_dir: Path | None) -> AsyncIterator[None]:
    """The build lane decoding: the phase is `decoding` for the block (touched every `HEARTBEAT_S` so a live
    build is never stale), and `gap` after it, even when it raises."""
    write_phase(stories_dir, "decoding", run_dir=run_dir)

    async def beat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            write_phase(stories_dir, "decoding", run_dir=run_dir)

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        write_phase(stories_dir, "gap", run_dir=None)


# ---- the pause switch (D7) ----


def paused(state_dir: Path) -> bool:
    """Whether the line is paused: `<state_dir>/line.paused` exists."""
    return (state_dir / PAUSE_FILE).exists()


def set_paused(state_dir: Path, value: bool) -> None:
    """Pause (write the file) or resume (remove it). A beat that finds it does the gates and the bookkeeping
    and starts no job; a running job finishes."""
    path = state_dir / PAUSE_FILE
    if value:
        state_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(str(time.time()))
    else:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
