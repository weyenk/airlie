"""`task models:pull` / `task models:list`: fetch exactly the pinned revisions from the catalog."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from airlie.config import AirlieConfig

Runner = Callable[[Sequence[str]], int]


def _run(cmd: Sequence[str]) -> int:
    return subprocess.run(list(cmd), check=False).returncode


@dataclass(frozen=True)
class PullPlan:
    name: str
    repo: str
    revision: str
    cmd: tuple[str, ...]


def plan_pulls(cfg: AirlieConfig, only: Sequence[str] = ()) -> list[PullPlan]:
    """One `hf download` per catalog model with an HF repo (local-path models are skipped)."""
    plans: list[PullPlan] = []
    for name, m in cfg.models.items():
        if only and name not in only:
            continue
        if m.path is not None:
            continue
        file = (m.file,) if m.file else ()
        plans.append(
            PullPlan(name, m.repo, m.revision, ("hf", "download", m.repo, *file, "--revision", m.revision))
        )
    return plans


def pull(cfg: AirlieConfig, only: Sequence[str] = (), runner: Runner = _run) -> list[tuple[PullPlan, int]]:
    return [(p, runner(p.cmd)) for p in plan_pulls(cfg, only)]
