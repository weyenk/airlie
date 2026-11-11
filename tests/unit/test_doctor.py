"""task doctor: every check is a pure function over an injected SystemProbe; any fail → exit 1."""

from pathlib import Path

import pytest
import yaml

from airlie.config import AirlieConfig, ModelRef, load_config
from airlie.doctor import CheckResult, node_check, run_checks, web_node_pin
from tests.support.doctor import TOOLS_OK, FakeProbe, by_name


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
    assert "task gpu:limit" in r.detail


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


class HealthProbe(FakeProbe):
    def __init__(self, health: dict[str, bool], **kw) -> None:
        super().__init__(**kw)
        self._health = health

    def http_ok(self, url: str) -> bool:
        return self._health.get(url, False)


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
    # 71.7 GB of weights (small, generalist and its MTP head, reviewer, decider-2b, gemma4 and its assistant)
    # + the build lane's one sequence at the coder's 65.536k × 64 MB/1k + 4 asks at 8.192k, at main's 8-bit KV
    # (8.5/16 of bf16) = 3.3 GB + one sequence on each other engine (reviewer 65.536k × 64 at 8-bit = 2.2,
    # small 16.384k × 144 in bf16 = 2.3, gemma 262.144k × 22 at 8-bit = 3.0) + the caches the autostart
    # mlx-vlm engines set aside (main 8, reviewer 3.5, small 1, gemma 4) + 12 GB reserve
    assert cache_memory_gb(studio.engines["main"]) == 8.0
    assert (round(projected, 1), unknown) == (111.0, [])
    assert projected < studio.memory.wired_limit_mb / 1024  # 124 GB
    monkeypatch.setattr(
        "airlie.config.load.DEFAULT_LOCAL", NO_LOCAL.name
    )  # the profiles reload from the file
    base = load_config(ROOT / "airlie.yaml", NO_LOCAL)
    rows = [r for r in run_checks(base, FakeProbe()) if "memory_projection" in r.name]
    assert rows and all(r.status == "ok" for r in rows), rows  # every committed profile fits


def test_the_memory_projection_counts_the_two_lanes_sequences() -> None:
    """Planning-line P5b, D10: the studio's live KV is the build lane's one sequence at the coder's context
    and line.fanout asks at line.consult_context, not teams.k sequences at the engine's cap."""
    from airlie.doctor import live_kv_gb, projection_detail

    studio = load_config(ROOT / "airlie.yaml", NO_LOCAL, topology="studio")
    e = studio.engines["main"]
    build = studio.roles["coder"]
    rate = studio.models[build.model].kv_mb_per_1k_tokens
    assert round(live_kv_gb(studio, e), 3) == round(
        (build.max_context / 1000 * rate / 1024 + 4 * 8192 / 1000 * rate / 1024) * 8.5 / 16, 3
    )  # main keeps its KV at 8 bits
    wider = studio.model_copy(deep=True)
    wider.line.fanout = 8
    assert live_kv_gb(wider, e) > live_kv_gb(studio, e)  # more asks, more KV
    assert (
        "the build lane's 1 sequence at 65536 + 4 asks at 8192 + 1 sequence on reviewer at 65536, "
        "small at 16384, gemma at 262144 + 16 GB prefix cache" in projection_detail(studio, e)
    )
    teams = load_config(ROOT / "airlie.yaml", NO_LOCAL, topology="teams")
    assert (
        live_kv_gb(teams, teams.engines["main"]) == 3 * 131.072 * 64 / 1024 * 8.5 / 16
    )  # the other topologies count K × max_kv_size


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


def test_a_bench_engine_mismatch_is_restarted_by_name_not_by_task_restart(tmp_path: Path) -> None:
    from airlie.doctor import engine_matches_config, start_command

    cfg = mlx_vlm_cfg(tmp_path)  # `bench` is autostart: false
    stale = mlx_vlm_argv(cfg, "bench")
    stale[stale.index("--max-kv-size") + 1] = "4096"
    bad = engine_matches_config(cfg, MlxVlmProcesses([stale]), "bench")
    assert bad.name == "engine:config[bench]" and bad.status == "fail", bad
    assert bad.detail.endswith("restart it: uv run airlie engine restart bench"), bad.detail
    assert "task restart" not in bad.detail
    assert start_command(cfg.engines["bench"], "bench") == "uv run airlie engine up bench"
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


TEAM_ROLES = ("coder", "summarizer", "judge", "aggregator")


