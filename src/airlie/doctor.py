"""`task doctor`: pure checks over an injected SystemProbe, so they are unit-testable without a Mac."""

from __future__ import annotations

import importlib.metadata
import json
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from airlie.config import AirlieConfig, ConfigError, load_config
from airlie.engine.mlx_vlm import MLX_VLM_ENTRY

Status = Literal["ok", "warn", "fail"]
REQUIRED_TOOLS = ("task", "uv", "git", "hf")
REPO_ROOT = Path(__file__).resolve().parents[2]
"""This checkout: where web/package.json, the node pin, is read from."""


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
    def http_status(self, url: str, headers: dict[str, str] | None = None) -> int | None:
        """The HTTP status `url` answers with (sending `headers`), None when nothing answers."""
        return 200 if self.http_ok(url) else None

    def all_process_args(self, marker: str) -> list[list[str]] | None:
        return None

    def package_version(self, dist: str) -> str | None:
        return None


def is_hybrid_attention(config: dict[str, Any]) -> bool | None:
    """True when the checkpoint mixes linear/SSM layers with attention (Qwen3.5/3.8, Qwen3-Next, Mamba).

    Such a model's prefix cache keeps checkpoints of the SSM state instead of rewinding it. None when the
    config does not say."""
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
        weights = [p for p in entries if p.suffix in {".safetensors"}]
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
        return self.http_status(url) == 200

    def http_status(self, url: str, headers: dict[str, str] | None = None) -> int | None:
        try:
            return httpx.get(url, timeout=3.0, headers=headers).status_code
        except httpx.HTTPError:
            return None

    def all_process_args(self, marker: str) -> list[list[str]] | None:
        """The arguments of every running process whose command line holds `marker` (mlx-vlm runs one
        `python -m mlx_vlm.server` per engine), or None when `ps` cannot be read."""
        try:
            out = subprocess.run(["ps", "-axo", "args="], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        found: list[list[str]] = []
        for line in out.stdout.splitlines():
            if marker in line and "ps -axo" not in line:
                try:
                    found.append(shlex.split(line))
                except ValueError:
                    found.append(line.split())
        return found

    def package_version(self, dist: str) -> str | None:
        try:
            return importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            return None

    def tool_version(self, name: str) -> str | None:
        exe = shutil.which(name)
        if not exe:
            return None
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
            line = (out.stdout or out.stderr).strip().splitlines()
            if not line or line[0].lower().startswith("usage:"):
                return f"present ({exe})"  # tool has no --version
            return line[0]
        except (OSError, subprocess.TimeoutExpired):
            return "present"


def web_node_pin(root: Path) -> str:
    """The node the web app pins (engines.node in web/package.json, the only place node is pinned)."""
    package = json.loads((root / "web" / "package.json").read_text())
    return str(package["engines"]["node"])


def node_check(version: str | None, pin: str) -> CheckResult:
    """The first node on the PATH must be the major `pin` names (npm itself only warns about engines).

    Worded like web/scripts/check-node.mjs, which stops the web tasks for the same reason."""
    pinned = re.search(r"\d+", pin)
    if pinned is None:
        return CheckResult(
            "tool:node", "fail", f"web/package.json's engines.node {pin!r} names no node major"
        )
    wanted = int(pinned.group())
    if version is None:
        return CheckResult(
            "tool:node",
            "fail",
            f"'node' not on PATH; install node {wanted} (see README install) "
            "and put its bin directory on your PATH",
        )
    found = re.match(r"v?(\d+)\.", version)
    if found and int(found.group(1)) == wanted:
        return CheckResult("tool:node", "ok", version)
    return CheckResult(
        "tool:node",
        "fail",
        f"node {version} is the first node on the PATH; airlie's web app needs node {wanted} "
        f"first on the PATH (install node {wanted} and put its bin directory ahead of the other node's "
        "in your PATH)",
    )


def cache_memory_gb(e: Any) -> float:
    """The prefix cache the engine is told to keep, in GB: an mlx_vlm engine's `cache_memory_gb` when its
    prefix cache is on; 0 when it is left to the server's own default."""
    return float(e.cache_memory_gb) if e.kind == "mlx_vlm" and e.prefix_cache else 0.0


def _resident_engines(cfg: AirlieConfig) -> list[Any]:
    """The engines resident beside each other under this (profile-resolved) config: every one that starts with
    `task up`, and every bench engine a role's model sits on (the experiment profiles start it by hand)."""
    used = {cfg.engine_for_model(r.model) for r in cfg.roles.values()}
    return [e for n, e in cfg.engines.items() if e.kind != "openai_http" and (e.autostart or n in used)]


def _weights(cfg: AirlieConfig, e: Any) -> tuple[float, list[str]]:
    """An engine's resident weights (each model and its MTP head) and the names with no estimate."""
    total = 0.0
    unknown: list[str] = []
    for mname in e.models:
        m = cfg.models[mname]
        parts = [(mname, m.estimated_memory_gb)]
        if m.mtp and not e.decision:  # served beside the body (engine/mlx_vlm.py)
            parts.append((f"{mname} MTP head", m.mtp.estimated_memory_gb))
        for label, gb in parts:
            if gb is None:
                unknown.append(label)
            else:
                total += gb
    return total, unknown


def _kv_gb(cfg: AirlieConfig, e: Any, engine_name: str) -> float:
    """The KV one engine holds: the lane KV when it is a lane engine, else its `other_engines_kv` sequence."""
    if cfg.topology.kind == "studio":
        lane = 0.0
        if cfg.engine_for_role(cfg.story.build_role) == engine_name:
            build = cfg.roles[cfg.story.build_role]
            lane += (
                build.max_context
                / 1000.0
                * cfg.models[build.model].kv_mb_per_1k_tokens
                / 1024.0
                * kv_scale(e)
            )
        if cfg.engine_for_role(cfg.story.architect_role) == engine_name:
            ask = cfg.roles[cfg.story.architect_role]
            lane += (
                cfg.line.fanout
                * (cfg.line.consult_context / 1000.0)
                * cfg.models[ask.model].kv_mb_per_1k_tokens
                / 1024.0
                * kv_scale(e)
            )
        if lane:
            return lane
    elif engine_name in _lane_engines(cfg):
        return live_kv_gb(cfg, e)
    seq = _sequence_kv(cfg, engine_name)
    return seq[1] if seq else 0.0


def _footprint(cfg: AirlieConfig, name: str) -> tuple[float, list[str]]:
    e = cfg.engines[name]
    weights, unknown = _weights(cfg, e)
    return weights + _kv_gb(cfg, e, name) + cache_memory_gb(e), unknown


def engine_footprint_gb(cfg: AirlieConfig, name: str) -> float:
    """What one engine holds while it runs: its weights and MTP head, its KV (the lane KV for a lane engine,
    else its `other_engines_kv` sequence) and its prefix cache. A model with no estimate counts 0."""
    return _footprint(cfg, name)[0]


def _sum_footprints(cfg: AirlieConfig, names: list[str]) -> tuple[float, list[str]]:
    total = 0.0
    unknown: list[str] = []
    for name in names:
        gb, miss = _footprint(cfg, name)
        total += gb
        unknown += miss
    return total + cfg.memory.os_reserve_gb, unknown


def _mlx_vlm_names(cfg: AirlieConfig, engines: list[Any]) -> list[str]:
    return [n for n, e in cfg.engines.items() if e.kind == "mlx_vlm" and e in engines]


def project_memory_gb(cfg: AirlieConfig, engine_name: str) -> tuple[float, list[str]]:
    """Spec 01 §1 budget: preloaded weights + the live KV + the prefix cache + OS reserve, over every engine
    that can be resident at once (`_resident_engines`).

    The weights are each resident model's and its MTP head's (the --draft-model loads beside the body). The
    live KV of the single, teams and fork topologies is K × max_kv_size. The studio's is its two lanes'
    (planning-line P5b, D10): the build lane's one sequence at the coder's `max_context`, plus `line.fanout`
    short asks at `line.consult_context`, each at its model's KV rate. Every other resident engine holds one
    sequence at the longest context its roles ask (`other_engines_kv`). KV is the catalog's bf16 rate scaled
    to each engine's `kv_bits` (`kv_scale`). The prefix cache is what the engine's `cache_memory_gb` sets
    aside.

    Returns (projected GB, names of preloaded models or MTP heads with no estimated_memory_gb)."""
    return _sum_footprints(cfg, _mlx_vlm_names(cfg, _resident_engines(cfg)))


def _pinned_engines(cfg: AirlieConfig) -> list[Any]:
    return [e for e in _resident_engines(cfg) if e.lifecycle == "pinned"]


def pinned_floor_gb(cfg: AirlieConfig) -> float:
    """The same sum over the pinned engines only: what stays resident whatever the on-demand ones do."""
    return _sum_footprints(cfg, _mlx_vlm_names(cfg, _pinned_engines(cfg)))[0]


def _lane_engines(cfg: AirlieConfig) -> set[str]:
    """The engines whose KV `live_kv_gb` counts: the studio's lanes', or the team role's."""
    if cfg.topology.kind == "studio":
        return {cfg.engine_for_role(cfg.story.build_role), cfg.engine_for_role(cfg.story.architect_role)}
    return {cfg.engine_for_role(cfg.teams.role)}


def other_engines_kv(cfg: AirlieConfig) -> dict[str, tuple[int, float]]:
    """The KV of every resident engine `live_kv_gb` leaves out, as {engine: (tokens, GB)}: one sequence at
    the longest `max_context` a role on it asks, up to its `max_kv_size`, at that role's model's KV rate and
    the engine's `kv_bits`. An
    engine no role sits on (the decision engine, a bench engine) is left out: its prompts are short."""
    lanes = _lane_engines(cfg)
    resident = _resident_engines(cfg)
    out: dict[str, tuple[int, float]] = {}
    for name, e in cfg.engines.items():
        if e.kind != "mlx_vlm" or name in lanes or e not in resident:
            continue
        if seq := _sequence_kv(cfg, name):
            out[name] = seq
    return out


def _sequence_kv(cfg: AirlieConfig, name: str) -> tuple[int, float] | None:
    """One sequence at the longest context a role on the engine asks, as (tokens, GB), else None."""
    e = cfg.engines[name]
    seqs = [
        (tokens, tokens / 1000.0 * cfg.models[r.model].kv_mb_per_1k_tokens / 1024.0 * kv_scale(e))
        for r in cfg.roles.values()
        if r.model in e.models
        for tokens in [min(r.max_context, e.max_kv_size)]
    ]
    return max(seqs, key=lambda s: s[1]) if seqs else None


_KV_GROUP = 64  # mlx-vlm's KV quantization group (mlx_vlm/generate/common.py DEFAULT_KV_GROUP_SIZE)


def kv_scale(e: Any) -> float:
    """What an engine's KV costs against the catalog's bf16 `kv_mb_per_1k_tokens`: `kv_bits` a value plus a
    16-bit scale and bias for each group of 64 when the engine quantizes its KV, else 1."""
    if e.kind != "mlx_vlm" or e.kv_bits is None:
        return 1.0
    return float((e.kv_bits + 2 * 16 / _KV_GROUP) / 16)


def live_kv_gb(cfg: AirlieConfig, e: Any) -> float:
    """The KV the running sequences hold (see `project_memory_gb`), at each engine's `kv_bits`."""
    if cfg.topology.kind == "studio":
        build = cfg.roles[cfg.story.build_role]
        ask = cfg.roles[cfg.story.architect_role]
        lane = (
            (build.max_context / 1000.0)
            * cfg.models[build.model].kv_mb_per_1k_tokens
            / 1024.0
            * kv_scale(cfg.engines[cfg.engine_for_role(cfg.story.build_role)])
        )
        asks = (
            cfg.line.fanout
            * (cfg.line.consult_context / 1000.0)
            * cfg.models[ask.model].kv_mb_per_1k_tokens
            / 1024.0
            * kv_scale(cfg.engines[cfg.engine_for_role(cfg.story.architect_role)])
        )
        return lane + asks
    team_model = cfg.models[cfg.roles[cfg.teams.role].model]
    return float(
        cfg.teams.k * (e.max_kv_size / 1000.0) * team_model.kv_mb_per_1k_tokens / 1024.0 * kv_scale(e)
    )


def projection_detail(cfg: AirlieConfig, e: Any) -> str:
    """What `project_memory_gb` counted, in words for the doctor's row."""
    cache = sum(cache_memory_gb(o) for o in _resident_engines(cfg))
    others = other_engines_kv(cfg)
    seqs = ", ".join(f"{n} at {tokens}" for n, (tokens, _) in others.items())
    tail = f" + 1 sequence on {seqs}" if others else ""
    tail += f" + {cache:.0f} GB prefix cache" if cache else ""
    if cfg.topology.kind == "studio":
        build = cfg.roles[cfg.story.build_role]
        return (
            f"preloaded weights + the build lane's 1 sequence at {build.max_context} + "
            f"{cfg.line.fanout} asks at {cfg.line.consult_context}{tail} + "
            f"{cfg.memory.os_reserve_gb:.0f} GB reserve"
        )
    return (
        f"preloaded weights + {cfg.teams.k} teams × {e.max_kv_size} KV{tail} + "
        f"{cfg.memory.os_reserve_gb:.0f} GB reserve"
    )


def restart_command(spec: Any, engine_name: str) -> str:
    """What brings an engine back on the config: `task restart` restarts the autostart engines only, so a
    bench engine (autostart: false) is restarted by name."""
    if spec.autostart:
        return "task restart"
    return f"uv run airlie engine restart {engine_name}"


def start_command(spec: Any, engine_name: str) -> str:
    """What starts a down engine: `task restart` for an autostart one, else the engine by name."""
    return "task restart" if spec.autostart else f"uv run airlie engine up {engine_name}"


# the flags a running mlx-vlm server must carry to be the one the config describes
_MLX_VLM_FLAGS = ("--max-num-seqs", "--max-kv-size", "--kv-bits", "--draft-model")


def _mlx_vlm_flags(argv: list[str]) -> dict[str, str]:
    return {a: argv[i + 1] for i, a in enumerate(argv[:-1]) if a in _MLX_VLM_FLAGS}


def _port_of(argv: list[str]) -> str | None:
    return next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--port"), None)


def engine_matches_config(cfg: AirlieConfig, probe: SystemProbe, engine_name: str) -> CheckResult:
    """The running `mlx_vlm.server` on the engine's port against the command the config builds for it: the
    sequences, the KV cap, the KV bits and the draft model. `fail` names the flags that differ and the command
    that restarts it; a KV smaller than the config's rotates a long prompt and wastes a layer, so the line
    starts no build on a mismatch. `warn` when none runs on that port (the engine gate says so)."""
    name = f"engine:config[{engine_name}]"
    spec = cfg.engines[engine_name]
    running = next(
        (a for a in probe.all_process_args("mlx_vlm.server") or [] if _port_of(a) == str(spec.port)), None
    )
    if running is None:
        return CheckResult(name, "warn", f"no mlx_vlm.server is running on port {spec.port}")
    from airlie.config.load import hf_snapshot_dir
    from airlie.engine.mlx_vlm import build_mlx_vlm_command

    wanted = _mlx_vlm_flags(build_mlx_vlm_command(cfg, engine_name, hf_snapshot_dir)[0])
    have = _mlx_vlm_flags(running)
    differ = [
        f"{flag} {have.get(flag, '(unset)')} (config: {wanted.get(flag, '(unset)')})"
        for flag in _MLX_VLM_FLAGS
        if wanted.get(flag) != have.get(flag)
    ]
    if differ:
        return CheckResult(
            name,
            "fail",
            f"engine {engine_name} runs other flags than the config: {'; '.join(differ)}; restart it: "
            + restart_command(spec, engine_name),
        )
    return CheckResult(name, "ok", f"engine {engine_name} carries the config's flags")


def engine_matches_profile(cfg: AirlieConfig, probe: SystemProbe, profile: str) -> CheckResult:
    """The running engine's arguments against the profile's build engine (planning-line P5b, D10), by
    `engine_matches_config`: a KV smaller than the profile's rotates a long prompt and wastes a layer, so the
    line starts no build on a mismatch. `ok` when the engine is not one airlie manages."""
    name = f"engine:profile[{profile}]"
    if profile not in cfg.topology.profiles:
        return CheckResult(name, "warn", f"the config declares no profile {profile}: nothing to compare")
    try:
        target = load_config(cfg.source_path, topology=profile) if cfg.source_path is not None else cfg
    except ConfigError as e:
        return CheckResult(name, "fail", f"profile {profile}: {e}")
    build_engine = target.engine_for_role(target.story.build_role)
    if target.engines[build_engine].kind == "mlx_vlm":
        found = engine_matches_config(target, probe, build_engine)
        return CheckResult(f"engine:config[{build_engine}]", found.status, found.detail)
    return CheckResult(name, "ok", "no engine airlie manages for this profile")


def _profile_variants(cfg: AirlieConfig) -> dict[str, AirlieConfig | ConfigError]:
    """Each declared profile from the source file (an invalid one keeps its error), else the config."""
    if cfg.source_path is None or not cfg.topology.profiles:
        return {cfg.topology.kind: cfg}
    out: dict[str, AirlieConfig | ConfigError] = {}
    for name in cfg.topology.profiles:
        try:
            out[name] = load_config(cfg.source_path, topology=name)
        except ConfigError as e:
            out[name] = e
    return out


def _projection_rows(
    cfg: AirlieConfig, variants: dict[str, AirlieConfig | ConfigError], ename: str
) -> list[CheckResult]:
    """The memory projection of every profile against the wired limit, under engine `ename`'s name."""
    rows: list[CheckResult] = []
    limit_gb = cfg.memory.wired_limit_mb / 1024.0
    for pname, pcfg in variants.items():
        name = f"engine:{ename}:memory_projection[{pname}]"
        if isinstance(pcfg, ConfigError):
            rows.append(CheckResult(name, "fail", f"profile: {pcfg}"))
            continue
        if ename not in pcfg.engines:
            continue
        projected, unknown = project_memory_gb(pcfg, ename)
        detail = (
            f"projected {projected:.0f} GB ({projection_detail(pcfg, pcfg.engines[ename])}) "
            f"vs wired limit {limit_gb:.0f} GB"
        )
        if unknown:
            detail += f"; no estimated_memory_gb for {', '.join(unknown)}"
        floor = pinned_floor_gb(pcfg)
        status: Status = "ok"
        if projected > limit_gb:
            # over only with everything resident: the manager queues or stops idle on-demand engines
            status = "fail" if floor > limit_gb else "warn"
            if status == "warn":
                detail += "; the manager will queue or stop idle engines"
        elif unknown:
            status = "warn"
        rows.append(CheckResult(name, status, detail))
        rows.append(
            CheckResult(
                f"engine:memory_floor[{pname}]",
                "fail" if floor > limit_gb else "ok",
                f"pinned engines {floor:.0f} GB vs wired limit {limit_gb:.0f} GB",
            )
        )
    return rows


def run_checks(cfg: AirlieConfig, probe: SystemProbe, *, root: Path = REPO_ROOT) -> list[CheckResult]:
    results: list[CheckResult] = []
    variants = _profile_variants(cfg)
    for tool in REQUIRED_TOOLS:
        v = probe.tool_version(tool)
        results.append(
            CheckResult(
                f"tool:{tool}", "ok" if v else "fail", v or f"'{tool}' not on PATH (see README install)"
            )
        )
    try:
        pin = web_node_pin(root)
    except (OSError, ValueError, KeyError, TypeError) as err:  # no file, not JSON, no engines.node
        results.append(
            CheckResult(
                "tool:node",
                "fail",
                f"cannot read the node pin: web/package.json is missing, malformed or has no engines.node "
                f"({type(err).__name__}: {err})",
            )
        )
    else:
        results.append(node_check(probe.tool_version("node"), pin))

    if cfg.web.search.kind == "searxng":
        if probe.http_ok(f"{cfg.web.search.url.rstrip('/')}/search?q=doctor&format=json"):
            results.append(CheckResult("web:search", "ok", f"searxng answers at {cfg.web.search.url}"))
        else:
            results.append(
                CheckResult(
                    "web:search",
                    "warn",
                    f"searxng not reachable at {cfg.web.search.url} (web_search will fail; "
                    "run: task searxng:up)",
                )
            )

    want = cfg.memory.wired_limit_mb
    have = probe.sysctl_int("iogpu.wired_limit_mb")
    fix = "run: task gpu:limit (sets it now and at every boot)"
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
    projected_once = False
    for ename, e in cfg.engines.items():
        if e.kind == "mlx_vlm":
            managed_models.update(e.models)
            results.append(_mlx_vlm_binary_check(ename, e.entry, probe))
            health_url = f"{(e.base_url or '').removesuffix('/v1')}/health"
            auth = {"Authorization": f"Bearer {e.api_key}"} if e.api_key else None
            code = probe.http_status(health_url, auth)
            if code == 200:
                results.append(CheckResult(f"engine:{ename}:health", "ok", f"{health_url} answers"))
            elif code in (401, 403) and e.api_key:
                results.append(
                    CheckResult(
                        f"engine:{ename}:health",
                        "warn",
                        f"{health_url} answers {code}: engines.{ename}.api_key is not the key the running "
                        "engine was started with",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        f"engine:{ename}:health",
                        "warn",
                        f"not running ({health_url}); run: {start_command(e, ename)}",
                    )
                )
            if e.autostart and not projected_once:  # one wired limit for every process: one projection
                projected_once = True
                results.extend(_projection_rows(cfg, variants, ename))
            continue
    for mname in sorted(managed_models):
        m = cfg.models[mname]
        # an mlx_vlm engine loads its one model at start, so its model is lazy only when the engine is
        # started by hand (autostart: false)
        serving = [e for e in cfg.engines.values() if mname in e.models]
        needed = not (serving and all(not e.autostart for e in serving))
        if m.path is not None:
            ok = m.path.is_dir()
            results.append(
                CheckResult(
                    f"model:{mname}", "ok" if ok else "fail", str(m.path) if ok else f"{m.path} missing"
                )
            )
            continue
        if m.mtp is not None:
            head = probe.model_snapshot_dir(m.mtp.repo, m.mtp.revision)
            results.append(
                CheckResult(f"model:{mname}:mtp", "ok", f"{m.mtp.repo}@{m.mtp.revision[:8]}")
                if head
                else CheckResult(
                    f"model:{mname}:mtp",
                    "fail" if needed else "warn",
                    f"{m.mtp.repo}@{m.mtp.revision[:8]} not in HF cache; run: task models:pull -- {mname}",
                )
            )
        d = probe.model_snapshot_dir(m.repo, m.revision)
        if d:
            results.append(CheckResult(f"model:{mname}", "ok", f"{m.repo}@{m.revision[:8]}"))
            conf = probe.model_config(m.repo, m.revision)
            hybrid = is_hybrid_attention(conf) if conf is not None else None
            if hybrid is True:
                results.append(
                    CheckResult(
                        f"model:{mname}:attention",
                        "ok",
                        "hybrid (linear/SSM) attention: mlx-vlm's prefix cache stores it as checkpoints "
                        "(`cache_checkpoints`)",
                    )
                )
            elif hybrid is False:
                results.append(
                    CheckResult(f"model:{mname}:attention", "ok", "standard attention (prefix cache works)")
                )
        elif needed:
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


def _mlx_vlm_binary_check(ename: str, entry: list[str] | None, probe: SystemProbe) -> CheckResult:
    name = f"engine:{ename}:binary"
    if entry:
        v = probe.tool_version(entry[0])
        return CheckResult(
            name, "ok" if v else "fail", f"{v} ({' '.join(entry)})" if v else f"{entry[0]} not found"
        )
    v = probe.package_version("mlx-vlm")
    if v:
        return CheckResult(name, "ok", f"mlx-vlm {v} ({' '.join(MLX_VLM_ENTRY)})")
    return CheckResult(name, "fail", "mlx-vlm not installed; run: uv sync")


def worst(results: list[CheckResult]) -> Status:
    if any(r.status == "fail" for r in results):
        return "fail"
    if any(r.status == "warn" for r in results):
        return "warn"
    return "ok"
