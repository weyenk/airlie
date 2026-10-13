"""Topology seam: one object per run that arranges branches/teams and returns a RunReport."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from airlie.config import AirlieConfig
from airlie.engine.protocol import Engine
from airlie.obs import RunDir
from airlie.report import RunReport


class Topology(Protocol):
    run_dir: RunDir

    async def execute_task(
        self, query_text: str, *, repo: Path, test_cmd: str | None = None
    ) -> RunReport: ...


def build_topology(cfg: AirlieConfig, engine: Engine, run_id: str | None = None) -> Topology:
    if cfg.topology.kind == "fork":
        from airlie.topology.fork import ForkOrchestrator

        return ForkOrchestrator(cfg, engine, run_id=run_id)
    if cfg.topology.kind == "single":
        cfg.teams.k = 1
        cfg.lts.enabled = False
    from airlie.orchestrator import ParallelAgentTeamOrchestrator

    return ParallelAgentTeamOrchestrator(cfg, engine, run_id=run_id)
