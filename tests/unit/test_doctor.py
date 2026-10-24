"""task doctor: every check is a pure function over an injected SystemProbe; any fail → exit 1."""

from pathlib import Path

import pytest
import yaml

from airlie.config import AirlieConfig, load_config
from airlie.doctor import CheckResult, SystemProbe, node_check, run_checks, web_node_pin

TOOLS_OK = {
    "task": "3.54.0",
    "uv": "0.6.0",
    "git": "2.45.0",
    "hf": "1.0.0",
    "vllm-mlx": "0.5.0",
    "node": "v26.10.0",
}


class FakeProbe(SystemProbe):
    def __init__(
        self,
        *,
        wired_mb: int | None = 106496,
        ram_gb: float = 128.0,
        snapshots: set[str] | None = None,
        tools: dict[str, str | None] | None = None,
        binaries: dict[str, str] | None = None,
        dylib_has_systemone: bool = True,
        files: set[str] | None = None,
    ) -> None:
        self._wired = wired_mb
        self._ram = ram_gb
        self._snaps = snapshots if snapshots is not None else set()
        self._tools = tools if tools is not None else dict(TOOLS_OK)
        self._bins = binaries or {}
        self._systemone = dylib_has_systemone
        self._files = files or set()

    def resolve_binary(self, name: str) -> Path | None:
        return Path(self._bins[name]) if name in self._bins else None

    def file_contains(self, path: Path, needle: str) -> bool | None:
        return self._systemone if needle == "/v1/systemone" else None

    def model_file(self, repo: str, revision: str, file: str) -> Path | None:
        return Path(f"/fake/{repo}/{revision}/{file}") if f"{repo}@{revision}/{file}" in self._files else None

    def sysctl_int(self, name: str) -> int | None:
        return self._wired if name == "iogpu.wired_limit_mb" else None

    def total_ram_bytes(self) -> int:
        return int(self._ram * 2**30)

    def model_snapshot_dir(self, repo: str, revision: str) -> Path | None:
        return Path(f"/fake/{repo}/{revision}") if f"{repo}@{revision}" in self._snaps else None

    def tool_version(self, name: str) -> str | None:
        return self._tools.get(name)

    def model_config(self, repo: str, revision: str) -> dict | None:
        return None

    def http_ok(self, url: str) -> bool:
        return True


def make_cfg(tmp_path: Path, engines: dict, models: dict) -> AirlieConfig:
    data = {
        "version": 1,
        "engines": engines,
        "models": {name: {"repo": repo, "revision": "r1"} for name, repo in models.items()},
        "roles": {
            r: {"prompt": "p.md", "model": "team"} for r in ("coder", "summarizer", "judge", "aggregator")
        },
    }
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


def managed(tmp_path: Path) -> AirlieConfig:
    return make_cfg(
        tmp_path, {"main": {"kind": "vllm_mlx", "port": 8080, "models": ["team"]}}, {"team": "org/model"}
    )


def by_name(results: list[CheckResult]) -> dict[str, CheckResult]:
    return {r.name: r for r in results}


def test_all_green_when_everything_present(tmp_path: Path) -> None:
    results = run_checks(projected_cfg(tmp_path, 16), FakeProbe(snapshots={"org/model@r1"}))
    assert all(r.status == "ok" for r in results), [r for r in results if r.status != "ok"]


def test_missing_model_is_fail_and_names_pull_task(tmp_path: Path) -> None:
    r = by_name(run_checks(managed(tmp_path), FakeProbe(snapshots=set())))["model:team"]
    assert r.status == "fail"
    assert "task models:pull" in r.detail


def test_wired_limit_unset_is_warn_with_fix_command(tmp_path: Path) -> None:
    r = by_name(run_checks(managed(tmp_path), FakeProbe(wired_mb=None, snapshots={"org/model@r1"})))[
        "iogpu.wired_limit_mb"
    ]
    assert r.status == "warn"
    assert "sysctl iogpu.wired_limit_mb=106496" in r.detail


def test_wired_limit_below_config_is_warn(tmp_path: Path) -> None:
    r = by_name(run_checks(managed(tmp_path), FakeProbe(wired_mb=50000, snapshots={"org/model@r1"})))[
        "iogpu.wired_limit_mb"
    ]
    assert r.status == "warn"


def test_missing_tool_is_fail(tmp_path: Path) -> None:
    probe = FakeProbe(snapshots={"org/model@r1"}, tools={**TOOLS_OK, "uv": None})
    assert by_name(run_checks(managed(tmp_path), probe))["tool:uv"].status == "fail"


