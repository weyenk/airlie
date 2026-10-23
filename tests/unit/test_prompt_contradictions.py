"""The gate on the repository's own prompts (spec 2026-10-22-prompt-hub-design, "Contradiction check"): no two
texts that reach the same role may give opposite stances on a subject, unless one yields to the other. The
check itself is tested on synthetic texts in `test_prompt_check.py`."""

from __future__ import annotations

from pathlib import Path

from airlie.config import AirlieConfig, load_config
from airlie.prompts import load
from airlie.prompts.check import Conflict, conflicts

ROOT = Path(__file__).resolve().parents[2]
NO_LOCAL = ROOT / "no-local-overrides.yaml"  # never the person's own airlie.local.yaml


def _config() -> AirlieConfig:
    return load_config(ROOT / "airlie.yaml", NO_LOCAL)


def _role_prompts(cfg: AirlieConfig) -> dict[str, Path]:
    return {name: role.prompt for name, role in cfg.roles.items()}


def test_the_repository_prompts_do_not_contradict() -> None:
    cfg = _config()
    assert cfg.roles
    found = conflicts(load().values(), _role_prompts(cfg))
    lines = [f"{c.role}: {c.subject}: {c.a} says {c.a_value!r}, {c.b} says {c.b_value!r}" for c in found]
    assert not lines, "prompt texts contradict each other:\n" + "\n".join(lines)


def test_every_hub_text_governs_something() -> None:
    untagged = sorted(t.path for t in load().values() if not t.governs)
    assert not untagged, "these texts govern no subject:\n" + "\n".join(untagged)


def test_every_role_named_by_a_hub_text_exists() -> None:
    known = set(_config().roles) | {"any"}
    unknown = sorted(f"{t.path}: {r}" for t in load().values() for r in t.roles if r not in known)
    assert not unknown, "these texts name a role airlie.yaml does not define:\n" + "\n".join(unknown)


def test_the_tests_location_rules_are_settled(tmp_path: Path) -> None:
    """PR 57's precedence, written down: the coder's prompt says tests go in a new file, and gives way to the
    rule that a test file the layer names wins. Without coder.md's `yields_to`, the check reports that pair,
    and the two other brief rules coder.md gives way to (test-first, and edit_file for an existing file)."""
    cfg = _config()
    roles = _role_prompts(cfg)
    assert conflicts(load().values(), roles) == []

    original = roles["coder"].read_text()
    lines = original.splitlines(keepends=True)
    without = "".join(line for line in lines if not line.startswith("yields_to:"))
    assert without != original, "coder.md has no yields_to line to remove"
    copy = tmp_path / "coder.md"
    copy.write_text(without)

    def against_coder(subject: str, rule: str, value: str, coder_value: str) -> Conflict:
        return Conflict("coder", subject, rule, value, "role:coder", coder_value)  # the rule id sorts first

    assert conflicts(load().values(), {**roles, "coder": copy}) == [
        against_coder("file.rewrite", "brief-layer-plan-edit-file", "never", "short-files"),
        against_coder("tests.location", "brief-layer-plan-tests-where", "named-file", "new-file"),
        against_coder("tests.order", "brief-layer-plan-tests-first", "first", "code-first"),
    ]
