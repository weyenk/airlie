"""The gate on the repository's own prompts (spec 2026-10-22-prompt-hub-design, "Contradiction check"): no two
texts that reach the same role may give opposite stances on a subject, unless one yields to the other. The
check itself is tested on synthetic texts in `test_prompt_check.py`."""

from __future__ import annotations

from pathlib import Path

from airlie.config import load_config
from airlie.prompts import load
from airlie.prompts.check import conflicts

ROOT = Path(__file__).resolve().parents[2]
NO_LOCAL = ROOT / "no-local-overrides.yaml"  # never the person's own airlie.local.yaml


def test_the_repository_prompts_do_not_contradict() -> None:
    cfg = load_config(ROOT / "airlie.yaml", NO_LOCAL)
    found = conflicts(load().values(), {name: role.prompt for name, role in cfg.roles.items()})
    lines = [f"{c.role}: {c.subject}: {c.a} says {c.a_value!r}, {c.b} says {c.b_value!r}" for c in found]
    assert not lines, "prompt texts contradict each other:\n" + "\n".join(lines)
