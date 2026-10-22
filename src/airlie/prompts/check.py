"""The contradiction check (docs/superpowers/specs/2026-10-22-prompt-hub-design.md, "Contradiction check"):
two texts that reach the same role must not give opposite instructions on one subject.

Each hub file and each role prompt names, in `governs`, the subjects it instructs on and its stance on each
(`tests.location: new-file`); `yields_to` lists the sources it deliberately gives way to. A source is a hub
id, or `role:<name>` for a role prompt. A text reaches role R when its `roles` holds R or `any`; a role prompt
reaches only its own role. Within one role, rules (role prompts included) are compared with rules and steering
with steering, never steering with a rule: steering exists to override a standing rule ("reading is switched
off" against a prompt that says to read first). Two sources conflict on a subject when their stances differ
and neither yields to the other. The check is deterministic text-matching on the hand-written tags and reads
no config: `tests/unit/test_prompt_contradictions.py` runs it on the repository in the gate.

Role prompts keep the flat `skills.library.split_frontmatter` (one `key: value` per line, `summary:`
unchanged), so their tags are flat too: a `governs.<subject>: <value>` line per subject and `yields_to: a, b`.

Subjects are free-form dotted names with no registry; `subjects()` lists each in use with its sources, so a
near-duplicate (`tests.location` against `test.location`) shows in review.

Not exported from `airlie.prompts`: this module imports `airlie.skills.library`, which imports
`airlie.config`, and a config module that reads the hub must not meet a cycle on the way in."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from airlie.prompts.hub import Kind, PromptError, PromptText
from airlie.skills.library import split_frontmatter

_ANY = "any"
_GOVERNS = "governs."
_KINDS: tuple[Kind, ...] = ("rule", "steering")


@dataclass(frozen=True)
class Conflict:
    """Two sources that give different stances on `subject` for `role`. `a` sorts before `b`."""

    role: str
    subject: str
    a: str
    a_value: str
    b: str
    b_value: str


@dataclass(frozen=True)
class _Source:
    """What the check needs of a hub text or a role prompt. Role prompts are rules for their own role."""

    name: str
    kind: Kind
    roles: tuple[str, ...]
    governs: Mapping[str, str]
    yields_to: tuple[str, ...]


def reaches(roles: Iterable[str], role: str) -> bool:
    """Whether a text whose `roles` is this reaches `role`: it names the role, or `any`. A role prompt names
    only its own role. The audit (`audit.py`) builds each role's instruction set with the same test."""
    return role in roles or _ANY in roles


def role_tags(prompt: Path) -> tuple[dict[str, str], tuple[str, ...]]:
    """The `governs` subjects (subject -> value) and the `yields_to` sources of a role prompt, from its flat
    front matter. A prompt without tags gives `({}, ())`. A bare `governs:` line is an error, not an ignored
    key: a tag the check silently skipped would hide the contradiction it was written to catch."""
    meta, _ = split_frontmatter(prompt.read_text())
    governs: dict[str, str] = {}
    for key, value in meta.items():
        if key == "governs" or key == _GOVERNS:
            raise PromptError(f"{prompt}: write one governs.<subject>: <value> line per subject, got {key!r}")
        if key.startswith(_GOVERNS):
            governs[key.removeprefix(_GOVERNS)] = value
    yields_to = tuple(part.strip() for part in meta.get("yields_to", "").split(",") if part.strip())
    return governs, yields_to


def _sources(texts: Iterable[PromptText], role_prompts: Mapping[str, Path]) -> list[_Source]:
    found = [_Source(t.id, t.kind, t.roles, t.governs, t.yields_to) for t in texts]
    for role, path in role_prompts.items():
        governs, yields_to = role_tags(path)
        found.append(_Source(f"role:{role}", "rule", (role,), governs, yields_to))
    return found


def conflicts(texts: Iterable[PromptText], role_prompts: Mapping[str, Path]) -> list[Conflict]:
    """Every conflicting pair, once per role, sorted by (role, subject, a, b). `role_prompts` maps a role name
    to its prompt file. The roles are those a role prompt or a text's `roles` names; `any` reaches all."""
    sources = sorted(_sources(texts, role_prompts), key=lambda s: s.name)
    found: list[Conflict] = []
    for role in sorted({r for s in sources for r in s.roles} - {_ANY}):
        reaching = [s for s in sources if reaches(s.roles, role)]
        for kind in _KINDS:
            for a, b in combinations([s for s in reaching if s.kind == kind], 2):
                if a.name in b.yields_to or b.name in a.yields_to:
                    continue
                for subject in sorted(a.governs.keys() & b.governs.keys()):
                    if a.governs[subject] != b.governs[subject]:
                        found.append(
                            Conflict(role, subject, a.name, a.governs[subject], b.name, b.governs[subject])
                        )
    return sorted(found, key=lambda c: (c.role, c.subject, c.a, c.b))


def subjects(texts: Iterable[PromptText], role_prompts: Mapping[str, Path]) -> dict[str, list[str]]:
    """Each `governs` subject in use, sorted, with the sorted sources that give a stance on it."""
    by_subject: dict[str, list[str]] = {}
    for source in _sources(texts, role_prompts):
        for subject in source.governs:
            by_subject.setdefault(subject, []).append(source.name)
    return {subject: sorted(by_subject[subject]) for subject in sorted(by_subject)}
