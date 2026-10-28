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
from typing import Any

import pytest

os.environ.setdefault("NO_PROXY", "*")

ATLASSIAN = ("ATLASSIAN_API_KEY", "ATLASSIAN_EMAIL", "ATLASSIAN_BASE_URL", "ATLASSIAN_PROJECT_KEY")
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_atlassian_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The person may export the ATLASSIAN_* variables in their shell, and they hold their real Jira site and
    credentials: a unit test must never see them, or a story test would reach their Jira."""
    for name in ATLASSIAN:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_real_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """airlie reads the .env beside airlie.yaml and ~/.config/airlie/.env: no unit test may read the person's
    real ones. Tests of the reader pass their own tmp paths, and a test of the lookup imports the real
    config_dotenv (and, with a HOME of its own, the real user_dotenv_path) itself."""
    monkeypatch.setattr("airlie.story.jira.config_dotenv", lambda cfg: {})
    nowhere = tmp_path / "no-home" / ".config" / "airlie" / ".env"
    monkeypatch.setattr("airlie.story.jira.user_dotenv_path", lambda: nowhere)


@pytest.fixture(autouse=True)
def _no_local_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """This checkout's airlie.local.yaml holds the person's own settings (their repository, their Jira): no
    unit test may read it. A test of the committed airlie.yaml gives load_config a local path that does not
    exist."""
    from airlie.config import load

    real, local = load._read_yaml, ROOT / "airlie.local.yaml"

    def guarded(path: Path) -> dict[str, object]:
        if path.resolve() == local.resolve():
            raise AssertionError(
                f"a unit test read {local}: give load_config a local path that does not exist"
            )
        return real(path)

    monkeypatch.setattr(load, "_read_yaml", guarded)


@pytest.fixture(autouse=True)
def _no_real_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The committed airlie.yaml keeps state in ~/.local/state/airlie, where the person's own auth.sqlite and
    board.sqlite live: a unit test that loads it writes a state directory of its own instead (a test's own
    config keeps the state_dir it names). A story's gate runs these tests under the
    write sandbox (airlie.tools.confine), which refuses that directory, so a test that touched it failed
    there and passed everywhere else."""
    from airlie.config import load

    real, shipped = load._read_yaml_with_includes, (ROOT / "airlie.yaml").resolve()

    def own_state(path: Path, *seen: Any) -> dict[str, Any]:  # load recurses into includes with _seen
        data = real(path, *seen)
        if path.resolve() == shipped and isinstance(data.get("observability"), dict):
            data["observability"]["state_dir"] = str(tmp_path / "state")
        return data

    monkeypatch.setattr(load, "_read_yaml_with_includes", own_state)


@pytest.fixture
def local_remotes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Publishing as you also reaches a remote on this machine: for a test that publishes to (or fetches from)
    a local bare repository. airlie itself allows SSH and HTTPS remotes only (publish.PERSON_TRANSPORTS)."""
    monkeypatch.setattr("airlie.story.publish.PERSON_TRANSPORTS", "file:ssh:https")


@pytest.fixture
def ascii_locale(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Text files opened without an encoding are ASCII, as under LC_ALL=C with Python's UTF-8 mode off.
    pathlib asks io.text_encoding for the default, so that is what this replaces. A test that uses it shows
    that the code under test names its encoding: Python here is built in UTF-8 mode, where forgetting to
    would not fail."""
    monkeypatch.setattr(io, "text_encoding", lambda encoding, stacklevel=2: encoding or "ascii")
    with pytest.raises(UnicodeEncodeError):  # the simulation holds, or a test that uses it proves nothing
        (tmp_path / "probe.txt").write_text("​")
