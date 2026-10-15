"""Unit-test environment and shared fixtures.

macOS: httpx builds its proxy map from the system configuration (`_scproxy` -> SCDynamicStore) unless a
`*_proxy` variable is set. That CoreFoundation call segfaults in a forked, multi-threaded process, which is
how mutmut runs every mutant, and each crash raises a "Python quit unexpectedly" dialog. A mutant that makes
a test build a real HTTP client (e.g. `transport=None`) was enough to trigger it. Setting NO_PROXY makes
`urllib.request.getproxies()` take the environment path and never touch SystemConfiguration. Unit tests never
use a proxy, so nothing else changes."""

import io
import os
from pathlib import Path

import pytest

os.environ.setdefault("NO_PROXY", "*")

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


@pytest.fixture
def ascii_locale(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Text files opened without an encoding are ASCII, as under LC_ALL=C with Python's UTF-8 mode off.
    pathlib asks io.text_encoding for the default, so that is what this replaces. A test that uses it shows
    that the code under test names its encoding: Python here is built in UTF-8 mode, where forgetting to
    would not fail."""
    monkeypatch.setattr(io, "text_encoding", lambda encoding, stacklevel=2: encoding or "ascii")
    with pytest.raises(UnicodeEncodeError):  # the simulation holds, or a test that uses it proves nothing
        (tmp_path / "probe.txt").write_text("​")
