"""mlx-vlm engines as manager resources: download what is missing, start, and adopt one already running."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from airlie.config import AirlieConfig
from airlie.doctor import RealProbe, engine_footprint_gb
from airlie.engine.process import EngineProcess
from airlie.engine.types import EngineError
from airlie.manager.core import Report
from airlie.models_download import Downloader, DownloadProgress


class EngineResource:
    kind: Literal["engine", "vm"] = "engine"

    def __init__(
        self,
        cfg: AirlieConfig,
        name: str,
        *,
        process: EngineProcess,
        downloader: Downloader,
        on_disk: Callable[[str, str], bool],
    ) -> None:
        self.cfg = cfg
        self.name = name
        self.process = process
        self.downloader = downloader
        self.on_disk = on_disk
        spec = cfg.engines[name]
        self.lifecycle: Literal["pinned", "on_demand"] = spec.lifecycle
        self.idle_unload_s = spec.idle_unload_s
        self.footprint_gb = engine_footprint_gb(cfg, name)

    def _repos(self) -> list[tuple[str, str]]:
        """The body and MTP head of each model the engine serves, as (repo, revision)."""
        spec = self.cfg.engines[self.name]
        out: list[tuple[str, str]] = []
        for mname in spec.models:
            m = self.cfg.models[mname]
            out.append((m.repo, m.revision))
            if m.mtp and not spec.decision:
                out.append((m.mtp.repo, m.mtp.revision))
        return out

    async def running(self) -> bool:
        """Healthy on its port, whoever started it: an engine started by hand counts."""
        return await self.process.prober.health_ok()

    async def start(self, report: Report) -> None:
        for repo, revision in self._repos():
            if self.on_disk(repo, revision):
                continue

            def progress(p: DownloadProgress, repo: str = repo) -> None:
                report("downloading", repo, p)

            report("downloading", repo, None)
            await self.downloader.ensure(repo, revision, progress)
        report("starting", "", None)
        try:
            await self.process.start(timeout_s=self.cfg.manager.start_timeout_s)
        except EngineError as e:
            tail = self.process.tail_log(20)
            raise EngineError(f"{e}\n{tail}" if tail else str(e)) from e

    async def stop(self) -> None:
        await self.process.stop()


def _on_disk_in_hf_cache() -> Callable[[str, str], bool]:
    probe = RealProbe()
    return lambda repo, revision: probe.model_snapshot_dir(repo, revision) is not None


def engine_resources(
    cfg: AirlieConfig,
    *,
    state_dir: Path,
    downloader: Downloader,
    on_disk: Callable[[str, str], bool] | None = None,
) -> list[EngineResource]:
    """A resource for every `mlx_vlm` engine; `on_disk` defaults to the HF cache check the doctor uses."""
    check = on_disk or _on_disk_in_hf_cache()
    return [
        EngineResource(
            cfg,
            name,
            process=EngineProcess(cfg, name, state_dir=state_dir),
            downloader=downloader,
            on_disk=check,
        )
        for name, e in cfg.engines.items()
        if e.kind == "mlx_vlm"
    ]
