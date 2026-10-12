from pathlib import Path

import yaml

from airlie.config import load_config
from airlie.models_cmd import plan_pulls, pull


def cfg(tmp_path: Path):
    data = {
        "version": 1,
        "engines": {"main": {"kind": "vllm_mlx", "port": 8080, "models": ["team", "local"]}},
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
    assert [p.name for p in plans] == ["team"]
    assert plans[0].cmd == ("hf", "download", "org/model", "--revision", "deadbeef")


def test_only_filters_by_name(tmp_path: Path) -> None:
    assert plan_pulls(cfg(tmp_path), only=["nope"]) == []


def test_pull_uses_injected_runner(tmp_path: Path) -> None:
    seen: list[tuple[str, ...]] = []

    def fake(cmd):
        seen.append(tuple(cmd))
        return 0

    results = pull(cfg(tmp_path), runner=fake)
    assert seen == [("hf", "download", "org/model", "--revision", "deadbeef")]
    assert results[0][1] == 0
