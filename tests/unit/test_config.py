"""Config schema: airlie.yaml is validated with path-qualified errors and deep-merged with local overrides."""

import os
import traceback
from pathlib import Path

import pytest
import yaml

from airlie.config import AirlieConfig, ConfigError, load_config, profile_configs

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


def test_teams_orchestrator_default_is_native(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "roles": {
            **MINIMAL["roles"],
            "aggregator": {"prompt": "prompts/roles/aggregator.md", "model": "team"},
        },
    }
    cfg = load_config(write(tmp_path, data))
    assert (
        cfg.teams.orchestrator == "native"
    )  # 2026-10-13: native by default, autogen (spec 01 §2) selectable


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


def test_profile_for_the_selected_kind_is_deep_merged(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "topology": {
            "kind": "teams",
            "profiles": {
                "single": {"teams": {"k": 1}, "lts": {"enabled": False}},
                "teams": {"teams": {"k": 3}},
            },
        },
    }
    cfg = load_config(write(tmp_path, data), topology="single")
    assert cfg.topology.kind == "single"
    assert cfg.teams.k == 1 and cfg.lts.enabled is False
    assert cfg.source_path == (tmp_path / "airlie.yaml").resolve()
    base = load_config(tmp_path / "airlie.yaml")  # no override → the file's own kind
    assert base.topology.kind == "teams" and base.teams.k == 3


def test_profile_extends_chain_merges_parent_first(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "topology": {
            "kind": "fork",
            "profiles": {
                "teams": {"teams": {"k": 3, "max_steps": 20}},
                "fork": {"extends": "teams", "teams": {"k": 1}},
            },
        },
    }
    cfg = load_config(write(tmp_path, data))
    assert cfg.teams.k == 1 and cfg.teams.max_steps == 20


def test_profile_extends_cycle_is_a_config_error(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "topology": {
            "kind": "fork",
            "profiles": {"teams": {"extends": "fork"}, "fork": {"extends": "teams"}},
        },
    }
    with pytest.raises(ConfigError, match=r"topology\.profiles\.fork\.extends.*cycle"):
        load_config(write(tmp_path, data))


def test_profile_extends_unknown_parent_is_a_config_error(tmp_path: Path) -> None:
    data = {**MINIMAL, "topology": {"kind": "fork", "profiles": {"fork": {"extends": "nope"}}}}
    with pytest.raises(ConfigError, match=r"topology\.profiles\.fork\.extends.*'nope'"):
        load_config(write(tmp_path, data))


def test_unknown_topology_override_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"topology.*'ring'.*single, teams, fork, studio"):
        load_config(write(tmp_path, MINIMAL), topology="ring")


def test_jev_router_needs_a_llama_server_engine_serving_its_model(tmp_path: Path) -> None:
    data = {**MINIMAL, "router": {"kind": "jev", "model": "team"}}
    with pytest.raises(ConfigError, match=r"router\.model.*'team'.*llama_server"):
        load_config(write(tmp_path, data))


def test_llama_server_engine_serves_one_gguf_model(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "engines": {
            **MINIMAL["engines"],
            "decision": {"kind": "llama_server", "port": 8081, "models": ["kev"]},
        },
        "models": {**MINIMAL["models"], "kev": {"repo": "ggml-org/Kev-4B-GGUF", "revision": "d9"}},
        "router": {"kind": "jev", "model": "kev"},
    }
    with pytest.raises(ConfigError, match=r"models\.kev\.file.*llama_server"):
        load_config(write(tmp_path, data))
    data["models"]["kev"]["file"] = "Kev-4B-Q4_K_M.gguf"
    cfg = load_config(write(tmp_path, data))
    assert cfg.engines["decision"].base_url == "http://127.0.0.1:8081/v1"
    assert cfg.engines["decision"].server_parsers is True and cfg.engines["decision"].ctx_size == 16384
    assert cfg.router.kind == "jev" and cfg.router.ambiguous_gap == 0.15


def test_profile_configs_loads_every_profile_from_the_source_file(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "topology": {"kind": "teams", "profiles": {"single": {"teams": {"k": 1}}, "teams": {}}},
    }
    cfg = load_config(write(tmp_path, data))
    profiles = profile_configs(cfg)
    assert set(profiles) == {"single", "teams"}
    assert profiles["single"].teams.k == 1 and profiles["teams"].teams.k == 3