def test_vllm_mlx_binary_missing_is_fail_with_install_task(tmp_path: Path) -> None:
    probe = FakeProbe(snapshots={"org/model@r1"}, tools={**TOOLS_OK, "vllm-mlx": None})
    r = by_name(run_checks(managed(tmp_path), probe))["engine:main:binary"]
    assert r.status == "fail"
    assert "task engine:install" in r.detail


def test_external_openai_engine_needs_no_binary_or_model_files(tmp_path: Path) -> None:
    cfg = make_cfg(
        tmp_path,
        {"main": {"kind": "openai_http", "base_url": "http://127.0.0.1:9/v1", "models": ["team"]}},
        {"team": "org/model"},
    )
    names = {
        r.name for r in run_checks(cfg, FakeProbe(snapshots=set(), tools={**TOOLS_OK, "vllm-mlx": None}))
    }
    assert "engine:main:binary" not in names
    assert "model:team" not in names  # an external server owns its own weights


def test_memory_budget_over_ram_is_fail(tmp_path: Path) -> None:
    cfg = make_cfg(
        tmp_path,
        {"main": {"kind": "vllm_mlx", "port": 8080, "models": ["team"], "memory_budget_gb": 200}},
        {"team": "org/model"},
    )
    r = by_name(run_checks(cfg, FakeProbe(snapshots={"org/model@r1"})))["engine:main:memory_budget"]
    assert r.status == "fail"


# ---- M0 Task 3: memory projection (spec 01 §1) ----


def projected_cfg(tmp_path: Path, est_gb: float) -> AirlieConfig:
    data = {
        "version": 1,
        "engines": {"main": {"kind": "vllm_mlx", "port": 8080, "models": ["team"], "max_kv_size": 65536}},
        "models": {
            "team": {
                "repo": "org/model",
                "revision": "r1",
                "estimated_memory_gb": est_gb,
                "kv_mb_per_1k_tokens": 256,
            }
        },
        "roles": {
            r: {"prompt": "p.md", "model": "team"} for r in ("coder", "summarizer", "judge", "aggregator")
        },
        "teams": {"k": 3},
    }
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


def test_memory_projection_within_limit_is_ok(tmp_path: Path) -> None:
    # 16 GB weights + 3 teams × 65.536k tokens × 256 MB/1k ≈ 49 GB + 12 GB reserve ≈ 77 GB < 104 GB
    r = by_name(run_checks(projected_cfg(tmp_path, 16), FakeProbe(snapshots={"org/model@r1"})))[
        "engine:main:memory_projection[teams]"
    ]
    assert r.status == "ok"
    assert "77" in r.detail


def test_memory_projection_over_limit_is_fail(tmp_path: Path) -> None:
    r = by_name(run_checks(projected_cfg(tmp_path, 90), FakeProbe(snapshots={"org/model@r1"})))[
        "engine:main:memory_projection[teams]"
    ]
    assert r.status == "fail"
    assert "151" in r.detail


# ---- RealProbe snapshot detection: HF cache uses symlinks into a shared blob store ----


def make_snapshot(hf_cache: Path, repo: str, revision: str, *, weights: str | None) -> Path:
    """weights: 'real' (symlink to an existing blob), 'dangling' (missing blob) or None (metadata only)."""
    snap = hf_cache / f"models--{repo.replace('/', '--')}" / "snapshots" / revision
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}")
    if weights:
        blob = hf_cache / "blobs" / "ab" / "abcdef"
        blob.parent.mkdir(parents=True, exist_ok=True)
        if weights == "real":
            blob.write_bytes(b"\0" * 16)
        (snap / "model.safetensors").symlink_to(blob)
    return snap


def test_real_probe_accepts_snapshot_with_resolvable_weights(tmp_path: Path) -> None:
    from airlie.doctor import RealProbe

    snap = make_snapshot(tmp_path, "org/model", "r1", weights="real")
    assert RealProbe(hf_cache=tmp_path).model_snapshot_dir("org/model", "r1") == snap


def test_real_probe_rejects_dangling_weight_symlinks(tmp_path: Path) -> None:
    from airlie.doctor import RealProbe

    make_snapshot(tmp_path, "org/model", "r1", weights="dangling")
    assert RealProbe(hf_cache=tmp_path).model_snapshot_dir("org/model", "r1") is None