def two_engine_cfg(tmp_path: Path, *, head_gb: float | None) -> AirlieConfig:
    """main serves the team (with an MTP head); side serves a chat role; idle serves no role."""
    data = {
        "version": 1,
        "engines": {
            "main": {"kind": "mlx_vlm", "port": 8080, "model": "team", "max_kv_size": 65536},
            "side": {"kind": "mlx_vlm", "port": 8084, "model": "talk", "max_kv_size": 16384},
            "idle": {"kind": "mlx_vlm", "port": 8085, "model": "spare"},
        },
        "models": {
            "team": {
                "repo": "org/t",
                "revision": "r1",
                "estimated_memory_gb": 16,
                "kv_mb_per_1k_tokens": 64,
                "mtp": {"repo": "org/t-mtp", "revision": "m1", "estimated_memory_gb": head_gb},
            },
            "talk": {"repo": "org/c", "revision": "r1", "estimated_memory_gb": 28, "kv_mb_per_1k_tokens": 12},
            "spare": {"repo": "org/s", "revision": "r1", "estimated_memory_gb": 3},
        },
        "roles": {
            **{r: {"prompt": "p.md", "model": "team"} for r in TEAM_ROLES},
            "chat": {"prompt": "p.md", "model": "talk", "max_context": 131072},
        },
        "teams": {"k": 1},
    }
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


def test_kv_is_charged_at_the_engines_kv_bits() -> None:
    """The catalog's kv_mb_per_1k_tokens is bf16; an engine started with --kv-bits keeps that many bits a
    value plus a 16-bit scale and bias per group of 64 (mlx-vlm's default group)."""
    from airlie.config.schema import EngineSpec
    from airlie.doctor import kv_scale

    assert kv_scale(EngineSpec(kind="mlx_vlm", model="m")) == 1.0
    assert kv_scale(EngineSpec(kind="mlx_vlm", model="m", kv_bits=8)) == 8.5 / 16
    assert kv_scale(EngineSpec(kind="mlx_vlm", model="m", kv_bits=4)) == 4.5 / 16


def test_memory_projection_counts_the_mtp_head_beside_its_body(tmp_path: Path) -> None:
    from airlie.doctor import project_memory_gb

    with_head, unknown = project_memory_gb(two_engine_cfg(tmp_path, head_gb=0.85), "main")
    assert unknown == []
    without, unknown = project_memory_gb(two_engine_cfg(tmp_path, head_gb=None), "main")
    assert unknown == ["team MTP head"]  # a head with no estimate is named, as a body is
    assert with_head - without == pytest.approx(0.85)


def test_memory_projection_counts_one_sequence_on_every_other_engine_a_role_uses(tmp_path: Path) -> None:
    from airlie.doctor import live_kv_gb, other_engines_kv, project_memory_gb, projection_detail

    cfg = two_engine_cfg(tmp_path, head_gb=0.85)
    side = 16384 / 1000 * 12 / 1024  # the chat role asks 131072, the engine stops at its max_kv_size
    assert other_engines_kv(cfg) == {"side": (16384, pytest.approx(side))}  # idle serves no role: no KV
    gb, _ = project_memory_gb(cfg, "main")
    assert gb == pytest.approx(16 + 0.85 + 28 + 3 + live_kv_gb(cfg, cfg.engines["main"]) + side + 12)
    assert "1 teams × 65536 KV + 1 sequence on side at 16384 + 12 GB reserve" in projection_detail(
        cfg, cfg.engines["main"]
    )


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


def test_a_managed_engines_health_row_is_ok_when_it_answers_and_names_the_start_command_when_not(
    tmp_path: Path,
) -> None:
    cfg = managed(tmp_path)
    up = HealthProbe({"http://127.0.0.1:8080/health": True}, snapshots={"org/model@r1"})
    ok = by_name(run_checks(cfg, up))["engine:main:health"]
    assert ok.status == "ok" and "8080/health" in ok.detail
    down = by_name(run_checks(cfg, HealthProbe({}, snapshots={"org/model@r1"})))["engine:main:health"]
    assert down.status == "warn" and "run: task restart" in down.detail
    cfg.engines["main"].autostart = False
    by_hand = by_name(run_checks(cfg, HealthProbe({}, snapshots={"org/model@r1"})))["engine:main:health"]
    assert "uv run airlie engine up main" in by_hand.detail


class KeyedProbe(HealthProbe):
    """A host whose engine answers /health only to the right Bearer key."""

    def __init__(self, key: str | None, **kw) -> None:
        super().__init__({}, **kw)
        self._key = key
        self.sent: list[dict[str, str] | None] = []

    def http_status(self, url: str, headers: dict[str, str] | None = None) -> int | None:
        self.sent.append(headers)
        if self._key is None:
            return None
        return 200 if headers == {"Authorization": f"Bearer {self._key}"} else 401


def test_a_keyed_engines_health_row_sends_its_key_and_blames_a_wrong_one(tmp_path: Path) -> None:
    cfg = managed(tmp_path)
    cfg.engines["main"].api_key = "sekret"
    snaps = {"snapshots": {"org/model@r1"}}
    right = KeyedProbe("sekret", **snaps)
    assert by_name(run_checks(cfg, right))["engine:main:health"].status == "ok"
    assert right.sent == [{"Authorization": "Bearer sekret"}]
    wrong = by_name(run_checks(cfg, KeyedProbe("other", **snaps)))["engine:main:health"]
    assert wrong.status == "warn" and "api_key" in wrong.detail and "not running" not in wrong.detail
    down = by_name(run_checks(cfg, KeyedProbe(None, **snaps)))["engine:main:health"]
    assert down.status == "warn" and "not running" in down.detail and "run: task restart" in down.detail
    cfg.engines["main"].api_key = None  # no key set: a 401 is only "not answering well", never a key blame
    keyless = by_name(run_checks(cfg, KeyedProbe("other", **snaps)))["engine:main:health"]
    assert "not running" in keyless.detail


