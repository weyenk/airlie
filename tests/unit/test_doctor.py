"""task doctor: every check is a pure function over an injected SystemProbe; any fail → exit 1."""

from pathlib import Path

import pytest
import yaml

from airlie.config import AirlieConfig, ModelRef, load_config
from airlie.doctor import CheckResult, SystemProbe, node_check, run_checks, web_node_pin

TOOLS_OK = {
    "task": "3.54.0",
    "uv": "0.6.0",
    "git": "2.45.0",
    "hf": "1.0.0",
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
        mlx_vlm: str | None = "0.7.12",
    ) -> None:
        self._wired = wired_mb
        self._ram = ram_gb
        self._snaps = snapshots if snapshots is not None else set()
        self._tools = tools if tools is not None else dict(TOOLS_OK)
        self._mlx_vlm = mlx_vlm

    def package_version(self, dist: str) -> str | None:
        return self._mlx_vlm if dist == "mlx-vlm" else None

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
        tmp_path, {"main": {"kind": "mlx_vlm", "port": 8080, "models": ["team"]}}, {"team": "org/model"}
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


def test_external_openai_engine_needs_no_binary_or_model_files(tmp_path: Path) -> None:
    cfg = make_cfg(
        tmp_path,
        {"main": {"kind": "openai_http", "base_url": "http://127.0.0.1:9/v1", "models": ["team"]}},
        {"team": "org/model"},
    )
    names = {r.name for r in run_checks(cfg, FakeProbe(snapshots=set(), mlx_vlm=None))}
    assert "engine:main:binary" not in names
    assert "model:team" not in names  # an external server owns its own weights


# ---- M0 Task 3: memory projection (spec 01 §1) ----


