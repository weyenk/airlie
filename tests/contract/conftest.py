"""Contract-test environment: the same guards the unit tests have (see tests/unit/conftest.py), so no test
here reaches the person's Jira, reads their .env or their airlie.local.yaml, writes their real state
directory, builds an HTTP client that asks macOS for its proxies, or is coloured by their shell. A contract
test pins one boundary (an engine's HTTP wire); its call phase may not run longer than 30 seconds. No
_no_mutation_harvest here: a contract test runs no story beat."""

import os
from pathlib import Path

import pytest

os.environ.setdefault("NO_PROXY", "*")

ATLASSIAN = ("ATLASSIAN_API_KEY", "ATLASSIAN_EMAIL", "ATLASSIAN_BASE_URL", "ATLASSIAN_PROJECT_KEY")
ROOT = Path(__file__).resolve().parents[2]

TIMEOUT_S = 30
"""The seconds a contract test's call phase may run before pytest-timeout fails it (pyproject.toml lists
pytest-timeout); a test that needs longer carries its own `timeout` marker and keeps it."""


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(TIMEOUT_S))


@pytest.fixture(autouse=True)
def _no_atlassian_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ATLASSIAN:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_forced_colour(monkeypatch: pytest.MonkeyPatch) -> None:
    from rich.console import Console

    from airlie import cli

    for name in ("FORCE_COLOR", "CLICOLOR_FORCE", "COLUMNS", "LINES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(cli, "console", Console())
    monkeypatch.setattr(cli, "err", Console(stderr=True))


@pytest.fixture(autouse=True)
def _no_real_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("airlie.story.jira.config_dotenv", lambda cfg: {})
    nowhere = tmp_path / "no-home" / ".config" / "airlie" / ".env"
    monkeypatch.setattr("airlie.story.jira.user_dotenv_path", lambda: nowhere)


@pytest.fixture(autouse=True)
def _no_local_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    from airlie.config import load

    real, local = load._read_yaml, ROOT / "airlie.local.yaml"

    def guarded(path: Path) -> dict[str, object]:
        if path.resolve() == local.resolve():
            raise AssertionError(
                f"a contract test read {local}: give load_config a local path that does not exist"
            )
        return real(path)

    monkeypatch.setattr(load, "_read_yaml", guarded)


@pytest.fixture(autouse=True)
def _no_real_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from airlie.config import load

    real, shipped = load._read_yaml_with_includes, (ROOT / "airlie.yaml").resolve()

    def own_state(path: Path, *seen: object) -> dict[str, object]:
        data = real(path, *seen)
        if path.resolve() == shipped and isinstance(data.get("observability"), dict):
            data["observability"]["state_dir"] = str(tmp_path / "state")
        return data

    monkeypatch.setattr(load, "_read_yaml_with_includes", own_state)
