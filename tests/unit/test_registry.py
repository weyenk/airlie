"""Registry file and serve command generated from airlie.yaml (spec 02 §1 launch parameters)."""

from pathlib import Path

import yaml

from airlie.config import ModelSpec, load_config
from airlie.engine.registry import build_registry, build_serve_command, write_registry


def cfg(tmp_path: Path, **engine_overrides):
    data = {
        "version": 1,
        "engines": {
            "main": {"kind": "vllm_mlx", "port": 8080, "models": ["base", "small"], **engine_overrides}
        },
        "models": {
            "base": {
                "repo": "org/base",
                "revision": "r1",
                "estimated_memory_gb": 16,
                "tool_call_parser": "qwen",
                "reasoning_parser": "qwen3",
            },
            "small": {
                "repo": "org/small",
                "revision": "r2",
                "estimated_memory_gb": 3,
                "preload": False,
                "continuous_batching": False,
                "enable_mtp": True,
            },
        },
        "roles": {
            r: {"prompt": "p.md", "model": "base"} for r in ("coder", "summarizer", "judge", "aggregator")
        },
    }
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


def snap(m: ModelSpec) -> Path:
    return Path(f"/hf/{m.repo}/{m.revision}")


def test_registry_lists_each_model_by_catalog_name_with_snapshot_path(tmp_path: Path) -> None:
    reg = build_registry(cfg(tmp_path), "main", snap)
    assert reg["manager"] == {
        "memory_budget_gb": 90.0,
        "contention_policy": {"strategy": "wait_then_fail", "wait_timeout_s": 60},
    }
    base, small = reg["models"]
    assert base == {
        "name": "base",
        "path": "/hf/org/base/r1",
        "preload": True,
        "continuous_batching": True,
        "enable_mtp": False,
        "prefill_step_size": 2048,
        "estimated_memory_gb": 16.0,
    }
    assert small["preload"] is False
    assert small["continuous_batching"] is False  # per-model override wins over the engine default
    assert small["enable_mtp"] is True


def test_registry_omits_estimate_when_unknown(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    c.models["base"].estimated_memory_gb = None
    assert "estimated_memory_gb" not in build_registry(c, "main", snap)["models"][0]


def test_write_registry_round_trips(tmp_path: Path) -> None:
    out = write_registry(build_registry(cfg(tmp_path), "main", snap), tmp_path / "x" / "models.yaml")
    assert out.is_file()
    assert yaml.safe_load(out.read_text())["models"][0]["name"] == "base"


def test_serve_command_pins_spec_launch_parameters(tmp_path: Path) -> None:
    argv = build_serve_command(cfg(tmp_path), "main", Path("/state/models.yaml"))
    assert argv[:4] == ["vllm-mlx", "serve", "--models-config", "/state/models.yaml"]
    pairs = dict(zip(argv[4::2], argv[5::2], strict=False))
    flags = set(argv)
    assert pairs["--host"] == "127.0.0.1"
    assert pairs["--port"] == "8080"
    assert pairs["--max-num-seqs"] == "16"  # spec 02 §1
    assert pairs["--max-kv-size"] == "65536"
    assert pairs["--prefill-step-size"] == "2048"  # spec 02 §1
    assert pairs["--tool-call-parser"] == "qwen"  # from the team role's model
    assert pairs["--reasoning-parser"] == "qwen3"
    assert "--continuous-batching" in flags
    assert "--enable-auto-tool-choice" in flags
    assert "--offline" in flags
    assert "--kv-cache-quantization" not in flags


def test_serve_command_extra_args_and_kv_quant(tmp_path: Path) -> None:
    argv = build_serve_command(
        cfg(tmp_path, kv_cache_quantization=True, extra_args=["--enable-metrics"]), "main", Path("m.yaml")
    )
    assert "--kv-cache-quantization" in argv
    assert argv[-1] == "--enable-metrics"


def test_serve_command_omits_parsers_when_model_has_none(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    c.models["base"].tool_call_parser = None
    c.models["base"].reasoning_parser = None
    argv = build_serve_command(c, "main", Path("m.yaml"))
    assert "--tool-call-parser" not in argv and "--enable-auto-tool-choice" not in argv
    assert "--reasoning-parser" not in argv