def test_the_pinned_floor_counts_on_demand_engines_separately() -> None:
    """The committed studio keeps reviewer and small on demand: they are excluded from the pinned floor."""
    from airlie.doctor import engine_footprint_gb

    studio = load_config(ROOT / "airlie.yaml", NO_LOCAL, topology="studio")
    assert studio.engines["reviewer"].lifecycle == "on_demand"
    assert studio.engines["small"].lifecycle == "on_demand"
    assert round(engine_footprint_gb(studio, "reviewer"), 1) == 25.1
    assert round(engine_footprint_gb(studio, "small"), 1) == 6.3


def test_the_pinned_floor_is_the_projection_without_the_on_demand_engines() -> None:
    """The floor is the 111.0 GB projection minus reviewer's 25.1 GB (19.4 weights + 2.2 KV + 3.5 cache)
    and small's 6.3 GB (3 + 2.3 + 1), yielding 79.6 GB."""
    from airlie.doctor import engine_footprint_gb, pinned_floor_gb, project_memory_gb

    studio = load_config(ROOT / "airlie.yaml", NO_LOCAL, topology="studio")
    projected, _ = project_memory_gb(studio, "main")
    floor = pinned_floor_gb(studio)
    assert round(floor, 1) == 79.6
    assert floor == pytest.approx(
        projected - engine_footprint_gb(studio, "reviewer") - engine_footprint_gb(studio, "small")
    )


def test_pinned_floor_within_limit_is_ok_row() -> None:
    """The floor row is ok when pinned engines fit within the wired limit."""
    studio = load_config(ROOT / "airlie.yaml", NO_LOCAL, topology="studio")
    rows = by_name(run_checks(studio, FakeProbe()))
    assert rows["engine:memory_floor[studio]"].status == "ok"


def test_pinned_floor_exceeding_limit_is_fail_row(tmp_path: Path) -> None:
    """When pinned engines exceed the wired limit, the floor and projection rows are both fail."""
    data = {
        "version": 1,
        "engines": {
            "main": {"kind": "mlx_vlm", "port": 8080, "model": "team", "max_kv_size": 1024},
            "big": {"kind": "mlx_vlm", "port": 8081, "model": "huge", "lifecycle": "on_demand"},
        },
        "models": {
            "team": {"repo": "org/t", "revision": "r1", "estimated_memory_gb": 70},
            "huge": {"repo": "org/h", "revision": "r1", "estimated_memory_gb": 20},
        },
        "roles": {r: {"prompt": "p.md", "model": "team"} for r in TEAM_ROLES},
        "teams": {"k": 1},
        "memory": {"wired_limit_mb": 65536},  # 64 GB limit
    }
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    cfg = load_config(tmp_path / "airlie.yaml")
    rows = by_name(run_checks(cfg, FakeProbe(snapshots={"org/t@r1", "org/h@r1"})))
    assert rows["engine:memory_floor[teams]"].status == "fail"
    assert rows["engine:memory_floor[teams]"].detail.startswith("pinned engines")
    assert "vs wired limit 64" in rows["engine:memory_floor[teams]"].detail
    # projection row is under the first autostart engine (main)
    assert any("memory_projection" in k for k in rows), list(rows.keys())
    proj_row = next(r for r in rows.values() if "memory_projection" in r.name)
    assert proj_row.status == "fail"


def test_an_overcommit_that_only_everything_resident_causes_is_a_warn(tmp_path: Path) -> None:
    """Pinned 16 GB of weights + 12 GB reserve fits; the on-demand engine's 90 GB does not fit beside it."""
    data = {
        "version": 1,
        "engines": {
            "main": {"kind": "mlx_vlm", "port": 8080, "model": "team", "max_kv_size": 1024},
            "big": {"kind": "mlx_vlm", "port": 8081, "model": "huge", "lifecycle": "on_demand"},
        },
        "models": {
            "team": {"repo": "org/t", "revision": "r1", "estimated_memory_gb": 16},
            "huge": {"repo": "org/h", "revision": "r1", "estimated_memory_gb": 90},
        },
        "roles": {r: {"prompt": "p.md", "model": "team"} for r in TEAM_ROLES},
        "teams": {"k": 1},
    }
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    cfg = load_config(tmp_path / "airlie.yaml")
    rows = by_name(run_checks(cfg, FakeProbe(snapshots={"org/t@r1", "org/h@r1"})))
    row = rows["engine:big:memory_projection[teams]"]  # the row sits under the first autostart engine
    assert row.status == "warn"
    assert "the manager will queue or stop idle engines" in row.detail
    assert rows["engine:memory_floor[teams]"].status == "ok"
