"""Cross-reference problems and loader errors, pinned to their exact wording (the user's only clue)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from airlie.config import AirlieConfig, ConfigError, load_config
from tests.unit.test_config import MINIMAL, write

ROLES = "['aggregator', 'coder', 'judge', 'summarizer']"


def valid(tmp_path: Path) -> dict[str, Any]:
    """MINIMAL with absolute prompt paths that exist, so validate_references() can be called directly."""
    data = copy.deepcopy(MINIMAL)
    for name, role in data["roles"].items():
        prompt = tmp_path / f"{name}.md"
        prompt.write_text("# prompt\n")
        role["prompt"] = str(prompt)
    return data


def problems(data: dict[str, Any]) -> list[str]:
    return AirlieConfig.model_validate(data).validate_references()


def test_a_consistent_config_has_no_problems(tmp_path: Path) -> None:
    assert problems(valid(tmp_path)) == []


def test_engine_naming_an_unknown_model(tmp_path: Path) -> None:
    data = valid(tmp_path)
    data["engines"]["main"]["models"] = ["team", "ghost"]
    assert problems(data) == ["engines.main.models names 'ghost', which is not in models (['team'])"]


def test_managed_engines_may_not_share_a_port_but_unmanaged_ones_may(tmp_path: Path) -> None:
    data = valid(tmp_path)
    data["models"]["kev"] = {"repo": "o/kev", "revision": "r", "file": "k.gguf"}
    data["engines"]["dec"] = {"kind": "llama_server", "port": 8080, "models": ["kev"]}
    assert problems(data) == ["port 8080 is used by more than one managed engine: main, dec"]
    data["engines"]["dec"]["port"] = 8081
    data["models"]["spare"] = {"repo": "o/spare", "revision": "r"}
    data["engines"]["ext"] = {
        "kind": "openai_http",
        "port": 8080,
        "base_url": "http://x/v1",
        "models": ["spare"],
    }
    assert problems(data) == []


def test_llama_server_engines_serve_exactly_one_model_with_a_file(tmp_path: Path) -> None:
    data = valid(tmp_path)  # the vllm_mlx engine comes first: a skipped engine must not end the checks
    data["models"]["kev"] = {"repo": "o/kev", "revision": "r"}
    data["models"]["kev2"] = {"repo": "o/kev2", "revision": "r", "file": "k.gguf"}
    data["engines"]["dec"] = {"kind": "llama_server", "port": 8081, "models": ["kev", "kev2"]}
    assert problems(data) == [
        "engines.dec.models: a llama_server engine serves exactly one model",
        "models.kev.file is required (served by llama_server engine 'dec')",
    ]
    data["engines"]["dec"]["models"] = ["kev2"]
    assert problems(data) == []


def test_jev_router_needs_a_catalog_model_served_by_llama_server(tmp_path: Path) -> None:
    data = valid(tmp_path)
    data["router"] = {"kind": "jev"}
    assert problems(data) == ["router.model must name a catalog model when router.kind is jev (got None)"]
    data["router"] = {"kind": "jev", "model": "ghost"}
    assert problems(data) == ["router.model must name a catalog model when router.kind is jev (got 'ghost')"]
    data["router"] = {"kind": "jev", "model": "team"}
    assert problems(data) == ["router.model 'team' must be served by a llama_server engine"]
    data["router"] = {"kind": "chief", "model": "ghost"}
    assert problems(data) == []  # only the jev router needs a decision model


def test_role_problems_do_not_stop_the_checks_of_later_roles(tmp_path: Path) -> None:
    data = valid(tmp_path)
    data["roles"]["coder"]["model"] = "ghost"
    data["roles"]["judge"]["prompt"] = str(tmp_path / "missing.md")
    assert problems(data) == [
        "roles.coder.model names 'ghost', which is not in models (['team'])",
        f"roles.judge.prompt file not found: {tmp_path / 'missing.md'}",
    ]


def test_a_role_model_served_by_no_engine_or_by_two(tmp_path: Path) -> None:
    data = valid(tmp_path)
    data["models"]["loose"] = {"repo": "o/l", "revision": "r"}
    data["roles"]["coder"]["model"] = "loose"
    assert problems(data) == [
        "models.loose (used by roles.coder) is served by no engine; add it to an engine's models list"
    ]
    data = valid(tmp_path)
    data["engines"]["other"] = {"kind": "vllm_mlx", "port": 8090, "models": ["team"]}
    assert problems(data) == ["models.team is served by more than one engine (main, other); pick one"] * 4


@pytest.mark.parametrize(
    ("label", "path"),
    [
        ("teams.role", ("teams", "role")),
        ("lts.summarizer.role", ("lts", "summarizer", "role")),
        ("lts.controller.judge_role", ("lts", "controller", "judge_role")),
        ("aggregator.role", ("aggregator", "role")),
        ("aggregator.judge_role", ("aggregator", "judge_role")),
        ("fork.hypothesis_role", ("fork", "hypothesis_role")),
    ],
)
def test_every_role_reference_is_checked_and_named_in_the_message(
    tmp_path: Path, label: str, path: tuple[str, ...]
) -> None:
    data = valid(tmp_path)
    node = data
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = "nope"
    assert problems(data) == [f"{label} names 'nope', which is not in roles ({ROLES})"]


# ---- loader ----


def test_invalid_yaml_and_non_mapping_files_name_the_file(tmp_path: Path) -> None:
    bad = tmp_path / "airlie.yaml"
    bad.write_text("a: [unclosed\n")
    with pytest.raises(ConfigError) as ei:
        load_config(bad)
    assert str(ei.value).startswith(f"{bad.resolve()}: not valid YAML: ")
    bad.write_text("- 1\n- 2\n")
    with pytest.raises(ConfigError) as ei:
        load_config(bad)
    assert str(ei.value) == f"{bad.resolve()}: top level must be a mapping"


def test_a_missing_config_file_is_named(tmp_path: Path) -> None:
    ghost = tmp_path / "nope.yaml"
    with pytest.raises(ConfigError) as ei:
        load_config(ghost)
    assert str(ei.value) == f"config file not found: {ghost.resolve()}"


def test_problems_are_reported_together_under_one_heading(tmp_path: Path) -> None:
    data = copy.deepcopy(MINIMAL)
    data["engines"]["main"]["models"] = ["team", "ghost"]
    data["roles"]["summarizer"]["model"] = "ghost"
    with pytest.raises(ConfigError) as ei:
        load_config(write(tmp_path, data))
    assert str(ei.value) == (
        "config invalid:\n"
        "  engines.main.models names 'ghost', which is not in models (['team'])\n"
        "  roles.summarizer.model names 'ghost', which is not in models (['team'])"
    )


def test_an_explicit_local_file_overrides_the_default_one(tmp_path: Path) -> None:
    path = write(tmp_path, MINIMAL)
    (tmp_path / "airlie.local.yaml").write_text(yaml.safe_dump({"teams": {"k": 2}}))
    extra = tmp_path / "extra.yaml"
    extra.write_text(yaml.safe_dump({"teams": {"k": 5}}))
    assert load_config(path).teams.k == 2
    assert load_config(path, extra).teams.k == 5


def test_the_learned_controller_adapter_path_is_resolved_like_other_paths(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    data = copy.deepcopy(MINIMAL)
    data["lts"] = {"controller": {"kind": "learned", "adapter_path": "weights/ctl"}}
    cfg = load_config(write(tmp_path, data))
    assert cfg.lts.controller.adapter_path == (tmp_path / "weights" / "ctl").resolve()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    data["lts"]["controller"]["adapter_path"] = "~/ctl"
    cfg = load_config(write(tmp_path, data))
    assert cfg.lts.controller.adapter_path == (tmp_path / "home" / "ctl").resolve()


def test_unknown_topology_messages_list_the_choices(tmp_path: Path) -> None:
    path = write(tmp_path, MINIMAL)
    with pytest.raises(ConfigError) as ei:
        load_config(path, topology="zzz")
    assert str(ei.value) == "unknown topology 'zzz'; use one of single, teams, fork"
    data = copy.deepcopy(MINIMAL)
    data["topology"] = {"profiles": {"slow": {}, "fast": {"topology": {"kind": "single"}}}}
    with pytest.raises(ConfigError) as ei:
        load_config(write(tmp_path, data), topology="zzz")
    assert str(ei.value) == "unknown topology 'zzz'; use one of single, teams, fork or a profile (fast, slow)"


def test_a_profile_with_an_unknown_kind_is_reported_where_it_is_declared(tmp_path: Path) -> None:
    data = copy.deepcopy(MINIMAL)
    data["topology"] = {"profiles": {"odd": {"topology": {"kind": "bogus"}}}}
    with pytest.raises(ConfigError) as ei:
        load_config(write(tmp_path, data), topology="odd")
    assert str(ei.value) == (
        "topology.profiles.odd.topology.kind 'bogus' is unknown; use one of single, teams, fork"
    )
