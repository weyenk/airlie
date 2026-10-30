"""The gate on the repository's own prompts (spec 2026-10-22-prompt-hub-design, "Contradiction check"): no two
texts that reach the same role may give opposite stances on a subject, unless one yields to the other or the
conflict is in ACCEPTED below. The check itself is tested on synthetic texts in `test_prompt_check.py`.

`yields_to` records runtime precedence only: a stage's brief over the role prompt's general default, a later
nudge over an earlier one. A real conflict that is accepted as it stands goes in ACCEPTED, one entry per role
and subject, so it covers that subject alone; a wording that settles it is Noah's call."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from airlie.config import AirlieConfig, load_config
from airlie.prompts import load
from airlie.prompts.check import Conflict, conflicts, idle_yields

ROOT = Path(__file__).resolve().parents[2]
NO_LOCAL = ROOT / "no-local-overrides.yaml"  # never the person's own airlie.local.yaml


class Accepted(NamedTuple):
    """A known conflict the gate lets stand: the role, the subject, both sources, and why."""

    role: str
    subject: str
    a: str
    b: str
    why: str


GOAL_CLOSE = (
    "goal-tools ends every tooled team role's goal with 'change it, run the tests, stop with a short summary "
    "when they pass'; a neutral close would settle it"
)
ACCEPTED = (
    Accepted(
        "coder",
        "file.rewrite",
        "brief-layer-plan-edit-file",
        "role:coder",
        "coder.md allows write_file on a short existing file, the build brief never; mild",
    ),
)


def _key(role: str, subject: str, a: str, b: str) -> tuple[str, str, frozenset[str]]:
    return role, subject, frozenset((a, b))


def _accepted_keys() -> set[tuple[str, str, frozenset[str]]]:
    return {_key(e.role, e.subject, e.a, e.b) for e in ACCEPTED}


def _config() -> AirlieConfig:
    return load_config(ROOT / "airlie.yaml", NO_LOCAL)


def _role_prompts(cfg: AirlieConfig) -> dict[str, Path]:
    return {name: role.prompt for name, role in cfg.roles.items()}


def _live() -> list[Conflict]:
    return conflicts(load().values(), _role_prompts(_config()))


def test_the_repository_prompts_do_not_contradict() -> None:
    assert _config().roles
    accepted = _accepted_keys()
    found = [c for c in _live() if _key(c.role, c.subject, c.a, c.b) not in accepted]
    lines = [f"{c.role}: {c.subject}: {c.a} says {c.a_value!r}, {c.b} says {c.b_value!r}" for c in found]
    assert not lines, "prompt texts contradict each other:\n" + "\n".join(lines)


def test_every_accepted_conflict_is_still_live() -> None:
    """An entry that no longer matches a conflict (a tag or a wording changed, a role was narrowed) goes."""
    live = {_key(c.role, c.subject, c.a, c.b) for c in _live()}
    stale = [
        f"{e.role}: {e.subject}: {e.a} / {e.b}"
        for e in ACCEPTED
        if _key(e.role, e.subject, e.a, e.b) not in live
    ]
    assert not stale, "these accepted conflicts no longer occur; remove them from ACCEPTED:\n" + "\n".join(
        stale
    )


def test_each_accepted_conflict_is_listed_once_with_a_reason() -> None:
    assert len(_accepted_keys()) == len(ACCEPTED)
    assert all(e.why.strip() for e in ACCEPTED)


def test_every_yields_to_decides_a_live_disagreement() -> None:
    idle = idle_yields(load().values(), _role_prompts(_config()))
    lines = [f"{source} yields to {other}" for source, other in idle]
    assert not idle, "these yields_to entries settle nothing; remove them:\n" + "\n".join(lines)


def test_every_hub_text_governs_something() -> None:
    untagged = sorted(t.path for t in load().values() if not t.governs)
    assert not untagged, "these texts govern no subject:\n" + "\n".join(untagged)


def test_every_role_named_by_a_hub_text_exists() -> None:
    known = set(_config().roles) | {"any"}
    unknown = sorted(f"{t.path}: {r}" for t in load().values() for r in t.roles if r not in known)
    assert not unknown, "these texts name a role airlie.yaml does not define:\n" + "\n".join(unknown)
