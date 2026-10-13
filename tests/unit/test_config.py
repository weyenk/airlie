"""Config schema: airlie.yaml is validated with path-qualified errors and deep-merged with local overrides."""

from pathlib import Path

import pytest
import yaml

from airlie.config import AirlieConfig, ConfigError, load_config

MINIMAL: dict = {
    "version": 1,
    "engines": {"main": {"kind": "vllm_mlx", "port": 8080, "models": ["team"]}},
    "models": {"team": {"repo": "mlx-community/Qwen3.8-27B-4bit", "revision": "abc123"}},
    "roles": {
        "coder": {"prompt": "prompts/roles/coder.md", "model": "team"},
        "summarizer": {"prompt": "prompts/roles/summarizer.md", "model": "team"},
        "judge": {"prompt": "prompts/roles/judge.md", "model": "team"},
        "aggregator": {"prompt": "prompts/roles/aggregator.md", "model": "team"},
    },
}


def write(tmp_path: Path, data: dict, name: str = "airlie.yaml") -> Path:
    p = tmp_path / name
    p.write_text(yaml.safe_dump(data))
    for role in data.get("roles", {}).values():
        (tmp_path / role["prompt"]).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / role["prompt"]).write_text("# prompt\n")
    return p


def test_minimal_config_loads_with_defaults(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, MINIMAL))
    assert isinstance(cfg, AirlieConfig)
    assert cfg.teams.k == 3
    assert cfg.teams.role == "coder"
    assert cfg.lts.controller.kind == "heuristic"
    assert cfg.scheduler.max_active == 4
    # prompt paths resolve relative to the config file, not the cwd
    assert cfg.roles["coder"].prompt == (tmp_path / "prompts/roles/coder.md").resolve()
    # a role's engine is derived from which engine serves its model
    assert cfg.engine_for_role("coder") == "main"


def test_role_referencing_unknown_model_fails_with_named_error(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "roles": {**MINIMAL["roles"], "coder": {"prompt": "prompts/roles/coder.md", "model": "nope"}},
    }
    with pytest.raises(ConfigError, match=r"roles\.coder\.model.*'nope'.*models"):
        load_config(write(tmp_path, data))


def test_engine_serving_unknown_model_fails(tmp_path: Path) -> None:
    data = {**MINIMAL, "engines": {"main": {"kind": "vllm_mlx", "port": 8080, "models": ["ghost"]}}}
    with pytest.raises(ConfigError, match=r"engines\.main\.models.*'ghost'"):
        load_config(write(tmp_path, data))


def test_model_used_by_a_role_but_served_by_no_engine_fails(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "models": {**MINIMAL["models"], "orphan": {"repo": "x/y", "revision": "r"}},
        "roles": {**MINIMAL["roles"], "judge": {"prompt": "prompts/roles/judge.md", "model": "orphan"}},
    }
    with pytest.raises(ConfigError, match=r"models\.orphan.*no engine"):
        load_config(write(tmp_path, data))


def test_model_served_by_two_engines_fails(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "engines": {
            "main": {"kind": "vllm_mlx", "port": 8080, "models": ["team"]},
            "dup": {"kind": "vllm_mlx", "port": 8081, "models": ["team"]},
        },
    }
    with pytest.raises(ConfigError, match=r"models\.team.*(main.*dup|dup.*main)"):
        load_config(write(tmp_path, data))


def test_missing_prompt_file_fails(tmp_path: Path) -> None:
    p = write(tmp_path, MINIMAL)
    (tmp_path / "prompts/roles/judge.md").unlink()
    with pytest.raises(ConfigError, match=r"roles\.judge\.prompt.*judge\.md"):
        load_config(p)


def test_unknown_key_is_rejected_to_catch_typos(tmp_path: Path) -> None:
    data = {**MINIMAL, "teams": {"k": 3, "rol": "coder"}}
    with pytest.raises(ConfigError, match=r"teams.*rol"):
        load_config(write(tmp_path, data))


def test_controller_kind_must_be_known(tmp_path: Path) -> None:
    data = {**MINIMAL, "lts": {"controller": {"kind": "magic"}}}
    with pytest.raises(ConfigError, match=r"lts\.controller\.kind"):
        load_config(write(tmp_path, data))


def test_openai_http_engine_requires_base_url(tmp_path: Path) -> None:
    data = {**MINIMAL, "engines": {"main": {"kind": "openai_http", "models": ["team"]}}}
    with pytest.raises(ConfigError, match=r"engines\.main.*base_url"):
        load_config(write(tmp_path, data))


def test_teams_role_must_exist(tmp_path: Path) -> None:
    data = {**MINIMAL, "teams": {"role": "architect"}}
    with pytest.raises(ConfigError, match=r"teams\.role.*'architect'"):
        load_config(write(tmp_path, data))


def test_two_managed_engines_cannot_share_a_port(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "models": {**MINIMAL["models"], "small": {"repo": "x/y", "revision": "r"}},
        "engines": {
            "main": {"kind": "vllm_mlx", "port": 8080, "models": ["team"]},
            "aux": {"kind": "vllm_mlx", "port": 8080, "models": ["small"]},
        },
    }
    with pytest.raises(ConfigError, match=r"port 8080.*(main.*aux|aux.*main)"):
        load_config(write(tmp_path, data))


