"""The downloader: pinned snapshots through huggingface_hub with progress, a disk check and shared downloads.

The fake `fetch` mirrors huggingface_hub 2.2: a dry run returns DryRunFileInfo-like entries, a real
call builds the aggregate byte bar from `tqdm_class` (total=0, unit="B") and drives it with update(n)."""

import asyncio
import io
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from airlie.models_download import Downloader, DownloadError, DownloadProgress, _free
from airlie.serve.bridge.clock import FakeClock

GB = 1_000_000_000


@dataclass
class Entry:
    file_size: int | None
    will_download: bool = True
    is_cached: bool = False


class FakeHub:
    """Stands in for snapshot_download."""

    def __init__(self, tmp_path: Path, entries: list[Entry], chunks: list[int | None] | None = None) -> None:
        self.snapshot = tmp_path / "snap"
        self.entries = entries
        self.chunks = chunks or []
        self.real_calls: list[str] = []
        self.fail: Exception | None = None
        self.on_chunk: Callable[[int | None], None] = lambda n: (
            None
        )  # run before each update (advance the clock)
        self.gate = threading.Event()  # a real call waits here until it is set
        self.gate.set()
        self.seen_kwargs: dict[str, object] = {}  # of the real call
        self.dry_kwargs: dict[str, object] = {}
        self.bars: list[Any] = []

    def __call__(self, repo: str, *, revision: str, dry_run: bool = False, **kw) -> object:
        if dry_run:
            self.dry_kwargs = {"repo": repo, "revision": revision, **kw}
            return self.entries
        self.seen_kwargs = {"repo": repo, "revision": revision, **kw}
        self.real_calls.append(repo)
        assert self.gate.wait(5)
        if self.fail is not None:
            raise self.fail
        bar = kw["tqdm_class"](total=0, unit="B", desc="Reconstructing (incomplete total...)")
        transfer = kw["tqdm_class"](total=0, unit="B", desc="Downloading bytes")  # network bytes, not counted
        files = kw["tqdm_class"](range(2), total=len(self.entries), desc="Fetching files", disable=False)
        self.bars = [bar, transfer, files]
        assert list(files) == [0, 1]
        assert files.total == len(self.entries)
        for n in self.chunks:
            self.on_chunk(n)
            transfer.update(1_000_000)
            bar.update() if n is None else bar.update(n)
        return str(self.snapshot)


def downloader(hub: FakeHub, tmp_path: Path, *, free: int = 100 * GB, clock: FakeClock | None = None):
    return Downloader(
        fetch=hub,
        disk_free=lambda p: free,
        cache_dir=tmp_path / "cache",
        clock=(clock or FakeClock()).monotonic,
    )


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


async def test_progress_reports_bytes_rate_and_eta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stderr", _Tty())  # a terminal: tqdm would draw its bars unless told not to
    clock = FakeClock()
    clock.now = 100.0
    hub = FakeHub(tmp_path, [Entry(400)], chunks=[100, 100])
    hub.on_chunk = lambda n: setattr(clock, "now", clock.now + 2.0)
    seen: list[DownloadProgress] = []

    path = await downloader(hub, tmp_path, clock=clock).ensure("org/m", "rev1", seen.append)

    assert path == hub.snapshot
    assert seen[0] == DownloadProgress(done_bytes=100, total_bytes=400, rate_bps=50.0, eta_s=6.0)
    assert seen[1] == DownloadProgress(done_bytes=200, total_bytes=400, rate_bps=50.0, eta_s=4.0)
    assert seen[-1] == DownloadProgress(done_bytes=400, total_bytes=400, rate_bps=100.0, eta_s=0.0)
    assert hub.seen_kwargs["revision"] == "rev1"
    assert hub.seen_kwargs["cache_dir"] == tmp_path / "cache"
    assert hub.dry_kwargs == {"repo": "org/m", "revision": "rev1", "cache_dir": tmp_path / "cache"}
    assert all(bar.disable is True for bar in hub.bars)  # the bars draw nothing


