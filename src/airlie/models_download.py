"""Download pinned Hugging Face snapshots with progress, a disk check and one shared download per revision."""

from __future__ import annotations

import asyncio
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import huggingface_hub
from huggingface_hub import constants as hf_constants
from tqdm.auto import tqdm

DISK_MARGIN = 1.05  # free space needed, as a multiple of the bytes still to download
GB = 1e9
MIN_INTERVAL_S = 1.0  # progress goes to the caller at most this often


@dataclass(frozen=True)
class DownloadProgress:
    done_bytes: int
    total_bytes: int
    rate_bps: float
    eta_s: float | None


class DownloadError(Exception):
    pass


ProgressFn = Callable[[DownloadProgress], None]


def _nothing(p: DownloadProgress) -> None:
    pass


def _free(path: Path) -> int:
    return shutil.disk_usage(path).free


class _Run:
    """One real download: counts bytes from the snapshot's aggregate bar and forwards them, throttled."""

    def __init__(self, total: int, clock: Callable[[], float], emit: ProgressFn) -> None:
        self.total = total
        self.clock = clock
        self.emit = emit  # called from worker threads
        self.start = clock()
        self.done = 0
        self.last: float | None = None
        self.lock = threading.Lock()

    def progress(self, done: int, now: float) -> DownloadProgress:
        elapsed = now - self.start
        rate = done / elapsed if elapsed > 0 else 0.0
        eta = max(self.total - done, 0) / rate if rate > 0 else None
        return DownloadProgress(done, self.total, rate, eta)

    def add(self, n: int) -> None:
        with self.lock:
            self.done += n
            now = self.clock()
            if self.last is not None and now - self.last < MIN_INTERVAL_S:
                return
            self.last = now
            p = self.progress(self.done, now)
        self.emit(p)

    def final(self) -> DownloadProgress:
        now = self.clock()
        p = self.progress(self.total, now)
        return DownloadProgress(p.done_bytes, p.total_bytes, p.rate_bps, 0.0)

    def tqdm_class(self) -> type[tqdm]:
        run = self

        class ByteBar(tqdm):  # type: ignore[misc]
            """Drawn nowhere; the snapshot's reconstruct bar (desc 'Reconstructing…') feeds `run`."""

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs.pop("disable", None)
                self.counts = "Reconstructing" in str(kwargs.get("desc"))
                super().__init__(*args, disable=True, **kwargs)

            def update(self, n: float | None = 1) -> bool | None:
                if self.counts and n:
                    run.add(int(n))
                return None

        return ByteBar


class Downloader:
    def __init__(
        self,
        *,
        fetch: Callable[..., Any] = huggingface_hub.snapshot_download,
        disk_free: Callable[[Path], int] = _free,
        cache_dir: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._disk_free = disk_free
        self._cache_dir = cache_dir
        self._clock = clock
        self._inflight: dict[str, tuple[asyncio.Task[Path], list[ProgressFn]]] = {}

    async def ensure(self, repo: str, revision: str, on_progress: ProgressFn = _nothing) -> Path:
        """The snapshot dir of `repo` at `revision`, downloading what is missing. Callers asking for the same
        `repo@revision` while it downloads share it (and each hears the progress)."""
        key = f"{repo}@{revision}"
        running = self._inflight.get(key)
        if running is None:
            listeners = [on_progress]
            task = asyncio.create_task(self._download(repo, revision, key, listeners))
            self._inflight[key] = (task, listeners)
        else:
            task, listeners = running
            listeners.append(on_progress)
        return await asyncio.shield(task)

    async def _download(self, repo: str, revision: str, key: str, listeners: list[ProgressFn]) -> Path:
        try:
            return await self._run(repo, revision, listeners)
        except DownloadError:
            raise
        except Exception as e:
            raise DownloadError(f"download of {key} failed: {e}") from e
        finally:
            del self._inflight[key]

    def _kwargs(self) -> dict[str, Any]:
        return {} if self._cache_dir is None else {"cache_dir": self._cache_dir}

    async def _run(self, repo: str, revision: str, listeners: list[ProgressFn]) -> Path:
        loop = asyncio.get_running_loop()
        kw = self._kwargs()
        plan = await asyncio.to_thread(lambda: self._fetch(repo, revision=revision, dry_run=True, **kw))
        needed = sum(f.file_size or 0 for f in plan if f.will_download)
        cache = self._cache_dir or Path(hf_constants.HF_HUB_CACHE)
        probe = next(p for p in (cache, *cache.parents) if p.exists())  # the nearest directory that exists
        free = self._disk_free(probe)
        if free < DISK_MARGIN * needed:
            raise DownloadError(f"needs {needed / GB:.1f} GB, {free / GB:.1f} GB free")

        def forward(p: DownloadProgress) -> None:
            for fn in list(listeners):
                fn(p)

        def from_thread(p: DownloadProgress) -> None:
            loop.call_soon_threadsafe(forward, p)

        run = _Run(needed, self._clock, from_thread)
        path = await asyncio.to_thread(
            lambda: self._fetch(repo, revision=revision, tqdm_class=run.tqdm_class(), **kw)
        )
        forward(run.final())
        return Path(path)
