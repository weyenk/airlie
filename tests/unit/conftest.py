"""Shared unit-test fixtures."""

import pytest

ATLASSIAN = ("ATLASSIAN_API_KEY", "ATLASSIAN_EMAIL", "ATLASSIAN_BASE_URL", "ATLASSIAN_PROJECT_KEY")


@pytest.fixture(autouse=True)
def _no_atlassian_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The person may export the ATLASSIAN_* variables in their shell, and they hold their real Jira site and
    credentials: a unit test must never see them, or a story test would reach their Jira."""
    for name in ATLASSIAN:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_real_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """airlie reads the .env beside airlie.yaml: no unit test may read the person's real one. Tests of the
    reader pass their own tmp paths, and a test of the lookup imports the real config_dotenv itself."""
    monkeypatch.setattr("airlie.story.jira.config_dotenv", lambda cfg: {})