async def test_the_final_call_reports_what_was_downloaded(tmp_path: Path) -> None:
    clock = FakeClock()
    clock.now = 100.0
    hub = FakeHub(tmp_path, [Entry(300)], chunks=[100, 200])
    hub.on_chunk = lambda n: setattr(clock, "now", clock.now + 0.25)
    seen: list[DownloadProgress] = []

    await downloader(hub, tmp_path, clock=clock).ensure("org/m", "rev", seen.append)

    assert seen[-1] == DownloadProgress(done_bytes=300, total_bytes=300, rate_bps=600.0, eta_s=0.0)


async def test_a_slow_start_has_no_eta_yet_and_a_slow_link_still_has_one(tmp_path: Path) -> None:
    clock = FakeClock()
    clock.now = 100.0
    hub = FakeHub(tmp_path, [Entry(11)], chunks=[None, 10])
    ticks = iter([100.0, 102.0])  # the first update lands the instant the download starts
    hub.on_chunk = lambda n: setattr(clock, "now", next(ticks))
    seen: list[DownloadProgress] = []

    await downloader(hub, tmp_path, clock=clock).ensure("org/m", "rev", seen.append)

    assert seen[0] == DownloadProgress(done_bytes=1, total_bytes=11, rate_bps=0.0, eta_s=None)
    assert seen[1] == DownloadProgress(
        done_bytes=11, total_bytes=11, rate_bps=5.5, eta_s=0.0
    )  # done: no time left


async def test_the_eta_of_a_link_slower_than_one_byte_a_second(tmp_path: Path) -> None:
    clock = FakeClock()
    hub = FakeHub(tmp_path, [Entry(5)], chunks=[1])
    hub.on_chunk = lambda n: setattr(clock, "now", 2.0)
    seen: list[DownloadProgress] = []

    await downloader(hub, tmp_path, clock=clock).ensure("org/m", "rev", seen.append)

    assert seen[0] == DownloadProgress(done_bytes=1, total_bytes=5, rate_bps=0.5, eta_s=8.0)


async def test_only_files_that_will_download_count_toward_the_total(tmp_path: Path) -> None:
    hub = FakeHub(
        tmp_path,
        [Entry(300), Entry(900, will_download=False, is_cached=True), Entry(None), Entry(100)],
        chunks=[400],
    )
    seen: list[DownloadProgress] = []

    await downloader(hub, tmp_path).ensure("org/m", "rev", seen.append)

    assert {p.total_bytes for p in seen} == {400}


async def test_two_callers_share_one_download(tmp_path: Path) -> None:
    hub = FakeHub(tmp_path, [Entry(10)], chunks=[10])
    hub.gate.clear()
    d = downloader(hub, tmp_path)
    first: list[DownloadProgress] = []
    second: list[DownloadProgress] = []

    a = asyncio.create_task(d.ensure("org/m", "rev", first.append))
    await asyncio.sleep(0.05)  # the first caller is now downloading
    b = asyncio.create_task(d.ensure("org/m", "rev", second.append))
    other = asyncio.create_task(d.ensure("org/other", "rev"))
    await asyncio.sleep(0.05)
    hub.gate.set()
    pa, pb, _ = await asyncio.gather(a, b, other)

    assert pa == pb == hub.snapshot
    assert sorted(hub.real_calls) == ["org/m", "org/other"]  # org/m once, though two callers asked
    assert first and second  # both callers hear the progress


async def test_not_enough_disk_is_refused_before_downloading(tmp_path: Path) -> None:
    hub = FakeHub(tmp_path, [Entry(10 * GB)])

    with pytest.raises(DownloadError, match=r"^needs 10\.0 GB, 10\.4 GB free$"):
        await downloader(hub, tmp_path, free=10_400_000_000).ensure("org/m", "rev")

    assert hub.real_calls == []


async def test_exactly_one_point_oh_five_times_the_download_is_enough(tmp_path: Path) -> None:
    hub = FakeHub(tmp_path, [Entry(10 * GB)])

    path = await downloader(hub, tmp_path, free=10_500_000_000).ensure("org/m", "rev")

    assert path == hub.snapshot


