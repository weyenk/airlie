"""The asker's manners toward the build lane (planning-line P5b, D2 and N5, 2026-10-25): a long turn of the
planning lane starts in the build lane's gap, a short ask goes at once, the short asks fan out four wide and
one wide under the decode floor, and a stale phase file is no phase."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from airlie.story import asking, phase
from airlie.story.asking import Asker
from airlie.story.phase import fanout, read_phase, wait_for_gap, write_phase
from tests.unit.test_studio_orchestrator import cfg_for


class Record:
    """What an ask records on: a story's state, in memory."""

    def __init__(self, path: Path) -> None:
        self.runs: list[str] = []
        self.card: dict[str, Any] = {"key": "AIR-1", "title": "Avatars"}
        self.base: dict[str, str] | None = {"ref": "origin/main", "sha": "a" * 40}
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.path = path

    def save(self) -> None: ...

    def emit(self, kind: str, **fields: Any) -> None:
        self.events.append((kind, fields))


class Studio:
    """A studio that answers, and notes when each turn started and ended."""

    def __init__(self, tmp_path: Path, log: list[str], during: Any = None) -> None:
        self.run_dir = SimpleNamespace(run_id=f"run-{len(log)}", path=tmp_path / f"run-{len(log)}")
        self.log = log
        self.during = during

    async def execute_task(self, *args: Any, **kwargs: Any) -> Any:
        self.log.append("start")
        if self.during is not None:
            await self.during()
        self.log.append("end")
        return SimpleNamespace(teams=[SimpleNamespace(stop_reason="ok", ok=True, final_text="answer")])


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    real = phase.wait_for_gap

    async def quick(stories_dir: Path, *, poll_s: float = 2.0, **kw: Any) -> float:
        return await real(stories_dir, poll_s=0.01, **kw)

    monkeypatch.setattr(asking, "wait_for_gap", quick)


def asker_for(tmp_path: Path, log: list[str], during: Any = None) -> tuple[Asker, Record]:
    cfg = cfg_for(tmp_path)
    asker = Asker(cfg, lambda: Studio(tmp_path, log, during), tmp_path)  # type: ignore[arg-type, return-value]
    (tmp_path / "repo").mkdir(exist_ok=True)
    asker.repo = tmp_path / "repo"
    return asker, Record(tmp_path)


async def test_a_long_turn_waits_for_the_build_lanes_gap_and_a_started_one_finishes(tmp_path: Path) -> None:
    log: list[str] = []

    async def build_returns_to_decoding() -> None:
        write_phase(asker.stories, "decoding")  # the build starts decoding again while the turn runs
        await asyncio.sleep(0.05)

    asker, st = asker_for(tmp_path, log, build_returns_to_decoding)
    write_phase(asker.stories, "decoding")  # the build lane is decoding
    turn = asyncio.create_task(asker.ask(st, "refine", "product", "brief"))
    await asyncio.sleep(0.15)
    assert log == [] and not turn.done()  # the long turn waits
    write_phase(asker.stories, "gap")  # the build reaches its gate
    assert await asyncio.wait_for(turn, 5) == "answer"
    assert log == ["start", "end"]  # started in the gap, and ran to its end though the build decoded again
    [wait] = [f for kind, f in st.events if kind == "lane_wait"]
    assert wait["stage"] == "refine" and wait["seconds"] >= 0.1
    line = [
        json.loads(x)
        for x in (asker.cfg.observability.state_dir / "line" / "events.jsonl").read_text().splitlines()
    ]
    assert [e["kind"] for e in line] == ["lane_yield", "lane_wait", "lane_resume"] and line[0][
        "key"
    ] == "AIR-1"


async def test_a_short_ask_and_the_build_lanes_own_asks_never_wait(tmp_path: Path) -> None:
    log: list[str] = []
    asker, st = asker_for(tmp_path, log)
    write_phase(asker.stories, "decoding")
    assert await asyncio.wait_for(asker.ask(st, "consult:ux", "ux", "b", long=False), 5) == "answer"  # short
    asker.lane = None  # the build lane's package and critic, or a standalone run
    assert await asyncio.wait_for(asker.ask(st, "package", "architect", "b"), 5) == "answer"
    assert log == ["start", "end", "start", "end"] and not [k for k, _ in st.events if k == "lane_wait"]


def write_steps(run_dir: Path, steps: list[tuple[int, float]]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "events.jsonl").write_text(
        "".join(
            json.dumps({"kind": "step", "completion_tokens": tokens, "gen_ms": ms}) + "\n"
            for tokens, ms in steps
        )
    )


def test_short_asks_run_four_wide_and_one_wide_under_the_decode_floor(tmp_path: Path) -> None:
    stories = tmp_path / "stories"
    run = tmp_path / "run"
    assert fanout(stories, width=4, floor_tps=12.0) == 4  # no build: idle
    write_steps(run, [(100, 5000.0)] * 5)  # 20 tokens a second
    write_phase(stories, "decoding", run_dir=run)
    assert fanout(stories, width=4, floor_tps=12.0) == 4
    write_steps(run, [(100, 5000.0)] * 5 + [(10, 5000.0)] * 5)  # the last five: 2 tokens a second
    assert fanout(stories, width=4, floor_tps=12.0) == 1
    write_phase(stories, "gap", run_dir=run)  # in a gap nothing decodes: the riders go wide again
    assert fanout(stories, width=4, floor_tps=12.0) == 4
    write_phase(stories, "decoding", run_dir=tmp_path / "nowhere")  # a run with no steps to read
    assert fanout(stories, width=4, floor_tps=12.0) == 4


async def test_the_askers_gather_reads_the_width_each_round(tmp_path: Path) -> None:
    asker, _ = asker_for(tmp_path, [])
    running = 0
    peak = 0

    async def one() -> int:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return 1

    assert sum(await asker.gather(*(one() for _ in range(8)))) == 8 and peak == 4  # line.fanout
    run = tmp_path / "run"
    write_steps(run, [(1, 1000.0)] * 5)
    write_phase(asker.stories, "decoding", run_dir=run)
    peak = 0
    await asker.gather(*(one() for _ in range(8)))
    assert peak == 1  # the build crawls: one at a time


def test_a_stale_phase_file_reads_as_idle(tmp_path: Path) -> None:
    stories = tmp_path / "stories"
    assert read_phase(stories) == ("idle", 0.0)  # missing
    write_phase(stories, "decoding", now=time.time() - 30)
    phase_, age = read_phase(stories)
    assert phase_ == "decoding" and 29 < age < 40
    write_phase(stories, "decoding", now=time.time() - 3600)  # a build that died mid-decode
    assert read_phase(stories)[0] == "idle" and read_phase(stories)[1] > 3000
    (stories / "build.phase").write_text("{ not json")
    assert read_phase(stories) == ("idle", 0.0)


async def test_wait_for_gap_returns_at_once_in_a_gap_and_says_nothing(tmp_path: Path) -> None:
    said: list[str] = []
    write_phase(tmp_path, "gap")
    assert await wait_for_gap(tmp_path, poll_s=0.01, emit=lambda kind, **f: said.append(kind)) < 0.05
    assert said == []