def projected_cfg(tmp_path: Path, est_gb: float) -> AirlieConfig:
    data = {
        "version": 1,
        "engines": {"main": {"kind": "mlx_vlm", "port": 8080, "models": ["team"], "max_kv_size": 65536}},
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


# ---- hybrid (linear/SSM) attention ----


class ConfigProbe(FakeProbe):
    def __init__(self, configs: dict[str, dict], **kw) -> None:
        super().__init__(**kw)
        self._configs = configs

    def model_config(self, repo: str, revision: str) -> dict | None:
        return self._configs.get(f"{repo}@{revision}")


def test_standard_attention_model_is_ok(tmp_path: Path) -> None:
    cfg = make_cfg(
        tmp_path, {"main": {"kind": "mlx_vlm", "port": 8080, "models": ["team"]}}, {"team": "org/model"}
    )
    probe = ConfigProbe({"org/model@r1": {"layer_types": ["full_attention"] * 4}}, snapshots={"org/model@r1"})
    assert by_name(run_checks(cfg, probe))["model:team:attention"].status == "ok"


def test_attention_check_skipped_when_config_unreadable(tmp_path: Path) -> None:
    cfg = make_cfg(
        tmp_path, {"main": {"kind": "mlx_vlm", "port": 8080, "models": ["team"]}}, {"team": "org/model"}
    )
    names = {r.name for r in run_checks(cfg, ConfigProbe({}, snapshots={"org/model@r1"}))}
    assert "model:team:attention" not in names


def test_standard_config_without_layer_types_is_classified_standard() -> None:
    from airlie.doctor import is_hybrid_attention

    assert (
        is_hybrid_attention({"num_attention_heads": 32, "num_key_value_heads": 4, "model_type": "qwen3_moe"})
        is False
    )
    assert is_hybrid_attention({"model_type": "mystery"}) is None


# ---- per-profile projection ----


def decision_cfg(tmp_path: Path) -> AirlieConfig:
    data = {
        "version": 1,
        "engines": {
            "main": {"kind": "mlx_vlm", "port": 8080, "models": ["team"]},
            "decision": {"kind": "mlx_vlm", "port": 8081, "models": ["kev"], "decision": True},
        },
        "models": {
            "team": {"repo": "org/t", "revision": "r1", "estimated_memory_gb": 16},
            "kev": {"repo": "org/decider", "revision": "d9", "estimated_memory_gb": 3},
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


def test_memory_projection_is_reported_per_profile(tmp_path: Path) -> None:
    cfg = decision_cfg(tmp_path)
    rows = by_name(run_checks(cfg, FakeProbe(snapshots={"org/t@r1"})))
    assert "engine:decision:memory_projection[single]" in rows
    assert "engine:decision:memory_projection[teams]" in rows
    assert "1 teams" in rows["engine:decision:memory_projection[single]"].detail
    assert "3 teams" in rows["engine:decision:memory_projection[teams]"].detail


def test_memory_projection_counts_every_managed_engines_preloaded_weights(tmp_path: Path) -> None:
    cfg = decision_cfg(tmp_path)  # team 16 GB on main, the decision model 3 GB
    rows = by_name(run_checks(cfg, FakeProbe(snapshots={"org/t@r1"})))
    assert (
        "projected 47 GB" in rows["engine:decision:memory_projection[single]"].detail
    )  # 16 + 16.4 KV + 12 + 3


def test_invalid_profile_is_a_fail_row_not_a_crash(tmp_path: Path) -> None:
    cfg = decision_cfg(tmp_path)
    data = yaml.safe_load((tmp_path / "airlie.yaml").read_text())
    data["topology"]["profiles"]["single"] = {"teams": {"k": 0}}
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    rows = by_name(run_checks(cfg, FakeProbe(snapshots={"org/t@r1"})))
    row = rows["engine:decision:memory_projection[single]"]
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


def test_committed_studio_profiles_project_from_the_coders_corrected_kv_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from airlie.doctor import cache_memory_gb, project_memory_gb

    studio = load_config(ROOT / "airlie.yaml", NO_LOCAL, topology="studio")
    projected, unknown = project_memory_gb(studio, "main")
    # 42.0 GB of weights (small, generalist, reviewer, decider-2b) + the build lane's one sequence at the
    # coder's 65.536k × 64 MB/1k = 4.1 GB + 4 asks at 8.192k = 2.0 GB + 12 GB reserve = 59.6 GB, and the
    # caches the autostart mlx-vlm engines set aside (main 8, reviewer 3.5, small 1)
    assert cache_memory_gb(studio.engines["main"]) == 8.0
    assert (round(projected, 1), unknown) == (72.6, [])
    assert projected < studio.memory.wired_limit_mb / 1024  # 104 GB
    monkeypatch.setattr(
        "airlie.config.load.DEFAULT_LOCAL", NO_LOCAL.name
    )  # the profiles reload from the file
    base = load_config(ROOT / "airlie.yaml", NO_LOCAL)
    qwen = load_config(ROOT / "airlie.yaml", NO_LOCAL, topology="studio-qwen25")
    projected, unknown = project_memory_gb(qwen, "main")
    assert (round(projected, 1), unknown) == (103.9, [])
    assert projected < qwen.memory.wired_limit_mb / 1024
    row = by_name(run_checks(base, FakeProbe()))["engine:main:memory_projection[studio-qwen25]"]
    assert row.status == "ok", row


def test_the_memory_projection_counts_the_two_lanes_sequences() -> None:
    """Planning-line P5b, D10: the studio's live KV is the build lane's one sequence at the coder's context
    and line.fanout asks at line.consult_context, not teams.k sequences at the engine's cap."""
    from airlie.doctor import live_kv_gb, projection_detail

    studio = load_config(ROOT / "airlie.yaml", NO_LOCAL, topology="studio")
    e = studio.engines["main"]
    build = studio.roles["coder"]
    rate = studio.models[build.model].kv_mb_per_1k_tokens
    assert round(live_kv_gb(studio, e), 3) == round(
        build.max_context / 1000 * rate / 1024 + 4 * 8192 / 1000 * rate / 1024, 3
    )
    wider = studio.model_copy(deep=True)
    wider.line.fanout = 8
    assert live_kv_gb(wider, e) > live_kv_gb(studio, e)  # more asks, more KV
    assert "the build lane's 1 sequence at 65536 + 4 asks at 8192 + 12 GB prefix cache" in projection_detail(
        studio, e
    )
    teams = load_config(ROOT / "airlie.yaml", NO_LOCAL, topology="teams")
    assert (
        live_kv_gb(teams, teams.engines["main"]) == 3 * 131.072 * 64 / 1024
    )  # the other topologies are as they were


def profile_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AirlieConfig:
    """An mlx_vlm config with a `wide` profile that widens the KV."""
    monkeypatch.setattr("airlie.config.load.DEFAULT_LOCAL", NO_LOCAL.name)
    data = {
        "version": 1,
        "engines": {"main": {"kind": "mlx_vlm", "port": 8080, "models": ["team"], "max_kv_size": 65536}},
        "models": {"team": {"repo": "org/model", "revision": "r1"}},
        "roles": {
            r: {"prompt": "p.md", "model": "team"} for r in ("coder", "summarizer", "judge", "aggregator")
        },
        "topology": {"profiles": {"wide": {"engines": {"main": {"max_kv_size": 131072}}}}},
    }
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


def test_an_engine_running_other_flags_than_the_story_profile_is_a_mismatch_naming_the_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from airlie.doctor import engine_matches_profile

    cfg = profile_cfg(tmp_path, monkeypatch)
    wide = load_config(tmp_path / "airlie.yaml", topology="wide")
    ok = engine_matches_profile(cfg, MlxVlmProcesses([mlx_vlm_argv(wide, "main")]), "wide")
    assert ok.status == "ok" and "main" in ok.name
    wrong = engine_matches_profile(cfg, MlxVlmProcesses([mlx_vlm_argv(cfg, "main")]), "wide")
    assert wrong.status == "fail"
    assert "--max-kv-size 65536 (config: 131072)" in wrong.detail, wrong.detail
    assert wrong.detail.endswith("restart it: task restart")
    assert engine_matches_profile(cfg, MlxVlmProcesses([]), "wide").status == "warn"
    assert engine_matches_profile(cfg, MlxVlmProcesses([]), "nope").status == "warn"  # a profile not declared


class MlxVlmProcesses(FakeProbe):
    """A probe whose host runs mlx_vlm.server processes with the given argvs."""

    def __init__(self, argvs: list[list[str]]) -> None:
        super().__init__()
        self.argvs = argvs

    def all_process_args(self, marker: str) -> list[list[str]] | None:
        return [a for a in self.argvs if marker in " ".join(a)]


def mlx_vlm_argv(cfg: AirlieConfig, engine: str) -> list[str]:
    from airlie.config.load import hf_snapshot_dir
    from airlie.engine.mlx_vlm import build_mlx_vlm_command

    return ["python", *build_mlx_vlm_command(cfg, engine, hf_snapshot_dir)[0][1:]]


def committed_cfg(monkeypatch: pytest.MonkeyPatch) -> AirlieConfig:
    monkeypatch.setattr("airlie.config.load.DEFAULT_LOCAL", NO_LOCAL.name)
    return load_config(ROOT / "airlie.yaml", NO_LOCAL)


def test_an_mlx_vlm_engine_running_other_flags_than_the_config_is_a_mismatch_naming_task_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from airlie.doctor import engine_matches_config

    cfg = committed_cfg(monkeypatch)
    assert cfg.engines["main"].max_kv_size == 131072
    main = mlx_vlm_argv(cfg, "main")
    stale = list(main)
    stale[stale.index("--max-kv-size") + 1] = "65536"
    other = mlx_vlm_argv(cfg, "reviewer")  # on 8082: ignored by main's check
    ok = engine_matches_config(cfg, MlxVlmProcesses([other, main]), "main")
    assert (ok.name, ok.status) == ("engine:config[main]", "ok")
    bad = engine_matches_config(cfg, MlxVlmProcesses([other, stale]), "main")
    assert bad.status == "fail" and "--max-kv-size 65536 (config: 131072)" in bad.detail, bad.detail
    assert "--max-num-seqs" not in bad.detail and bad.detail.endswith("restart it: task restart")
    # the stale argv is on 8080, not the reviewer's port
    assert engine_matches_config(cfg, MlxVlmProcesses([other, stale]), "reviewer").status == "ok"
    bits = main.index("--kv-bits")
    no_bits = main[:bits] + main[bits + 2 :]
    assert (
        "--kv-bits (unset) (config: 8)"
        in engine_matches_config(cfg, MlxVlmProcesses([no_bits]), "main").detail
    )


def test_no_mlx_vlm_server_on_the_engines_port_is_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    from airlie.doctor import engine_matches_config

    cfg = committed_cfg(monkeypatch)
    only_reviewer = MlxVlmProcesses([mlx_vlm_argv(cfg, "reviewer")])
    found = engine_matches_config(cfg, only_reviewer, "main")
    assert found.status == "warn" and "8080" in found.detail
    assert engine_matches_config(cfg, FakeProbe(), "main").status == "warn"  # ps unreadable: no processes


def test_the_story_line_gates_on_the_build_engines_config_not_a_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from airlie.doctor import engine_matches_profile

    cfg = committed_cfg(monkeypatch)
    main = mlx_vlm_argv(cfg, "main")
    ok = engine_matches_profile(cfg, MlxVlmProcesses([main]), "studio")
    assert (ok.name, ok.status) == ("engine:config[main]", "ok")
    stale = list(main)
    stale[stale.index("--max-num-seqs") + 1] = "2"
    bad = engine_matches_profile(cfg, MlxVlmProcesses([stale]), "studio")
    assert bad.status == "fail" and "--max-num-seqs 2 (config: 16)" in bad.detail
    # the qwen25 experiment builds on its own engine (port 8093), which nothing runs
    assert engine_matches_profile(cfg, MlxVlmProcesses([main]), "studio-qwen25").status == "warn"


def test_real_probe_lists_every_matching_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    from airlie.doctor import RealProbe

    ps = (
        "python -m mlx_vlm.server --model /m/a --port 8080\n"
        "python -m mlx_vlm.server --model /m/b --port 8082\n"
        "ps -axo args= mlx_vlm.server\n"
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=ps, stderr="")
    )
    probe = RealProbe()
    ports = [a[a.index("--port") + 1] for a in probe.all_process_args("mlx_vlm.server") or []]
    assert ports == ["8080", "8082"]


def test_a_bench_engine_mismatch_is_restarted_by_name_not_by_task_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from airlie.doctor import engine_matches_profile, start_command

    cfg = committed_cfg(monkeypatch)
    qwen = mlx_vlm_argv(cfg, "qwen25_32b")
    stale = list(qwen)
    stale[stale.index("--max-kv-size") + 1] = "4096"
    bad = engine_matches_profile(cfg, MlxVlmProcesses([stale]), "studio-qwen25")
    assert bad.name == "engine:config[qwen25_32b]" and bad.status == "fail", bad
    assert bad.detail.endswith("restart it: uv run airlie engine restart qwen25_32b"), bad.detail
    assert "task restart" not in bad.detail
    assert start_command(cfg.engines["qwen25_32b"], "qwen25_32b") == "uv run airlie engine up qwen25_32b"
    assert start_command(cfg.engines["main"], "main") == "task restart"


def test_a_bench_engines_model_missing_from_the_cache_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    cfg = mlx_vlm_cfg(tmp_path)  # `bench` is autostart: false; `rev` is not
    found = by_name(run_checks(cfg, FakeProbe()))
    assert found["model:bench"].status == "warn"
    assert found["model:rev"].status == "fail"


def test_a_missing_mtp_head_is_its_own_finding_and_names_the_pull_task(tmp_path: Path) -> None:
    c = managed(tmp_path)
    c.models["team"].mtp = ModelRef(repo="org/model-MTP", revision="h1")
    missing = by_name(run_checks(c, FakeProbe(snapshots={"org/model@r1"})))
    assert missing["model:team"].status == "ok"
    assert (
        missing["model:team:mtp"].status == "fail"
        and "task models:pull -- team" in missing["model:team:mtp"].detail
    )
    present = by_name(run_checks(c, FakeProbe(snapshots={"org/model@r1", "org/model-MTP@h1"})))
    assert present["model:team:mtp"].status == "ok"


def mlx_vlm_cfg(
    tmp_path: Path, *, bench_autostart: bool = False, entry: list[str] | None = None
) -> AirlieConfig:
    main: dict = {
        "kind": "mlx_vlm",
        "port": 8080,
        "model": "base",
        "prefix_cache": True,
        "cache_memory_gb": 8,
    }
    if entry:
        main["entry"] = entry
    data = {
        "version": 1,
        "engines": {
            "main": main,
            "reviewer": {
                "kind": "mlx_vlm",
                "port": 8082,
                "model": "rev",
                "prefix_cache": True,
                "cache_memory_gb": 4,
            },
            "bench": {
                "kind": "mlx_vlm",
                "port": 8091,
                "model": "bench",
                "autostart": bench_autostart,
                "prefix_cache": True,
                "cache_memory_gb": 1,
            },
        },
        "models": {
            "base": {"repo": "org/base", "revision": "r1", "estimated_memory_gb": 16},
            "rev": {"repo": "org/rev", "revision": "r1", "estimated_memory_gb": 19},
            "bench": {"repo": "org/bench", "revision": "r1", "estimated_memory_gb": 20},
        },
        "roles": {
            r: {"prompt": "p.md", "model": "base"} for r in ("coder", "summarizer", "judge", "aggregator")
        },
    }
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


def test_memory_projection_counts_every_autostart_mlx_vlm_engine_and_its_cache(tmp_path: Path) -> None:
    from airlie.doctor import live_kv_gb, project_memory_gb

    cfg = mlx_vlm_cfg(tmp_path)
    gb, unknown = project_memory_gb(cfg, "main")
    assert gb == pytest.approx(
        16 + 19 + 8 + 4 + live_kv_gb(cfg, cfg.engines["main"]) + cfg.memory.os_reserve_gb
    )
    assert unknown == []


class PackageProbe(FakeProbe):
    def __init__(self, versions: dict[str, str], **kw) -> None:
        super().__init__(**kw)
        self._versions = versions

    def package_version(self, dist: str) -> str | None:
        return self._versions.get(dist)


def test_doctor_reports_the_installed_mlx_vlm_for_mlx_vlm_engines(tmp_path: Path) -> None:
    cfg = mlx_vlm_cfg(tmp_path)
    probe = PackageProbe({"mlx-vlm": "0.7.12"})
    r = by_name(run_checks(cfg, probe))["engine:main:binary"]
    assert r.status == "ok"
    assert r.detail == "mlx-vlm 0.7.12 (python -m mlx_vlm.server)"
    r = by_name(run_checks(cfg, PackageProbe({}, mlx_vlm=None)))["engine:main:binary"]
    assert r.status == "fail"
    assert "uv sync" in r.detail


def test_doctor_names_a_custom_mlx_vlm_entry_command(tmp_path: Path) -> None:
    cfg = mlx_vlm_cfg(tmp_path, entry=["mlx-vlm-server", "--x"])
    r = by_name(run_checks(cfg, PackageProbe({}, tools={**TOOLS_OK, "mlx-vlm-server": "1.2"})))
    assert r["engine:main:binary"].status == "ok"
    assert "mlx-vlm-server" in r["engine:main:binary"].detail


def test_hybrid_attention_model_is_ok_because_the_prefix_cache_stores_checkpoints(tmp_path: Path) -> None:
    cfg = mlx_vlm_cfg(tmp_path)
    hybrid = {"text_config": {"layer_types": ["linear_attention", "full_attention"]}}
    probe = ConfigProbe({"org/base@r1": hybrid}, snapshots={"org/base@r1"})
    r = by_name(run_checks(cfg, probe))["model:base:attention"]
    assert r.status == "ok"
    assert "checkpoints" in r.detail and "patch" not in r.detail