def test_real_probe_rejects_metadata_only_snapshot(tmp_path: Path) -> None:
    from airlie.doctor import RealProbe

    make_snapshot(tmp_path, "org/model", "r1", weights=None)
    assert RealProbe(hf_cache=tmp_path).model_snapshot_dir("org/model", "r1") is None


def test_real_probe_rejects_incomplete_download(tmp_path: Path) -> None:
    from airlie.doctor import RealProbe

    snap = make_snapshot(tmp_path, "org/model", "r1", weights="real")
    (snap / "model-00002.safetensors.incomplete").write_bytes(b"\0")
    assert RealProbe(hf_cache=tmp_path).model_snapshot_dir("org/model", "r1") is None


# ---- hybrid-attention warning (vllm-mlx 0.5.0 prefix cache cannot store linear/SSM attention state) ----


class ConfigProbe(FakeProbe):
    def __init__(self, configs: dict[str, dict], **kw) -> None:
        super().__init__(**kw)
        self._configs = configs

    def model_config(self, repo: str, revision: str) -> dict | None:
        return self._configs.get(f"{repo}@{revision}")


def test_hybrid_attention_model_is_ok_because_airlies_engine_patches_the_prefix_cache(tmp_path: Path) -> None:
    cfg = make_cfg(
        tmp_path, {"main": {"kind": "vllm_mlx", "port": 8080, "models": ["team"]}}, {"team": "org/model"}
    )
    hybrid = {"text_config": {"layer_types": ["linear_attention", "linear_attention", "full_attention"]}}
    probe = ConfigProbe({"org/model@r1": hybrid}, snapshots={"org/model@r1"})
    r = by_name(run_checks(cfg, probe))["model:team:attention"]
    assert r.status == "ok"
    assert "hybrid" in r.detail and "engine patch" in r.detail


def test_standard_attention_model_is_ok(tmp_path: Path) -> None:
    cfg = make_cfg(
        tmp_path, {"main": {"kind": "vllm_mlx", "port": 8080, "models": ["team"]}}, {"team": "org/model"}
    )
    probe = ConfigProbe({"org/model@r1": {"layer_types": ["full_attention"] * 4}}, snapshots={"org/model@r1"})
    assert by_name(run_checks(cfg, probe))["model:team:attention"].status == "ok"


def test_attention_check_skipped_when_config_unreadable(tmp_path: Path) -> None:
    cfg = make_cfg(
        tmp_path, {"main": {"kind": "vllm_mlx", "port": 8080, "models": ["team"]}}, {"team": "org/model"}
    )
    names = {r.name for r in run_checks(cfg, ConfigProbe({}, snapshots={"org/model@r1"}))}
    assert "model:team:attention" not in names


# ---- engine health row (M1 Task 5) ----


class HealthProbe(ConfigProbe):
    def __init__(self, health: dict[str, bool], **kw) -> None:
        super().__init__({}, **kw)
        self._health = health

    def http_ok(self, url: str) -> bool:
        return self._health.get(url, False)


def test_engine_health_ok_when_health_endpoint_answers(tmp_path: Path) -> None:
    cfg = managed(tmp_path)
    probe = HealthProbe({"http://127.0.0.1:8080/health": True}, snapshots={"org/model@r1"})
    r = by_name(run_checks(cfg, probe))["engine:main:health"]
    assert r.status == "ok"


def test_engine_health_warn_when_not_running(tmp_path: Path) -> None:
    cfg = managed(tmp_path)
    r = by_name(run_checks(cfg, HealthProbe({}, snapshots={"org/model@r1"})))["engine:main:health"]
    assert r.status == "warn"
    assert "task up" in r.detail


def test_standard_config_without_layer_types_is_classified_standard() -> None:
    from airlie.doctor import is_hybrid_attention

    assert (
        is_hybrid_attention({"num_attention_heads": 32, "num_key_value_heads": 4, "model_type": "qwen3_moe"})
        is False
    )
    assert is_hybrid_attention({"model_type": "mystery"}) is None


def test_missing_lazy_model_is_warn_not_fail(tmp_path: Path) -> None:
    cfg = make_cfg(
        tmp_path,
        {"main": {"kind": "vllm_mlx", "port": 8080, "models": ["team", "alt"]}},
        {"team": "org/model", "alt": "org/alt"},
    )
    cfg.models["alt"].preload = False
    r = by_name(run_checks(cfg, FakeProbe(snapshots={"org/model@r1"})))["model:alt"]
    assert r.status == "warn"
    assert "lazy" in r.detail and "task models:pull" in r.detail


# ---- Studio foundations Task 3: llama_server engine (Kev) and per-profile projection ----


