"""Integration-test environment: the same guards the unit tests have (see tests/unit/conftest.py), so no test
here reaches the person's Jira, reads their .env or their airlie.local.yaml, writes their real state
directory, builds an HTTP client that asks macOS for its proxies, is coloured by their shell, or lets a
story beat ask GitHub for a nightly mutation run. An integration test wires several units over real files
and subprocesses; its call phase may not run longer than 60 seconds.

test_trainer.py and test_cli_controller.py import torch at the top (marked `mac`): the Linux test image
(ops/test/Containerfile) leaves torch out, so they are not collected there. On a Mac with mlx they run."""

import importlib.util
import os
from pathlib import Path

import pytest

os.environ.setdefault("NO_PROXY", "*")

ATLASSIAN = ("ATLASSIAN_API_KEY", "ATLASSIAN_EMAIL", "ATLASSIAN_BASE_URL", "ATLASSIAN_PROJECT_KEY")
ROOT = Path(__file__).resolve().parents[2]

MAC_ONLY = ("test_trainer.py", "test_cli_controller.py")
"""The integration modules that import torch/mlx (macOS only): where mlx is missing these are not collected,
and each is marked `mac` so `task test:mac` runs it on a Mac (see tests/unit/conftest.py MAC_ONLY)."""
HAS_MLX = not os.environ.get("AIRLIE_TEST_WITHOUT_MLX") and importlib.util.find_spec("mlx") is not None
collect_ignore = [] if HAS_MLX else list(MAC_ONLY)

TIMEOUT_S = 60
"""The seconds an integration test's call phase may run before pytest-timeout fails it (pyproject.toml lists
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
                f"an integration test read {local}: give load_config a local path that does not exist"
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


@pytest.fixture(autouse=True)
def _no_mutation_harvest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every beat's bookkeeping asks GitHub for the newest nightly mutation run (airlie.story.mutation_cards):
    no integration test may reach GitHub, or put its answer in a beat's line."""

    async def nothing(*args: object, **kwargs: object) -> list[str]:
        return []

    monkeypatch.setattr("airlie.story.mutation_cards.harvest", nothing)


@pytest.fixture
def local_remotes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Publishing as you also reaches a remote on this machine: for a test that publishes to (or fetches from)
    a local bare repository. airlie itself allows SSH and HTTPS remotes only (publish.PERSON_TRANSPORTS)."""
    monkeypatch.setattr("airlie.story.publish.PERSON_TRANSPORTS", "file:ssh:https")


@pytest.fixture
def ascii_locale(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Text files opened without an encoding are ASCII, as under LC_ALL=C with Python's UTF-8 mode off.
    pathlib asks io.text_encoding for the default, so that is what this replaces."""
    import io

    monkeypatch.setattr(io, "text_encoding", lambda encoding, stacklevel=2: encoding or "ascii")
    with pytest.raises(UnicodeEncodeError):
        (tmp_path / "probe.txt").write_text("​")
