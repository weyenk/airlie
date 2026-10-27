"""The supervisor's configuration: off in the schema, on in the shipped airlie.yaml, and an enabled one must
name a real role (a typo there would otherwise surface mid-run, at the first stuck layer)."""

from pathlib import Path

import pytest

from airlie.config import ConfigError, load_config
from tests.unit.test_config import MINIMAL, write

REPO = Path(__file__).resolve().parents[2]


def test_the_shipped_config_turns_the_supervisor_on_with_muse_and_read_only_tools() -> None:
    cfg = load_config(REPO / "airlie.yaml", REPO / "no-local-overrides.yaml")
    assert cfg.story.supervisor.enabled and cfg.story.supervisor.per_layer == 6
    role = cfg.roles["supervisor"]
    assert role.model == "reviewer" and role.tools == ["read_file", "list_dir", "grep"]
    assert role.prompt.read_text().strip()


def test_an_enabled_supervisor_needs_its_role(tmp_path: Path) -> None:
    data = {**MINIMAL, "story": {"supervisor": {"enabled": True, "role": "nobody"}}}
    with pytest.raises(ConfigError, match="story.supervisor.role 'nobody' is not a role"):
        load_config(write(tmp_path, data))


def test_off_by_default_in_the_schema(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, MINIMAL))
    assert not cfg.story.supervisor.enabled
    assert cfg.story.supervisor.role == "supervisor"