def decision_cfg(tmp_path: Path) -> AirlieConfig:
    data = {
        "version": 1,
        "engines": {
            "main": {"kind": "vllm_mlx", "port": 8080, "models": ["team"]},
            "decision": {"kind": "llama_server", "port": 8081, "models": ["kev"]},
        },
        "models": {
            "team": {"repo": "org/t", "revision": "r1", "estimated_memory_gb": 16},
            "kev": {
                "repo": "ggml-org/Kev-4B-GGUF",
                "revision": "d9",
                "file": "Kev-4B-Q4_K_M.gguf",
                "preload": True,
                "estimated_memory_gb": 3,
            },
        },
        "roles": {
            r: {"prompt": "p.md", "model": "team"} for r in ("coder", "summarizer", "judge", "aggregator")
        },
        "router": {"kind": "jev", "model": "kev"},
        "topology": {
            "kind": "teams",
            "profiles": {"single": {"teams": {"k": 1}}, "teams": {"teams": {"k": 3}}},
        },
    }
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


def test_llama_server_engine_checks_binary_systemone_and_gguf(tmp_path: Path) -> None:
    cfg = decision_cfg(tmp_path)
    probe = FakeProbe(
        snapshots={"org/t@r1"},
        binaries={"llama-server": "/opt/homebrew/bin/llama-server"},
        files={"ggml-org/Kev-4B-GGUF@d9/Kev-4B-Q4_K_M.gguf"},
    )
    rows = by_name(run_checks(cfg, probe))
    assert rows["engine:decision:binary"].status == "ok"
    assert rows["engine:decision:systemone"].status == "ok"
    assert rows["model:kev:file"].status == "ok"


def test_llama_server_without_systemone_fails(tmp_path: Path) -> None:
    cfg = decision_cfg(tmp_path)
    probe = FakeProbe(
        snapshots={"org/t@r1"},
        binaries={"llama-server": "/opt/homebrew/bin/llama-server"},
        dylib_has_systemone=False,
        files={"ggml-org/Kev-4B-GGUF@d9/Kev-4B-Q4_K_M.gguf"},
    )
    rows = by_name(run_checks(cfg, probe))
    assert rows["engine:decision:systemone"].status == "fail"
    assert "brew" in rows["engine:decision:systemone"].detail


def test_missing_gguf_file_fails_with_pull_hint(tmp_path: Path) -> None:
    cfg = decision_cfg(tmp_path)
    probe = FakeProbe(snapshots={"org/t@r1"}, binaries={"llama-server": "/x/llama-server"})
    rows = by_name(run_checks(cfg, probe))
    assert rows["model:kev:file"].status == "fail"
    assert "task models:pull -- kev" in rows["model:kev:file"].detail


def test_memory_projection_is_reported_per_profile(tmp_path: Path) -> None:
    cfg = decision_cfg(tmp_path)
    rows = by_name(run_checks(cfg, FakeProbe(snapshots={"org/t@r1"})))
    assert "engine:main:memory_projection[single]" in rows
    assert "engine:main:memory_projection[teams]" in rows
    assert "1 teams" in rows["engine:main:memory_projection[single]"].detail
    assert "3 teams" in rows["engine:main:memory_projection[teams]"].detail


def test_memory_projection_counts_every_managed_engines_preloaded_weights(tmp_path: Path) -> None:
    cfg = decision_cfg(tmp_path)  # team 16 GB on main, Kev 3 GB preloaded on the decision engine
    rows = by_name(run_checks(cfg, FakeProbe(snapshots={"org/t@r1"})))
    assert "projected 47 GB" in rows["engine:main:memory_projection[single]"].detail  # 16 + 16.4 KV + 12 + 3


def test_invalid_profile_is_a_fail_row_not_a_crash(tmp_path: Path) -> None:
    cfg = decision_cfg(tmp_path)
    data = yaml.safe_load((tmp_path / "airlie.yaml").read_text())
    data["topology"]["profiles"]["single"] = {"teams": {"k": 0}}
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    rows = by_name(run_checks(cfg, FakeProbe(snapshots={"org/t@r1"})))
    row = rows["engine:main:memory_projection[single]"]
    assert row.status == "fail" and "config invalid" in row.detail


def test_node_matching_the_pin_is_ok() -> None:
    assert node_check("v26.10.0", "26.x") == CheckResult("tool:node", "ok", "v26.10.0")


