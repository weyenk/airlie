from pathlib import Path

import yaml

from airlie.config import ModelRef, load_config
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


def test_file_models_pull_only_that_file(tmp_path: Path) -> None:
    data = {
        "version": 1,
        "engines": {
            "main": {"kind": "vllm_mlx", "port": 8080, "models": ["team"]},
            "decision": {"kind": "llama_server", "port": 8081, "models": ["kev"]},
        },
        "models": {
            "team": {"repo": "org/model", "revision": "deadbeef"},
            "kev": {"repo": "ggml-org/Kev-4B-GGUF", "revision": "d9", "file": "Kev-4B-Q4_K_M.gguf"},
        },
        "roles": {
            r: {"prompt": "p.md", "model": "team"} for r in ("coder", "summarizer", "judge", "aggregator")
        },
    }
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    plans = {p.name: p for p in plan_pulls(load_config(tmp_path / "airlie.yaml"))}
    assert plans["kev"].cmd == (
        "hf", "download", "ggml-org/Kev-4B-GGUF", "Kev-4B-Q4_K_M.gguf", "--revision", "d9",
    )  # fmt: skip


def test_a_model_with_an_mtp_head_pulls_the_head_too(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    c.models["team"].mtp = ModelRef(repo="org/model-MTP-bf16", revision="cafe")
    plans = plan_pulls(c)
    assert [(p.name, p.cmd) for p in plans] == [
        ("team", ("hf", "download", "org/model", "--revision", "deadbeef")),
        ("team:mtp", ("hf", "download", "org/model-MTP-bf16", "--revision", "cafe")),
    ]