def test_a_profile_outside_the_kinds_can_be_a_parent_and_be_selected(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "topology": {
            "kind": "teams",
            "profiles": {
                "studio-qwen38": {"teams": {"k": 2}},
                "single": {"extends": "studio-qwen38"},
                "fast": {"topology": {"kind": "fork"}, "teams": {"max_steps": 5}},
            },
        },
    }
    p = write(tmp_path, data)
    assert load_config(p, topology="single").teams.k == 2
    chosen = load_config(p, topology="studio-qwen38")  # a profile name selects it; kind stays the file's
    assert chosen.topology.kind == "teams" and chosen.teams.k == 2
    fast = load_config(p, topology="fast")  # a profile may set the kind itself
    assert fast.topology.kind == "fork" and fast.teams.max_steps == 5
    assert set(profile_configs(load_config(p))) == {"studio-qwen38", "single", "fast"}


STUDIO_ROLES = {
    "chief": {"prompt": "prompts/roles/chief.md", "model": "team", "tools": []},
    "reviewer": {"prompt": "prompts/roles/reviewer.md", "model": "team", "tools": []},
    "security": {"prompt": "prompts/roles/security.md", "model": "team", "tools": ["read_file"]},
    "qa": {
        "prompt": "prompts/roles/qa.md",
        "model": "team",
        "skills": "skills/qa",
        "tools": ["read_file", "run_tests"],
    },
}


def test_studio_kind_loads_with_defaults_and_role_fields(tmp_path: Path) -> None:
    data = {**MINIMAL, "roles": {**MINIMAL["roles"], **STUDIO_ROLES}, "topology": {"kind": "studio"}}
    (tmp_path / "skills" / "qa").mkdir(parents=True)
    cfg = load_config(write(tmp_path, data))
    assert cfg.topology.kind == "studio"
    assert cfg.studio.chief_role == "chief" and cfg.studio.review.roles == ["coder", "qa", "security"]
    assert cfg.studio.parallel == 3 and cfg.studio.review.rounds == 1
    assert cfg.roles["qa"].skills == (tmp_path / "skills" / "qa").resolve()
    assert cfg.roles["qa"].tools == ["read_file", "run_tests"] and cfg.roles["coder"].tools is None
    assert cfg.roles["chief"].vision is False and cfg.roles["chief"].max_steps is None


def test_studio_kind_requires_chief_and_reviewer_roles(tmp_path: Path) -> None:
    data = {**MINIMAL, "topology": {"kind": "studio"}}
    with pytest.raises(ConfigError, match=r"studio\.chief_role.*'chief'"):
        load_config(write(tmp_path, data))


def test_role_tools_must_be_known(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "roles": {**MINIMAL["roles"], "coder": {**MINIMAL["roles"]["coder"], "tools": ["teleport"]}},
    }
    with pytest.raises(ConfigError, match=r"roles\.coder\.tools.*'teleport'"):
        load_config(write(tmp_path, data))


def test_role_skills_dir_must_exist(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "roles": {**MINIMAL["roles"], "coder": {**MINIMAL["roles"]["coder"], "skills": "skills/nope"}},
    }
    with pytest.raises(ConfigError, match=r"roles\.coder\.skills.*not a directory"):
        load_config(write(tmp_path, data))


def test_named_variant_inherits_the_kind_of_the_profile_it_extends(tmp_path: Path) -> None:
    data = {
        **MINIMAL,
        "topology": {
            "kind": "single",
            "profiles": {
                "fork": {"teams": {"k": 1}},
                "fork-hot": {"extends": "fork", "teams": {"max_steps": 3}},
            },
        },
    }
    cfg = load_config(write(tmp_path, data), topology="fork-hot")
    assert cfg.topology.kind == "fork" and cfg.teams.max_steps == 3


# ---- stories (2026-10-14) ----


def test_story_defaults(tmp_path: Path) -> None:
    s = load_config(write(tmp_path, MINIMAL)).story
    assert (s.topology, s.trunk, s.remote) == ("studio", "main", "origin")
    assert (s.max_layers, s.stage_attempts) == (5, 2)
    roles = (s.product_role, s.architect_role, s.tests_role, s.build_role)
    assert roles == ("product", "architect", "qa", "coder")
    assert s.repo is None and s.test_cmd is None
    j = s.jira
    assert (j.site, j.project, j.email, j.token, j.jql, j.api_url) == (None,) * 6
    statuses = (j.statuses.intake, j.statuses.building, j.statuses.review, j.statuses.blocked)
    assert statuses == ("To Do", "In Progress", "In Review", "Blocked")


def test_story_repo_resolves_beside_the_config_and_typos_fail(tmp_path: Path) -> None:
    jira = {"site": "https://acme.atlassian.net", "project": "AIR", "statuses": {"blocked": None}}
    cfg = load_config(write(tmp_path, {**MINIMAL, "story": {"repo": "../proj", "jira": jira}}))
    assert cfg.story.repo == (tmp_path / "../proj").resolve()
    assert (cfg.story.jira.project, cfg.story.jira.statuses.blocked) == ("AIR", None)
    with pytest.raises(ConfigError, match="story"):
        load_config(write(tmp_path, {**MINIMAL, "story": {"tickets": "x"}}))
    with pytest.raises(ConfigError, match="story.jira.board"):
        load_config(write(tmp_path, {**MINIMAL, "story": {"jira": {"board": "B1"}}}))


