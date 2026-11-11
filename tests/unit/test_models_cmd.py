from pathlib import Path

import yaml

from airlie.config import ModelRef, load_config
from airlie.models_cmd import plan_pulls, pull
from airlie.models_download import DownloadError, DownloadProgress


def cfg(tmp_path: Path):
    data = {
        "version": 1,
        "engines": {
            "main": {"kind": "mlx_vlm", "port": 8080, "models": ["team"]},
            "other": {"kind": "mlx_vlm", "port": 8081, "models": ["local"]},
        },
        "models": {
            "team": {"repo": "org/model", "revision": "deadbeef"},
            "local": {"repo": "x/y", "revision": "r", "path": "weights/local"},
        },
        "roles": {
            r: {"prompt": "p.md", "model": "team"} for r in ("coder", "summarizer", "judge", "aggregator")
        },
    }
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


def test_plan_pins_revision_and_skips_local_paths(tmp_path: Path) -> None:
    plans = plan_pulls(cfg(tmp_path))
    assert [(p.name, p.repo, p.revision) for p in plans] == [("team", "org/model", "deadbeef")]


def test_only_filters_by_name(tmp_path: Path) -> None:
    assert plan_pulls(cfg(tmp_path), only=["nope"]) == []


class FakeDownloader:
    def __init__(self, fail: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.fail = fail
        self.asked: list[tuple[str, str]] = []

    async def ensure(self, repo: str, revision: str, on_progress=lambda p: None) -> Path:
        self.asked.append((repo, revision))
        if repo in self.fail:
            raise DownloadError(f"{repo} is unreachable")
        on_progress(DownloadProgress(done_bytes=1, total_bytes=2, rate_bps=1.0, eta_s=1.0))
        return Path("/snap")


def test_pull_downloads_the_body_and_the_mtp_head_of_each_planned_model(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    c.models["team"].mtp = ModelRef(repo="org/model-MTP-bf16", revision="cafe")
    dl = FakeDownloader()
    seen: list[tuple[str, int]] = []

    results = pull(c, downloader=dl, on_progress=lambda plan, prog: seen.append((plan.name, prog.done_bytes)))  # type: ignore[arg-type]

    assert dl.asked == [("org/model", "deadbeef"), ("org/model-MTP-bf16", "cafe")]
    assert [(p.name, rc) for p, rc in results] == [("team", 0), ("team:mtp", 0)]
    assert seen == [("team", 1), ("team:mtp", 1)]


def test_a_download_error_gives_status_1_for_that_plan(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    c.models["team"].mtp = ModelRef(repo="org/model-MTP-bf16", revision="cafe")
    errors: list[tuple[str, str]] = []

    results = pull(
        c,
        downloader=FakeDownloader(fail={"org/model"}),  # type: ignore[arg-type]
        on_error=lambda plan, e: errors.append((plan.name, str(e))),
    )

    assert [(p.name, rc) for p, rc in results] == [("team", 1), ("team:mtp", 0)]  # the next plan still runs
    assert errors == [("team", "org/model is unreachable")]


def test_a_model_with_an_mtp_head_plans_the_head_too(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    c.models["team"].mtp = ModelRef(repo="org/model-MTP-bf16", revision="cafe")
    plans = plan_pulls(c)
    assert [(p.name, p.repo, p.revision) for p in plans] == [
        ("team", "org/model", "deadbeef"),
        ("team:mtp", "org/model-MTP-bf16", "cafe"),
    ]
