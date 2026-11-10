"""Shared helpers extracted from tests/unit/test_story_flow_build.py for tests.support.story_flow_build."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from tests.support.story_flow_plan import make_story

FIXTURES = Path(__file__).parent.parent / "fixtures" / "flow"


def from_fixture(root: Path, key: str) -> Path:
    """The trimmed real story `key` under `root` as airlie keeps it: its state and log, and its runs."""
    src = FIXTURES / key
    story = root / "stories" / key
    story.mkdir(parents=True)
    for f in ("state.json", "events.jsonl"):
        shutil.copy(src / f, story / f)
    if (src / "runs").is_dir():
        shutil.copytree(src / "runs", root / "runs", dirs_exist_ok=True)
    return root


def story_files(root: Path, key: str) -> tuple[Path, Path]:
    d = root / "stories" / key
    return d / "state.json", d / "events.jsonl"


def air47_at_16_12(root: Path) -> Path:
    """AIR-47 as it stood at 16:12 on 2026-11-02: L01 and L02 drafted, L03's turn out (its log is from before
    package_draft_start), nothing built yet."""
    from_fixture(root, "AIR-47")
    state, log = story_files(root, "AIR-47")
    lines = log.read_text().splitlines()
    drafted = [i for i, x in enumerate(lines) if json.loads(x)["kind"] == "package_drafted"]
    log.write_text("\n".join(lines[: drafted[1] + 1]) + "\n")
    st = json.loads(state.read_text())
    st.update(drafted=False, drafts={}, built={}, blocked=None)
    state.write_text(json.dumps(st))
    return root


LAYERS = [
    {"slug": f"l{n}", "title": f"Layer {n}", "scope": "s", "acceptance": []} for n in ("01", "02", "03")
]


def layered(root: Path, events: list[dict[str, Any]], **state: Any) -> Path:
    return make_story(root, events, stage="layers", layers=LAYERS, **state)