def test_jira_secrets_never_print(tmp_path: Path) -> None:
    jira = {
        "site": "https://acme.atlassian.net",
        "project": "AIR",
        "email": "me@example.org",
        "token": "t0ken",
    }
    cfg = load_config(write(tmp_path, {**MINIMAL, "story": {"jira": jira}}))
    token = cfg.story.jira.token
    assert token is not None and token.get_secret_value() == "t0ken"
    shown = yaml.safe_dump(cfg.model_dump(mode="json"))  # what `task config` prints
    assert "t0ken" not in shown


def test_unit_tests_never_see_the_atlassian_variables() -> None:
    names = ("ATLASSIAN_API_KEY", "ATLASSIAN_EMAIL", "ATLASSIAN_BASE_URL", "ATLASSIAN_PROJECT_KEY")
    # tests/unit/conftest.py removes any the person exported in their shell
    assert [n for n in names if n in os.environ] == []


def test_the_taskfile_does_not_export_dotenv() -> None:
    # Task would put .env (the Jira token) in the environment of every task's process, and so of the coder's
    # shell tool, the test gate, gh, git and the engines; airlie reads .env itself, only where it needs it.
    root = Path(__file__).resolve().parents[2]
    taskfile = yaml.safe_load((root / "Taskfile.yml").read_text())
    assert taskfile.get("dotenv") is None
    assert [name for name, task in taskfile["tasks"].items() if "dotenv" in task] == []


# ---- no secret in a config error (review, 2026-10-14) ----
# "t0ken-SYNTHETIC" stands in for a Jira token written into airlie.local.yaml. An error, and the traceback of
# everything it chains, must say where the file is wrong and never repeat what it holds.


@pytest.mark.parametrize(
    ("source", "reported"),
    [
        pytest.param(
            'story: {jira: {token: "t0ken-SYNTHETIC',
            "not valid YAML at line 1, column 39: while scanning a quoted scalar, "
            "found unexpected end of stream",
            id="unclosed quote",
        ),
        pytest.param(
            "story:\n  jira:\n    token: ab: t0ken-SYNTHETIC\n",
            "not valid YAML at line 3, column 14: mapping values are not allowed here",
            id="stray colon",
        ),
        pytest.param(
            "story: {jira: {token: @t0ken-SYNTHETIC}}\n",
            "not valid YAML at line 1, column 23: while scanning for the next token, "
            "found character '@' that cannot start any token",
            id="leading @",
        ),
        pytest.param(
            "story:\n\tjira: {token: t0ken-SYNTHETIC}\n",
            "not valid YAML at line 2, column 1: while scanning for the next token, "
            "found character '\\t' that cannot start any token",
            id="tab",
        ),
        pytest.param(
            "story: {jira: {token: *t0ken-SYNTHETIC}}\n",
            "not valid YAML at line 1, column 23: found undefined alias '…'",
            id="alias",
        ),
        pytest.param(
            "story: {jira: {token: !t0ken-SYNTHETIC x}}\n",
            "not valid YAML at line 1, column 23: could not determine a constructor for the tag '…'",
            id="tag",
        ),
        pytest.param(
            "story: {jira: {token: t0ken-SYNTHETIC\x00}}\n",
            "not valid YAML: special characters are not allowed",
            id="control character",
        ),
    ],
)
def test_a_yaml_error_names_the_place_and_never_quotes_the_source(
    tmp_path: Path, source: str, reported: str
) -> None:
    base = write(tmp_path, MINIMAL)
    local = tmp_path / "airlie.local.yaml"
    local.write_text(source)
    with pytest.raises(ConfigError) as err:
        load_config(base)
    assert "t0ken-SYNTHETIC" not in "".join(traceback.format_exception(err.value))
    assert str(err.value) == f"{local.resolve()}: {reported}"


def test_a_mistyped_key_never_chains_its_value_into_the_traceback(tmp_path: Path) -> None:
    base = write(tmp_path, MINIMAL)
    (tmp_path / "airlie.local.yaml").write_text("story: {jira: {api_key: t0ken-SYNTHETIC}}\n")
    with pytest.raises(ConfigError) as err:
        load_config(base)
    assert "t0ken-SYNTHETIC" not in "".join(traceback.format_exception(err.value))
    assert str(err.value) == "config invalid:\n  story.jira.api_key: Extra inputs are not permitted"
