"""`task models:pull` / `task models:list`: fetch exactly the pinned revisions from the catalog."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from airlie.config import AirlieConfig
from airlie.models_download import Downloader, DownloadError, DownloadProgress


@dataclass(frozen=True)
class PullPlan:
    name: str
    repo: str
    revision: str


def plan_pulls(cfg: AirlieConfig, only: Sequence[str] = ()) -> list[PullPlan]:
    """One download per catalog model with an HF repo (local-path models are skipped)."""
    plans: list[PullPlan] = []
    for name, m in cfg.models.items():
        if only and name not in only:
            continue
        if m.path is not None:
            continue
        plans.append(PullPlan(name, m.repo, m.revision))
        if m.mtp is not None:  # the head served beside it as the draft model
            r = m.mtp
            plans.append(PullPlan(f"{name}:mtp", r.repo, r.revision))
    return plans


def _forward(
    plan: PullPlan, on_progress: Callable[[PullPlan, DownloadProgress], None] | None
) -> Callable[[DownloadProgress], None]:
    def send(prog: DownloadProgress) -> None:
        if on_progress:
            on_progress(plan, prog)

    return send


def pull(
    cfg: AirlieConfig,
    only: Sequence[str] = (),
    downloader: Downloader | None = None,
    on_progress: Callable[[PullPlan, DownloadProgress], None] | None = None,
    on_error: Callable[[PullPlan, DownloadError], None] | None = None,
) -> list[tuple[PullPlan, int]]:
    """Download each planned model, one after the other: (plan, 0) when it is on disk, (plan, 1) when not."""
    dl = downloader or Downloader()

    async def run() -> list[tuple[PullPlan, int]]:
        out: list[tuple[PullPlan, int]] = []
        for p in plan_pulls(cfg, only):
            try:
                await dl.ensure(p.repo, p.revision, _forward(p, on_progress))
                out.append((p, 0))
            except DownloadError as e:
                if on_error:
                    on_error(p, e)
                out.append((p, 1))
        return out

    return asyncio.run(run())
