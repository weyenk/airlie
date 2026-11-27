"""`auth.oidc.external_url`: the origin airlie is reached at, which is what a redirect_uri is built from.

It is configured rather than derived from the request because serve runs uvicorn with proxy_headers=False
for every bind (serve/server.py:51), so a Host header is whatever an off-box peer asserts. A wrong value here
sends `state`, PKCE and the client secret to a host the caller chose. See the spec's security notes.

Every refusal is matched on the key prefix as well as the message, because pydantic reports a mistyped key as
"Extra inputs are not permitted" — a match on the message alone would pass for the wrong reason."""

from pathlib import Path

import pytest

from airlie.config import ConfigError, load_config
from tests.support.config import MINIMAL, write

NOT_AN_ORIGIN = r"auth\.oidc\.external_url: Value error, external_url must be an https origin"
NO_PATH = r"auth\.oidc\.external_url: Value error, external_url must carry no path"


def oidc(**overrides: object) -> dict:
    return {
        "enabled": True,
        "issuer": "https://acme.okta.com",
        "client_id": "0oa123",
        "client_secret_file": "~/.config/airlie/oidc-secret",
        "external_url": "https://airlie.acme.com",
        **overrides,
    }


def loaded(tmp_path: Path, **overrides: object):  # noqa: ANN201 - the loaded type is the loader's business
    return load_config(write(tmp_path, {**MINIMAL, "auth": {"oidc": oidc(**overrides)}}))


def test_off_by_default_needs_no_external_url(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, MINIMAL))
    assert cfg.auth.oidc.external_url is None


def test_an_enabled_oidc_needs_an_external_url(tmp_path: Path) -> None:
    data = {**MINIMAL, "auth": {"oidc": oidc(external_url=None)}}
    with pytest.raises(ConfigError, match=r"auth\.oidc\.enabled needs auth\.oidc\.external_url"):
        load_config(write(tmp_path, data))


def test_an_external_url_loads_as_written(tmp_path: Path) -> None:
    assert loaded(tmp_path).auth.oidc.external_url == "https://airlie.acme.com"


def test_an_http_external_url_is_refused(tmp_path: Path) -> None:
    """An http:// redirect_uri carries the code and the token exchange over the open network, and most IdPs
    refuse to register one at all — refuse the config rather than fail someone's first sign-in."""
    with pytest.raises(ConfigError, match=NOT_AN_ORIGIN):
        loaded(tmp_path, external_url="http://airlie.acme.com")


def test_an_external_url_with_a_path_is_refused(tmp_path: Path) -> None:
    """The redirect_uri is this origin plus a fixed path, so a path here would produce a URI no IdP has
    registered, and the failure would surface as the IdP rejecting the request."""
    with pytest.raises(ConfigError, match=NO_PATH):
        loaded(tmp_path, external_url="https://airlie.acme.com/studio")


def test_an_external_url_with_a_trailing_slash_is_refused(tmp_path: Path) -> None:
    """`https://host/` plus the path is `https://host//callback`, which will not match the registration."""
    with pytest.raises(ConfigError, match=NO_PATH):
        loaded(tmp_path, external_url="https://airlie.acme.com/")


def test_an_external_url_with_a_port_is_allowed(tmp_path: Path) -> None:
    """Airlie is often served on a non-standard port; that is a legitimate origin, not a mistake."""
    cfg = loaded(tmp_path, external_url="https://airlie.acme.com:8443")
    assert cfg.auth.oidc.external_url == "https://airlie.acme.com:8443"


def test_an_external_url_with_a_query_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=NOT_AN_ORIGIN):
        loaded(tmp_path, external_url="https://airlie.acme.com?next=/admin")


def test_an_external_url_with_user_info_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=NOT_AN_ORIGIN):
        loaded(tmp_path, external_url="https://admin:pw@airlie.acme.com")


def test_an_external_url_with_no_host_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=NOT_AN_ORIGIN):
        loaded(tmp_path, external_url="https://")


def test_an_external_url_with_an_empty_port_is_refused(tmp_path: Path) -> None:
    """`https://host:` would be dialled and fail, and the IdP would reject the URI it arrives in."""
    with pytest.raises(ConfigError, match=NOT_AN_ORIGIN):
        loaded(tmp_path, external_url="https://airlie.acme.com:")


def test_an_external_url_that_is_not_a_url_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=NOT_AN_ORIGIN):
        loaded(tmp_path, external_url="airlie.acme.com")