def test_node_of_another_major_fails_and_names_both() -> None:
    r = node_check("v24.16.0", "26.x")
    assert r.status == "fail" and "v24.16.0" in r.detail and "26" in r.detail


def test_no_node_fails_and_says_to_install_it() -> None:
    r = node_check(None, "26.x")
    assert r.status == "fail" and "install node 26" in r.detail


def test_node_with_a_version_line_that_is_not_a_version_fails() -> None:
    # RealProbe.tool_version answers "present" when `node --version` could not be run to the end
    r = node_check("present", "26.x")
    assert r.status == "fail" and "present" in r.detail and "26" in r.detail


def test_web_node_pin_reads_engines_node_from_web_package_json(tmp_path: Path) -> None:
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text('{"engines": {"node": "30.x"}}')
    assert web_node_pin(tmp_path) == "30.x"


def test_web_node_pin_of_this_repo_is_a_node_26_pin() -> None:
    assert web_node_pin(Path(__file__).resolve().parents[2]).startswith("26")


def test_run_checks_holds_node_to_the_web_pin(tmp_path: Path) -> None:
    ok = by_name(run_checks(managed(tmp_path), FakeProbe(snapshots={"org/model@r1"})))["tool:node"]
    assert ok.status == "ok"
    probe = FakeProbe(snapshots={"org/model@r1"}, tools={**TOOLS_OK, "node": "v24.16.0"})
    stale = by_name(run_checks(managed(tmp_path), probe))["tool:node"]
    assert stale.status == "fail" and "v24.16.0" in stale.detail


def _checks_for_package_json(tmp_path: Path, text: str | None) -> dict[str, CheckResult]:
    """run_checks for a checkout whose web/package.json holds `text` (None: there is no such file)."""
    root = tmp_path / "checkout"
    (root / "web").mkdir(parents=True)
    if text is not None:
        (root / "web" / "package.json").write_text(text)
    return by_name(run_checks(managed(tmp_path), FakeProbe(snapshots={"org/model@r1"}), root=root))


def test_run_checks_fails_the_node_row_when_web_package_json_is_missing_and_still_runs_the_rest(
    tmp_path: Path,
) -> None:
    rows = _checks_for_package_json(tmp_path, None)
    assert rows["tool:node"].status == "fail"
    assert "web/package.json" in rows["tool:node"].detail and "engines.node" in rows["tool:node"].detail
    assert rows["tool:uv"].status == "ok" and rows["model:team"].status == "ok"  # the other checks ran


@pytest.mark.parametrize(
    "text",
    ['{"engines": ', '{"name": "airlie-web"}', '{"engines": {}}', '{"engines": "26.x"}', "[]"],
    ids=["malformed", "no-engines", "no-node", "engines-not-an-object", "not-an-object"],
)
def test_run_checks_fails_the_node_row_when_web_package_json_has_no_readable_pin(
    tmp_path: Path, text: str
) -> None:
    row = _checks_for_package_json(tmp_path, text)["tool:node"]
    assert row.status == "fail" and "web/package.json" in row.detail and "engines.node" in row.detail


def test_a_pin_that_names_no_major_fails_whatever_node_is_found() -> None:
    r = node_check("v26.10.0", "*")
    assert r.status == "fail" and "engines.node" in r.detail


# ---- the committed config's projection (review 2026-10-23, finding 8: a KV constant four times too high) --

ROOT = Path(__file__).resolve().parents[2]
NO_LOCAL = ROOT / "no-local-overrides.yaml"  # never the person's own airlie.local.yaml


def test_committed_studio_profiles_project_from_the_coders_corrected_kv_constant() -> None:
    from airlie.doctor import project_memory_gb

    studio = load_config(ROOT / "airlie.yaml", NO_LOCAL, topology="studio")
    projected, unknown = project_memory_gb(studio, "main")
    # 41.5 GB of weights (small, generalist, reviewer, Kev) + 131.072k × 64 MB/1k = 8.2 GB KV + 12 GB reserve
    assert (round(projected, 1), unknown) == (61.7, [])
    coder8 = load_config(ROOT / "airlie.yaml", NO_LOCAL, topology="studio-coder8")
    projected, unknown = project_memory_gb(coder8, "main")
    # the KV is the coder's model's: + coder8's 32.5 GB of weights, 131.072k × 96 MB/1k = 12.3 GB of KV
    assert coder8.engines["main"].max_kv_size == 131072
    assert (round(projected, 1), unknown) == (98.3, [])
    assert projected < coder8.memory.wired_limit_mb / 1024  # 104 GB: the studio's KV fits this profile
