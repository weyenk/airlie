"""Shared helpers extracted from tests/unit/test_cli_story.py for tests.support.cli_story."""

from __future__ import annotations

from pathlib import Path

from tests.support.story import STORY_ROLES
from tests.support.studio import cfg_for


def story_cfg(tmp_path: Path, **story: object) -> Path:
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": story})
    assert cfg.source_path is not None
    return cfg.source_path
