"""`task doctor`: pure checks over an injected SystemProbe, so they are unit-testable without a Mac."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from airlie.config import AirlieConfig, profile_configs

Status = Literal["ok", "warn", "fail"]
REQUIRED_TOOLS = ("task", "uv", "git", "hf")


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str


class SystemProbe(Protocol):
    def sysctl_int(self, name: str) -> int | None: ...
    def total_ram_bytes(self) -> int: ...
    def model_snapshot_dir(self, repo: str, revision: str) -> Path | None: ...
    def tool_version(self, name: str) -> str | None: ...
    def model_config(self, repo: str, revision: str) -> dict[str, Any] | None: ...
    def http_ok(self, url: str) -> bool: ...
    def resolve_binary(self, name: str) -> Path | None: ...
    def file_contains(self, path: Path, needle: str) -> bool | None: ...
    def model_file(self, repo: str, revision: str, file: str) -> Path | None: ...


def systemone_impl_path(binary: Path) -> Path:
    """llama-server's HTTP routes live in lib/libllama-server-impl.dylib beside the binary's prefix
    (ai-mode's doctor checked the same file); the binary itself is a thin launcher."""
    real = binary.resolve()
    return real.parent.parent / "lib" / "libllama-server-impl.dylib"


def is_hybrid_attention(config: dict[str, Any]) -> bool | None:
    """True when the checkpoint mixes linear/SSM layers with attention (Qwen3.5/3.8, Qwen3-Next, Mamba).

    vllm-mlx 0.5.0's prefix cache cannot rewind SSM state, so such models never get prefix-cache hits
    (measured 2026-10-12). None when the config does not say."""
    text = config.get("text_config") if isinstance(config.get("text_config"), dict) else config
    assert isinstance(text, dict)
    layer_types = text.get("layer_types") or text.get("layers_block_type")
    if isinstance(layer_types, list) and layer_types:
        return any(
            str(t) != "full_attention" and "attention" in str(t) or str(t) in {"mamba", "ssm"}
            for t in layer_types
        )
    if any(k in text for k in ("linear_num_key_heads", "linear_conv_kernel_dim", "ssm_cfg", "mamba_d_state")):
        return True
    if "num_attention_heads" in text or "num_key_value_heads" in text:
        return False  # plain attention stack, nothing hybrid declared
    return None


class RealProbe:
    """Talks to the actual host. Only this class touches the OS."""

    def __init__(self, hf_cache: Path | None = None) -> None:
        self.hf_cache = hf_cache or Path("~/.cache/huggingface/hub").expanduser()

    def sysctl_int(self, name: str) -> int | None:
        try:
            out = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return int(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip().isdigit() else None

    def total_ram_bytes(self) -> int:
        v = self.sysctl_int("hw.memsize")
        if v is not None:
            return v
        try:
            import os

            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError, AttributeError):
            return 0

    def model_snapshot_dir(self, repo: str, revision: str) -> Path | None:
        d = self.hf_cache / f"models--{repo.replace('/', '--')}" / "snapshots" / revision
        if not d.is_dir():
            return None
        # The HF cache stores symlinks into a shared blob store; a metadata-only snapshot, a dangling symlink
        # (blob deleted) or an in-progress download (*.incomplete) must all count as "not on disk".
        entries = list(d.iterdir())
        if any(p.name.endswith(".incomplete") for p in entries):
            return None
        weights = [p for p in entries if p.suffix in {".safetensors", ".gguf"}]
        if not weights:
            return None
        if all(p.resolve().is_file() and p.resolve().stat().st_size > 0 for p in weights):
            return d
        return None

    def model_config(self, repo: str, revision: str) -> dict[str, Any] | None:
        d = self.model_snapshot_dir(repo, revision)
        if d is None:
            return None
        try:
            obj = json.loads((d / "config.json").read_text())
        except (OSError, ValueError):
            return None
        return obj if isinstance(obj, dict) else None

    def http_ok(self, url: str) -> bool:
        try:
            return httpx.get(url, timeout=3.0).status_code == 200
        except httpx.HTTPError:
            return False

    def resolve_binary(self, name: str) -> Path | None:
        exe = shutil.which(name)
        return Path(exe) if exe else None

    def file_contains(self, path: Path, needle: str) -> bool | None:
        try:
            return needle.encode() in path.read_bytes()
        except OSError:
            return None

    def model_file(self, repo: str, revision: str, file: str) -> Path | None:
        d = self.hf_cache / f"models--{repo.replace('/', '--')}" / "snapshots" / revision / file
        if d.is_file() and d.resolve().is_file() and d.resolve().stat().st_size > 0:
            return d
        return None

    def tool_version(self, name: str) -> str | None:
        exe = shutil.which(name)
        if not exe:
            return None
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
            line = (out.stdout or out.stderr).strip().splitlines()
            if not line or line[0].lower().startswith("usage:"):
                return f"present ({exe})"  # tool has no --version (e.g. vllm-mlx)
            return line[0]
        except (OSError, subprocess.TimeoutExpired):
            return "present"


def project_memory_gb(cfg: AirlieConfig, engine_name: str) -> tuple[float, list[str]]:
    """Spec 01 §1 budget: preloaded weights + K × KV at max_kv_size + OS reserve.

    Returns (projected GB, names of preloaded models with no estimated_memory_gb)."""
    e = cfg.engines[engine_name]
    weights = 0.0
    unknown: list[str] = []
    for mname in e.models:
        m = cfg.models[mname]
        if not m.preload:
            continue
        if m.estimated_memory_gb is None:
            unknown.append(mname)
        else:
            weights += m.estimated_memory_gb
    team_model = cfg.models[cfg.roles[cfg.teams.role].model]
    kv = cfg.teams.k * (e.max_kv_size / 1000.0) * team_model.kv_mb_per_1k_tokens / 1024.0
    return weights + kv + cfg.memory.os_reserve_gb, unknown


def run_checks(cfg: AirlieConfig, probe: SystemProbe) -> list[CheckResult]:
    results: list[CheckResult] = []
    for tool in REQUIRED_TOOLS:
        v = probe.tool_version(tool)
        results.append(
            CheckResult(
                f"tool:{tool}", "ok" if v else "fail", v or f"'{tool}' not on PATH (see README install)"
            )
        )

    want = cfg.memory.wired_limit_mb
    have = probe.sysctl_int("iogpu.wired_limit_mb")
    fix = f"run: sudo sysctl iogpu.wired_limit_mb={want}  (docs/wiki/build-spec/01 §1)"
    if have is None:
        results.append(CheckResult("iogpu.wired_limit_mb", "warn", f"not set; {fix}"))
    elif have < want:
        results.append(
            CheckResult("iogpu.wired_limit_mb", "warn", f"{have} MB < configured {want} MB; {fix}")
        )
    else:
        results.append(CheckResult("iogpu.wired_limit_mb", "ok", f"{have} MB"))

    ram_gb = probe.total_ram_bytes() / 2**30
    results.append(CheckResult("ram", "ok" if ram_gb >= 64 else "warn", f"{ram_gb:.0f} GiB"))

    managed_models: set[str] = set()
    for ename, e in cfg.engines.items():
        if e.kind == "llama_server":
            managed_models.update(e.models)
            results.extend(_llama_server_checks(ename, e.binary, e.base_url or "", probe))
            continue
        if e.kind != "vllm_mlx":
            continue
        managed_models.update(e.models)
        v = probe.tool_version("vllm-mlx")
        results.append(
            CheckResult(
                f"engine:{ename}:binary",
                "ok" if v else "fail",
                v or "vllm-mlx not installed; run: task engine:install",
            )
        )
        if e.memory_budget_gb > ram_gb:
            results.append(
                CheckResult(
                    f"engine:{ename}:memory_budget",
                    "fail",
                    f"memory_budget_gb={e.memory_budget_gb} exceeds host RAM {ram_gb:.0f} GiB",
                )
            )
        else:
            results.append(
                CheckResult(
                    f"engine:{ename}:memory_budget", "ok", f"{e.memory_budget_gb} GB of {ram_gb:.0f} GiB"
                )
            )
        health_url = f"{(e.base_url or '').removesuffix('/v1')}/health"
        if probe.http_ok(health_url):
            results.append(CheckResult(f"engine:{ename}:health", "ok", f"{health_url} answers"))
        else:
            results.append(
                CheckResult(f"engine:{ename}:health", "warn", f"not running ({health_url}); run: task up")
            )
        variants = profile_configs(cfg) or {cfg.topology.kind: cfg}
        limit_gb = cfg.memory.wired_limit_mb / 1024.0
        for pname, pcfg in variants.items():
            if ename not in pcfg.engines:
                continue
            pe = pcfg.engines[ename]
            projected, unknown = project_memory_gb(pcfg, ename)
            detail = (
                f"projected {projected:.0f} GB (preloaded weights + {pcfg.teams.k} teams × "
                f"{pe.max_kv_size} KV + {pcfg.memory.os_reserve_gb:.0f} GB reserve) "
                f"vs wired limit {limit_gb:.0f} GB"
            )
            if unknown:
                detail += f"; no estimated_memory_gb for {', '.join(unknown)}"
            status: Status = "fail" if projected > limit_gb else ("warn" if unknown else "ok")
            results.append(CheckResult(f"engine:{ename}:memory_projection[{pname}]", status, detail))

    for mname in sorted(managed_models):
        m = cfg.models[mname]
        if m.file:
            f = probe.model_file(m.repo, m.revision, m.file)
            if f:
                results.append(CheckResult(f"model:{mname}:file", "ok", str(f)))
            else:
                results.append(
                    CheckResult(
                        f"model:{mname}:file",
                        "fail",
                        f"{m.repo}@{m.revision[:8]} {m.file} not in HF cache; "
                        f"run: task models:pull -- {mname}",
                    )
                )
            continue
        if m.path is not None:
            ok = m.path.is_dir()
            results.append(
                CheckResult(
                    f"model:{mname}", "ok" if ok else "fail", str(m.path) if ok else f"{m.path} missing"
                )
            )
            continue
        d = probe.model_snapshot_dir(m.repo, m.revision)
        if d:
            results.append(CheckResult(f"model:{mname}", "ok", f"{m.repo}@{m.revision[:8]}"))
            conf = probe.model_config(m.repo, m.revision)
            hybrid = is_hybrid_attention(conf) if conf is not None else None
            if hybrid is True:
                results.append(
                    CheckResult(
                        f"model:{mname}:attention",
                        "warn",
                        "hybrid (linear/SSM) attention: vllm-mlx 0.5.0 prefix cache cannot store it, every "
                        "turn re-prefills the whole conversation; prefer standard attention for team roles",
                    )
                )
            elif hybrid is False:
                results.append(
                    CheckResult(f"model:{mname}:attention", "ok", "standard attention (prefix cache works)")
                )
        elif m.preload:
            results.append(
                CheckResult(
                    f"model:{mname}",
                    "fail",
                    f"{m.repo}@{m.revision[:8]} not in HF cache; run: task models:pull",
                )
            )
        else:
            results.append(
                CheckResult(
                    f"model:{mname}",
                    "warn",
                    f"{m.repo}@{m.revision[:8]} not in HF cache (lazy model, loads on demand); "
                    f"run: task models:pull -- {mname}",
                )
            )
    return results


def _llama_server_checks(ename: str, binary: str, base_url: str, probe: SystemProbe) -> list[CheckResult]:
    out: list[CheckResult] = []
    exe = probe.resolve_binary(binary)
    out.append(
        CheckResult(
            f"engine:{ename}:binary",
            "ok" if exe else "fail",
            str(exe) if exe else f"'{binary}' not on PATH; run: brew install llama.cpp",
        )
    )
    if exe:
        impl = systemone_impl_path(exe)
        if probe.file_contains(impl, "/v1/systemone"):
            out.append(CheckResult(f"engine:{ename}:systemone", "ok", f"{impl.name} has /v1/systemone"))
        else:
            out.append(
                CheckResult(
                    f"engine:{ename}:systemone",
                    "fail",
                    f"{impl} lacks /v1/systemone (Kev needs it); run: brew install --HEAD llama.cpp",
                )
            )
    health_url = f"{base_url.removesuffix('/v1')}/health"
    if probe.http_ok(health_url):
        out.append(CheckResult(f"engine:{ename}:health", "ok", f"{health_url} answers"))
    else:
        out.append(CheckResult(f"engine:{ename}:health", "warn", f"not running ({health_url}); run: task up"))
    return out


def worst(results: list[CheckResult]) -> Status:
    if any(r.status == "fail" for r in results):
        return "fail"
    if any(r.status == "warn" for r in results):
        return "warn"
    return "ok"
