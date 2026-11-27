"""`auth.oidc`: off in the schema, so an install that never mentions it changes nothing; and an enabled one
must be able to actually sign someone in — an issuer or client id missing would otherwise only show up when a
person hits /v1/auth/oidc/start and gets a 503 they cannot diagnose."""

from pathlib import Path

import pytest

from airlie.config import ConfigError, load_config
from tests.support.config import MINIMAL, write


def enabled(**overrides: object) -> dict:
    return {
        "enabled": True,
        "issuer": "https://acme.okta.com",
        "client_id": "0oa123",
        "client_secret_file": "~/.config/airlie/oidc-secret",
        "external_url": "https://airlie.acme.com",
        **overrides,
    }


def test_off_by_default_in_the_schema(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, MINIMAL))
    assert not cfg.auth.oidc.enabled
    assert cfg.auth.oidc.issuer is None
    assert cfg.auth.oidc.client_id is None
    assert cfg.auth.oidc.session_hours == 12


def test_an_enabled_oidc_needs_an_issuer(tmp_path: Path) -> None:
    data = {**MINIMAL, "auth": {"oidc": enabled(issuer=None)}}
    with pytest.raises(ConfigError, match="auth.oidc.issuer"):
        load_config(write(tmp_path, data))


def test_an_enabled_oidc_needs_a_client_id(tmp_path: Path) -> None:
    data = {**MINIMAL, "auth": {"oidc": enabled(client_id=None)}}
    with pytest.raises(ConfigError, match="auth.oidc.client_id"):
        load_config(write(tmp_path, data))


def test_an_enabled_oidc_names_its_secret_file(tmp_path: Path) -> None:
    data = {**MINIMAL, "auth": {"oidc": enabled(client_secret_file=None)}}
    with pytest.raises(ConfigError, match="auth.oidc.client_secret_file"):
        load_config(write(tmp_path, data))


def test_disabled_oidc_needs_nothing(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, {**MINIMAL, "auth": {"oidc": {"enabled": False}}}))
    assert not cfg.auth.oidc.enabled


def test_a_enabled_oidc_loads_with_its_defaults(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, {**MINIMAL, "auth": {"oidc": enabled()}}))
    assert cfg.auth.oidc.enabled
    assert cfg.auth.oidc.issuer == "https://acme.okta.com"
    assert cfg.auth.oidc.scopes == ["openid", "profile", "email"]
    assert cfg.auth.oidc.groups_claim is None
    assert cfg.auth.oidc.allowed_domains == []
    assert cfg.auth.oidc.allowed_groups == []


def test_the_issuer_is_an_https_origin_without_a_path(tmp_path: Path) -> None:
    """A trailing slash or a path would not equal the `iss` claim and every sign-in would be refused."""
    data = {**MINIMAL, "auth": {"oidc": enabled(issuer="https://acme.okta.com/oauth2/default/")}}
    with pytest.raises(ConfigError, match="auth.oidc.issuer"):
        load_config(write(tmp_path, data))


def test_a_groups_gate_needs_to_know_the_claim(tmp_path: Path) -> None:
    """allowed_groups with no claim name to read would gate on nothing and allow everyone."""
    data = {**MINIMAL, "auth": {"oidc": enabled(allowed_groups=["airlie-users"])}}
    with pytest.raises(ConfigError, match="auth.oidc.groups_claim"):
        load_config(write(tmp_path, data))


def test_an_unknown_key_is_a_config_error(tmp_path: Path) -> None:
    """AuthSpec is Strict, so a typo in a security setting fails at load instead of quietly doing nothing."""
    data = {**MINIMAL, "auth": {"oidc": enabled(alowed_domains=["acme.com"])}}
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, data))
