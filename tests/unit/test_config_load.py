"""Config loader helpers: exact messages and lookup order (pinned for mutation testing)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError, model_validator

from airlie.config.load import (
    ConfigError,
    _format_validation_error,
    find_config,
    resolve_profile,
)


def _validation_error(model: type[BaseModel], **data: object) -> ValidationError:
    with pytest.raises(ValidationError) as ei:
        model.model_validate(data)
    return ei.value


def test_validation_errors_are_listed_one_per_line_with_dotted_key_paths() -> None:
    class Inner(BaseModel):
        n: int

    class M(BaseModel):
        a: int
        inner: Inner

    msg = _format_validation_error(_validation_error(M, inner={}))
    assert msg == "config invalid:\n  a: Field required\n  inner.n: Field required"


def test_root_level_validation_errors_are_labelled_root() -> None:
    class M(BaseModel):
        a: int = 0

        @model_validator(mode="after")
        def boom(self) -> M:
            raise ValueError("nope")

    assert _format_validation_error(_validation_error(M)) == "config invalid:\n  <root>: Value error, nope"


def test_find_config_env_override_wins_and_expands_user(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "airlie.yaml").write_text("x: 1")
    elsewhere = tmp_path / "elsewhere.yaml"
    monkeypatch.setenv("AIRLIE_CONFIG", str(elsewhere))
    assert find_config(tmp_path) == elsewhere.resolve()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AIRLIE_CONFIG", "~/elsewhere.yaml")
    assert find_config(tmp_path) == elsewhere.resolve()


def test_find_config_walks_up_from_start_and_defaults_to_cwd(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("AIRLIE_CONFIG", raising=False)
    (tmp_path / "airlie.yaml").write_text("x: 1")
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert find_config(deep) == tmp_path.resolve() / "airlie.yaml"
    monkeypatch.chdir(deep)
    assert find_config() == tmp_path.resolve() / "airlie.yaml"
    nested = tmp_path / "n"
    nested.mkdir()
    (nested / "airlie.yaml").write_text("y: 1")
    assert find_config(nested) == nested.resolve() / "airlie.yaml"  # nearest file wins


def test_find_config_missing_names_the_search_start(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("AIRLIE_CONFIG", raising=False)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ConfigError) as ei:
        find_config(empty)
    assert str(ei.value) == (
        f"no airlie.yaml found in {empty.resolve()} or its parents (set AIRLIE_CONFIG to point at one)"
    )


def test_resolve_profile_cycle_message_shows_the_loop() -> None:
    profiles = {"a": {"extends": "b"}, "b": {"extends": "a"}}
    with pytest.raises(ConfigError) as ei:
        resolve_profile(profiles, "a")
    assert str(ei.value) == "topology.profiles.a.extends: cycle a -> b -> a"


def test_resolve_profile_unknown_names_say_where_they_were_referenced() -> None:
    with pytest.raises(ConfigError) as ei:
        resolve_profile({"a": {}}, "zzz")
    assert str(ei.value) == "topology.profiles names 'zzz', which is not a profile (['a'])"
    with pytest.raises(ConfigError) as ei:
        resolve_profile({"a": {"extends": "zzz"}}, "a")
    assert str(ei.value) == "topology.profiles.a.extends names 'zzz', which is not a profile (['a'])"


def test_resolve_profile_non_mapping_body_is_rejected() -> None:
    with pytest.raises(ConfigError) as ei:
        resolve_profile({"a": 5}, "a")
    assert str(ei.value) == "topology.profiles.a must be a mapping"


def test_resolve_profile_merges_the_extends_chain_parent_first_and_drops_extends() -> None:
    profiles = {
        "leaf": {"extends": "mid", "x": 1},
        "mid": {"extends": "root", "x": 0, "y": {"a": 1}},
        "root": {"y": {"b": 2}, "z": 3},
    }
    assert resolve_profile(profiles, "leaf") == {"x": 1, "y": {"a": 1, "b": 2}, "z": 3}
    assert resolve_profile(profiles, "root") == {"y": {"b": 2}, "z": 3}
