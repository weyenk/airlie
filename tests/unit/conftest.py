"""Shared unit-test fixtures."""

import pytest

ATLASSIAN = ("ATLASSIAN_API_KEY", "ATLASSIAN_EMAIL", "ATLASSIAN_BASE_URL", "ATLASSIAN_PROJECT_KEY")


@pytest.fixture(autouse=True)
def _no_atlassian_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`task` loads .env, which holds the person's real Jira site and credentials: a unit test must never
    see them, or a story test would reach their Jira."""
    for name in ATLASSIAN:
        monkeypatch.delenv(name, raising=False)
