"""Shared helpers extracted from tests/unit/test_workflows_api.py for tests.support.workflows_api."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from airlie.board import BoardStore
from tests.support.story import WORKFLOWS
from tests.support.studio import cfg_for


def cfg(tmp_path: Path):  # type: ignore[no-untyped-def]
    """The shipped story and layer workflows, as airlie.yaml includes them."""
    workflows = {
        name: spec
        for part in ("story", "layer")
        for name, spec in yaml.safe_load((WORKFLOWS / f"{part}.yaml").read_text())["workflows"].items()
    }
    return cfg_for(tmp_path, extra={"workflows": workflows})


def store(c: Any) -> BoardStore:
    return BoardStore(c.observability.state_dir.expanduser() / "board.sqlite", prefix=c.board.prefix)
