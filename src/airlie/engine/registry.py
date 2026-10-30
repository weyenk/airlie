"""Generate vllm-mlx's `--models-config` registry and the `vllm-mlx serve` argv from airlie.yaml.

Flag names follow vllm-mlx 0.5.0 (`vllm-mlx serve --help`); values follow docs/wiki/build-spec/02 §1.
The server starts through `SERVE_ENTRY`: vllm-mlx's CLI with airlie's parsers registered first."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

from airlie.config import AirlieConfig, ModelSpec

SnapshotResolver = Callable[[ModelSpec], Path]
SERVE_ENTRY = ["python", "-m", "airlie.engine.vllm_mlx_serve"]  # run by the runner inside `uv run`


def build_registry(
    cfg: AirlieConfig,
    engine_name: str,
    snapshot_dir: SnapshotResolver,
    paths: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    """The registry. `paths` are model directories built for the engine, by name."""
    e = cfg.engines[engine_name]
    built = paths or {}
    models: list[dict[str, Any]] = []
    for name in e.models:
        m = cfg.models[name]
        path = built.get(name) or (m.path if m.path is not None else snapshot_dir(m))
        entry: dict[str, Any] = {
            "name": name,
            "path": str(path),
            "preload": m.preload,
            "continuous_batching": e.continuous_batching
            if m.continuous_batching is None
            else m.continuous_batching,
            "enable_mtp": uses_mtp(m),
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


def uses_mtp(m: ModelSpec) -> bool:
    """Whether `m` decodes speculatively with an MTP head: one it carries, or one served beside it."""
    return m.enable_mtp or m.mtp is not None


def gguf_path(m: ModelSpec, snapshot_dir: SnapshotResolver) -> Path:
    if not m.file:
        raise ValueError(f"model {m.repo} has no `file`; llama_server engines serve one .gguf")
    return (m.path if m.path is not None else snapshot_dir(m)) / m.file


def build_llama_command(cfg: AirlieConfig, engine_name: str, gguf: Path) -> list[str]:
    e = cfg.engines[engine_name]
    name = e.models[0]
    argv = [
        e.binary, "-m", str(gguf),
        "--host", e.host, "--port", str(e.port),
        "-c", str(e.ctx_size),
        "--alias", e.served_name(name),
        "--jinja",
    ]  # fmt: skip
    if e.api_key:
        argv += ["--api-key", e.api_key]
    argv += list(e.extra_args)
    return argv


def write_registry(registry: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(registry, sort_keys=False))
    return path


def build_serve_command(cfg: AirlieConfig, engine_name: str, registry_path: Path) -> list[str]:
    e = cfg.engines[engine_name]
    team_model = cfg.models[cfg.roles[cfg.teams.role].model]
    argv = [
        *SERVE_ENTRY, "serve",
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
    if any(uses_mtp(cfg.models[m]) for m in e.models):
        # the batched engine reads MTP from the global flag only, not the registry entry; a model without a
        # head is served without it (vllm-mlx logs a warning and skips the install)
        argv.append("--enable-mtp")
    if e.use_paged_cache:
        argv.append("--use-paged-cache")
    if e.kv_cache_quantization:
        argv.append("--kv-cache-quantization")
    if e.api_key:
        argv += ["--api-key", e.api_key]
    if e.server_parsers and team_model.tool_call_parser:
        argv += ["--enable-auto-tool-choice", "--tool-call-parser", team_model.tool_call_parser]
    if e.server_parsers and team_model.reasoning_parser:
        argv += ["--reasoning-parser", team_model.reasoning_parser]
    team_role = cfg.roles[cfg.teams.role]
    argv += ["--default-chat-template-kwargs", json.dumps({"enable_thinking": bool(team_role.thinking)})]
    argv += ["--default-top-k", str(team_role.sampling.top_k)]
    if team_role.thinking and team_role.thinking_budget:
        argv += ["--default-thinking-token-budget", str(team_role.thinking_budget)]
    argv.append("--offline")
    argv += list(e.extra_args)
    return argv