async def test_progress_is_forwarded_at_most_once_a_second(tmp_path: Path) -> None:
    clock = FakeClock()
    clock.now = 100.0
    hub = FakeHub(tmp_path, [Entry(50)], chunks=[1] * 50)
    seen: list[DownloadProgress] = []

    await downloader(hub, tmp_path, clock=clock).ensure("org/m", "rev", seen.append)

    assert [p.done_bytes for p in seen] == [1, 50]  # the first update, then the final one


async def test_a_second_later_the_next_update_is_forwarded(tmp_path: Path) -> None:
    clock = FakeClock()
    hub = FakeHub(tmp_path, [Entry(30)], chunks=[10, 10, 10])
    ticks = iter(
        [0.0, 0.99, 1.0]
    )  # the clock at each update: the third is exactly one second after the first
    hub.on_chunk = lambda n: setattr(clock, "now", next(ticks))
    seen: list[DownloadProgress] = []

    await downloader(hub, tmp_path, clock=clock).ensure("org/m", "rev", seen.append)

    assert [p.done_bytes for p in seen] == [10, 30, 30]  # the first update, the one at +1.0 s, the final


async def test_a_failed_download_raises_download_error(tmp_path: Path) -> None:
    hub = FakeHub(tmp_path, [Entry(10)])
    hub.fail = OSError("connection reset")

    with pytest.raises(DownloadError, match=r"org/m@rev.*connection reset"):
        await downloader(hub, tmp_path).ensure("org/m", "rev")


async def test_a_failed_dry_run_raises_download_error(tmp_path: Path) -> None:
    def broken(repo: str, **kw) -> object:
        raise OSError("no network")

    d = Downloader(fetch=broken, disk_free=lambda p: GB, cache_dir=tmp_path)

    with pytest.raises(DownloadError, match=r"org/m@rev.*no network"):
        await d.ensure("org/m", "rev")


async def test_a_download_can_be_retried_after_it_failed(tmp_path: Path) -> None:
    hub = FakeHub(tmp_path, [Entry(10)], chunks=[10])
    hub.fail = OSError("boom")
    d = downloader(hub, tmp_path)
    with pytest.raises(DownloadError):
        await d.ensure("org/m", "rev")

    hub.fail = None
    path = await d.ensure("org/m", "rev")

    assert path == hub.snapshot
    assert hub.real_calls == ["org/m", "org/m"]


async def test_a_cached_snapshot_downloads_nothing(tmp_path: Path) -> None:
    hub = FakeHub(tmp_path, [Entry(5 * GB, will_download=False, is_cached=True)])
    seen: list[DownloadProgress] = []

    path = await downloader(hub, tmp_path, free=0).ensure("org/m", "rev", seen.append)

    assert path == hub.snapshot
    assert seen == [DownloadProgress(done_bytes=0, total_bytes=0, rate_bps=0.0, eta_s=0.0)]


async def test_the_disk_probe_is_the_nearest_directory_that_exists(tmp_path: Path) -> None:
    hub = FakeHub(tmp_path, [Entry(1)], chunks=[1])
    probed: list[Path] = []

    def free(p: Path) -> int:
        probed.append(p)
        return GB

    (tmp_path / "there").mkdir()
    for cache in (tmp_path / "there", tmp_path / "there" / "not" / "yet"):
        await Downloader(fetch=hub, disk_free=free, cache_dir=cache).ensure("org/m", "rev")

    assert probed == [tmp_path / "there", tmp_path / "there"]


def test_the_default_disk_probe_reads_the_real_free_space(tmp_path: Path) -> None:
    assert _free(tmp_path) > 0


async def test_the_default_cache_dir_is_left_to_huggingface_hub(tmp_path: Path) -> None:
    hub = FakeHub(tmp_path, [Entry(1)], chunks=[1])
    probed: list[Path] = []

    def free(p: Path) -> int:
        probed.append(p)
        return GB

    await Downloader(fetch=hub, disk_free=free).ensure("org/m", "rev")

    assert "cache_dir" not in hub.seen_kwargs
    assert len(probed) == 1 and probed[0].exists()  # the free-space probe is on a directory that exists