def test_vllm_mlx_engine_defaults_match_the_spec(tmp_path: Path) -> None:
    e = load_config(write(tmp_path, MINIMAL)).engines["main"]
    assert e.host == "127.0.0.1"
    assert e.base_url == "http://127.0.0.1:8080/v1"
    assert e.continuous_batching is True
    assert e.max_num_seqs == 16
    assert e.max_kv_size == 65536
    assert e.prefill_step_size == 2048
    assert e.extra_args == []


def test_local_overrides_deep_merge(tmp_path: Path) -> None:
    base = write(tmp_path, MINIMAL)
    local = tmp_path / "airlie.local.yaml"
    local.write_text(
        yaml.safe_dump(
            {
                "teams": {"k": 1},
                "engines": {"main": {"kind": "openai_http", "base_url": "http://127.0.0.1:1/v1"}},
            }
        )
    )
    cfg = load_config(base, local)
    assert cfg.teams.k == 1
    assert cfg.teams.role == "coder"  # untouched sibling survives the merge
    e = cfg.engines["main"]
    assert e.kind == "openai_http"
    assert e.base_url == "http://127.0.0.1:1/v1"
    assert e.models == ["team"]  # untouched sibling survives the merge


def test_local_override_is_optional(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, MINIMAL), tmp_path / "does-not-exist.yaml")
    assert cfg.teams.k == 3


def test_committed_default_config_is_valid() -> None:
    root = Path(__file__).resolve().parents[2]
    cfg = load_config(root / "airlie.yaml")
    assert {"coder", "summarizer", "judge", "aggregator"} <= set(cfg.roles)
    assert {"base", "llama70b", "qwen25_32b"} <= {n for n, m in cfg.models.items() if m.bench}
    assert cfg.engines["main"].kind == "vllm_mlx"
    for name, m in cfg.models.items():
        assert m.revision, f"models.{name} must pin a revision"
    for role in cfg.roles:
        assert cfg.engine_for_role(role) == "main"


# ---- M0 Task 2: catalog fields ----


def test_model_bench_and_tags_default_off(tmp_path: Path) -> None:
    m = load_config(write(tmp_path, MINIMAL)).models["team"]
    assert m.bench is False
    assert m.tags == []
    assert m.kv_mb_per_1k_tokens == 256.0


def test_role_may_use_non_preloaded_model(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "models": {"team": {**MINIMAL["models"]["team"], "preload": False, "bench": True, "tags": ["spec"]}},
    }
    cfg = load_config(write(tmp_path, data))
    assert cfg.models["team"].preload is False
    assert cfg.models["team"].bench is True
    assert cfg.models["team"].tags == ["spec"]


# ---- M0 Task 4: aggregator role, orchestrator and summarizer kinds ----


def test_aggregator_default_is_llm_with_role(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "roles": {
            **MINIMAL["roles"],
            "aggregator": {"prompt": "prompts/roles/aggregator.md", "model": "team"},
        },
    }
    cfg = load_config(write(tmp_path, data))
    assert cfg.aggregator.kind == "llm"
    assert cfg.aggregator.role == "aggregator"


def test_missing_aggregator_role_fails(tmp_path: Path) -> None:
    data = {**MINIMAL, "roles": {k: v for k, v in MINIMAL["roles"].items() if k != "aggregator"}}
    with pytest.raises(ConfigError, match=r"aggregator\.role names 'aggregator'"):
        load_config(write(tmp_path, data))


def test_teams_orchestrator_default_is_autogen(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "roles": {
            **MINIMAL["roles"],
            "aggregator": {"prompt": "prompts/roles/aggregator.md", "model": "team"},
        },
    }
    cfg = load_config(write(tmp_path, data))
    assert cfg.teams.orchestrator == "autogen"


def test_summarizer_kind_default_is_role(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "roles": {
            **MINIMAL["roles"],
            "aggregator": {"prompt": "prompts/roles/aggregator.md", "model": "team"},
        },
    }
    cfg = load_config(write(tmp_path, data))
    assert cfg.lts.summarizer.kind == "role"
    assert cfg.lts.summarizer.truncate_chars == 100


def test_topology_and_fork_defaults(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, MINIMAL))
    assert cfg.topology.kind == "teams"
    assert cfg.fork.width == 2 and cfg.fork.after_failures == 2 and cfg.fork.child_steps == 15
    assert cfg.fork.max_rounds == 3 and cfg.fork.max_total_steps == 120
    assert cfg.fork.diversify == "hypotheses" and cfg.fork.hypothesis_role == "judge"
    assert cfg.fork.temperatures == [0.3, 0.7]
    assert cfg.repo_map.enabled is True


def test_fork_hypothesis_role_must_exist(tmp_path: Path) -> None:
    data = {**MINIMAL, "fork": {"hypothesis_role": "nobody"}}
    with pytest.raises(ConfigError, match=r"fork\.hypothesis_role"):
        load_config(write(tmp_path, data))
