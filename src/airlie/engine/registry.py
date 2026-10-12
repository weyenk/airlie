"""Generate vllm-mlx's `--models-config` registry and the `vllm-mlx serve` argv from airlie.yaml.

Flag names follow vllm-mlx 0.5.0 (`vllm-mlx serve --help`); values follow docs/wiki/build-spec/02 §1."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from airlie.config import AirlieConfig, ModelSpec

SnapshotResolver = Callable[[ModelSpec], Path]


def build_registry(cfg: AirlieConfig, engine_name: str, snapshot_dir: SnapshotResolver) -> dict[str, Any]:
    e = cfg.engines[engine_name]
    models: list[dict[str, Any]] = []
    for name in e.models:
        m = cfg.models[name]
        entry: dict[str, Any] = {
            "name": name,
            "path": str(m.path if m.path is not None else snapshot_dir(m)),
            "preload": m.preload,
            "continuous_batching": e.continuous_batching
            if m.continuous_batching is None
            else m.continuous_batching,
            "enable_mtp": m.enable_mtp,
            "prefill_step_size": e.prefill_step_size,
        }
        if m.estimated_memory_gb is not None:
            entry["estimated_memory_gb"] = float(m.estimated_memory_gb)
        models.append(entry)
    return {
        "manager": {
            "memory_budget_gb": float(e.memory_budget_gb),
            # never silently preempt another model's in-flight requests (design doc, Review Focus 3)
            "contention_policy": {"strategy": "wait_then_fail", "wait_timeout_s": 60},
        },
        "models": models,
    }


def write_registry(registry: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(registry, sort_keys=False))
    return path


def build_serve_command(cfg: AirlieConfig, engine_name: str, registry_path: Path) -> list[str]:
    e = cfg.engines[engine_name]
    team_model = cfg.models[cfg.roles[cfg.teams.role].model]
    argv = [
        "vllm-mlx", "serve",
        "--models-config", str(registry_path),
        "--host", e.host,
        "--port", str(e.port),
        "--max-num-seqs", str(e.max_num_seqs),
        "--max-kv-size", str(e.max_kv_size),
        "--prefill-step-size", str(e.prefill_step_size),
        "--gpu-memory-utilization", str(e.gpu_memory_utilization),
        "--max-request-tokens", str(e.max_request_tokens),
        "--timeout", str(e.timeout_s),
    ]  # fmt: skip
    if e.continuous_batching:
        argv.append("--continuous-batching")
    if e.use_paged_cache:
        argv.append("--use-paged-cache")
    if e.kv_cache_quantization:
        argv.append("--kv-cache-quantization")
    if e.api_key:
        argv += ["--api-key", e.api_key]
    if team_model.tool_call_parser:
        argv += ["--enable-auto-tool-choice", "--tool-call-parser", team_model.tool_call_parser]
    if team_model.reasoning_parser:
        argv += ["--reasoning-parser", team_model.reasoning_parser]
    argv.append("--offline")
    argv += list(e.extra_args)
    return argv
