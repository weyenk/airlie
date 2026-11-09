"""The mlx-vlm server's argv and environment, built from an EngineSpec."""

from pathlib import Path
from typing import Any

import yaml

from airlie.config import AirlieConfig, ModelSpec, load_config
from airlie.engine.mlx_vlm import build_mlx_vlm_command, served_model


def snap(m: ModelSpec) -> Path:
    return Path(f"/hf/{m.repo}/{m.revision}")


def _cfg(tmp_path: Path, *, main: dict[str, Any]) -> AirlieConfig:
    data = {
        "version": 1,
        "engines": {"main": main},
        "models": {
            "base": {
                "repo": "org/base",
                "revision": "r1",
                "path": "/models/base",
                "mtp": {"repo": "org/base-mtp", "revision": "h1"},
                "estimated_memory_gb": 16,
            },
            "small": {"repo": "org/small", "revision": "r2", "path": "/models/small"},
        },
        "roles": {
            r: {"prompt": "p.md", "model": main["model"]}
            for r in ("coder", "summarizer", "judge", "aggregator")
        },
    }
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


def test_main_engine_command_pins_the_spec_flags(tmp_path: Path) -> None:
    cfg = _cfg(
        tmp_path,
        main={
            "kind": "mlx_vlm",
            "port": 8080,
            "model": "base",
            "max_num_seqs": 16,
            "max_kv_size": 131072,
            "kv_bits": 8,
            "prefix_cache": True,
            "cache_memory_gb": 8,
            "cache_checkpoints": 8,
            "max_image_px": 1536,
        },
    )
    argv, env = build_mlx_vlm_command(cfg, "main", snap)
    assert argv == [
        "python", "-m", "mlx_vlm.server",
        "--model", "/models/base",
        "--draft-model", "/hf/org/base-mtp/h1", "--draft-kind", "mtp",
        "--host", "127.0.0.1", "--port", "8080",
        "--max-num-seqs", "16", "--max-kv-size", "131072", "--kv-bits", "8",
        "--prefill-step-size", "2048", "--max-pixels", "2359296",
    ]  # fmt: skip
    assert served_model(cfg, "main") == "/models/base"  # what the client sends and readiness matches
    assert env == {
        "APC_ENABLED": "1",
        "APC_DISK_ENABLED": "0",
        "APC_MEMORY_MAX_GB": "8",
        "APC_CHECKPOINT_ENTRIES": "8",
        "APC_CHECKPOINT_INTERVAL_TOKENS": "2048",
    }  # no MLX_BUFFER_CACHE_LIMIT: mlx-vlm reads none


def test_a_model_without_a_head_gets_no_draft_flags_and_no_cache_env_when_off(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, main={"kind": "mlx_vlm", "port": 8083, "model": "small"})
    argv, env = build_mlx_vlm_command(cfg, "main", snap)
    assert argv[3:5] == ["--model", "/models/small"]
    assert "--draft-model" not in argv and "--kv-bits" not in argv and "--max-pixels" not in argv
    assert env == {}


def test_a_decision_engine_starts_its_model_with_decision_model(tmp_path: Path) -> None:
    cfg = _cfg(
        tmp_path,
        main={"kind": "mlx_vlm", "port": 8084, "model": "small", "decision": True, "max_num_seqs": 4},
    )
    argv, _ = build_mlx_vlm_command(cfg, "main", snap)
    assert argv[3:5] == ["--decision-model", "/models/small"] and "--model" not in argv
    assert "--draft-model" not in argv  # a decision engine never drafts
    assert served_model(cfg, "main") == "/models/small"


def test_a_decision_engine_never_drafts_even_when_its_model_has_a_head(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, main={"kind": "mlx_vlm", "port": 8084, "model": "base", "decision": True})
    argv, _ = build_mlx_vlm_command(cfg, "main", snap)
    assert "--draft-model" not in argv and "--draft-kind" not in argv


def test_api_key_extra_args_env_and_entry_override(tmp_path: Path) -> None:
    cfg = _cfg(
        tmp_path,
        main={
            "kind": "mlx_vlm",
            "port": 8080,
            "model": "base",
            "api_key": "k",
            "extra_args": ["--log-level", "DEBUG"],
            "env": {"FOO": "1"},
            "entry": ["uvx", "--from", "mlx-vlm==0.7.12", "mlx_vlm.server"],
        },
    )
    argv, env = build_mlx_vlm_command(cfg, "main", snap)
    assert argv[:4] == ["uvx", "--from", "mlx-vlm==0.7.12", "mlx_vlm.server"]
    assert argv[-2:] == ["--log-level", "DEBUG"]  # mlx-vlm's log levels are uppercase
    assert "--api-key" not in argv and "k" not in argv  # the key stays out of `ps`
    assert env["MLX_VLM_SERVER_API_KEY"] == "k"
    assert env["FOO"] == "1"
