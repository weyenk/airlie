"""The `python -m mlx_vlm.server` command line and environment from airlie.yaml (mlx-vlm 0.7.12).

The server knows its model only by the exact `--model` string, so `served_model` is that string and
every client sends and matches it. Prefix-cache settings are environment only (`APC_*`)."""

from __future__ import annotations

from airlie.config import AirlieConfig, ModelSpec
from airlie.engine.registry import SnapshotResolver

MLX_VLM_ENTRY = ["python", "-m", "mlx_vlm.server"]


def served_model(cfg: AirlieConfig, engine_name: str) -> str:
    e = cfg.engines[engine_name]
    return e.served_name(e.models[0])


def build_mlx_vlm_command(
    cfg: AirlieConfig, engine_name: str, snapshot_dir: SnapshotResolver
) -> tuple[list[str], dict[str, str]]:
    """The argv and the environment to add for one mlx_vlm engine."""
    e = cfg.engines[engine_name]
    m = cfg.models[e.models[0]]
    argv = [*(e.entry or MLX_VLM_ENTRY)]
    argv += ["--decision-model" if e.decision else "--model", served_model(cfg, engine_name)]
    if m.mtp and not e.decision:
        head = snapshot_dir(ModelSpec(repo=m.mtp.repo, revision=m.mtp.revision))
        argv += ["--draft-model", str(head), "--draft-kind", "mtp"]
    argv += ["--host", e.host, "--port", str(e.port)]
    argv += ["--max-num-seqs", str(e.max_num_seqs), "--max-kv-size", str(e.max_kv_size)]
    if e.kv_bits is not None:
        argv += ["--kv-bits", str(e.kv_bits)]
    argv += ["--prefill-step-size", str(e.prefill_step_size)]
    if e.max_image_px is not None:
        argv += ["--max-pixels", str(e.max_image_px**2)]
    argv += e.extra_args

    env: dict[str, str] = {}
    if e.prefix_cache:
        env |= {
            "APC_ENABLED": "1",
            "APC_DISK_ENABLED": "0",  # upstream's disk tier defaults on
            "APC_MEMORY_MAX_GB": f"{e.cache_memory_gb:g}",
            "APC_CHECKPOINT_ENTRIES": str(e.cache_checkpoints),
            "APC_CHECKPOINT_INTERVAL_TOKENS": str(e.cache_checkpoint_interval),
        }
    if e.api_key:
        env["MLX_VLM_SERVER_API_KEY"] = (
            e.api_key
        )  # the server reads this; --api-key would show the key in `ps`
    return argv, {**env, **e.env}
