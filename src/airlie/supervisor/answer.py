"""Muse's triage answer, parsed and checked before anything acts on it. Muse is a model, so its reply is
data: prose with no JSON, a made-up owner or an empty message is refused (`AnswerError`) and the caller falls
back to the role's text as it stands today, rather than passing on advice nobody can read.

The categories are a seed, not a closed set: Muse may name a new one (snake_case, so the ledger can group on
it), but not an "other_*" catch-all, which would hide exactly the failures worth counting."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from airlie.topology.studio_plan import extract_json_block

CATEGORIES: tuple[str, ...] = (
    "missing_import",
    "sandbox_escape",
    "design_relitigation",
    "empty_reply",
    "repeat_loop",
    "wrong_acceptance_test",
    "not_this_roles_test",
    "environment",
)
OWNERS: tuple[str, ...] = ("tester", "coder", "existing_code", "environment", "unknown")
_ROUTES = ("tester", "coder")
_CATEGORY = re.compile(r"^[a-z][a-z0-9_]{2,39}$")


class AnswerError(ValueError):
    """Muse's reply is not a usable answer; the message says which rule it broke."""


@dataclass(frozen=True)
class Answer:
    category: str
    owner: str
    diagnosis: str
    evidence: tuple[str, ...]
    message: str | None
    would_route_to: str | None
    fix_if_attempt: str | None
    class_agrees: bool | None = None
    """At a block only (P5a, 2026-10-25): whether Muse agrees with the mechanical class the diagnosis read
    first; None when it was not asked (any other trigger) or did not say."""
    class_reason: str | None = None
    """Why it agrees or not, in a sentence."""


def _text(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise AnswerError(f"{key} must be a string")
    return value.strip() or None


def parse_answer(text: str, *, block: bool) -> Answer:
    """The answer in Muse's reply. At a block the layer is stopping, so there is no next step for a role and
    `message` may be empty; anywhere else a role is about to read it, so it must say something."""
    data = extract_json_block(text)
    if data is None:
        raise AnswerError("no JSON object in the reply")
    category = _text(data, "category")
    if category is None or not _CATEGORY.match(category) or category.startswith("other"):
        raise AnswerError(f"category {category!r} is not snake_case, or is an 'other' catch-all")
    owner = _text(data, "owner")
    if owner not in OWNERS:
        raise AnswerError(f"owner {owner!r} is not one of {', '.join(OWNERS)}")
    diagnosis = _text(data, "diagnosis")
    if diagnosis is None:
        raise AnswerError("diagnosis is empty")
    message = _text(data, "message")
    if message is None and not block:
        raise AnswerError("message is empty")
    route = _text(data, "would_route_to")
    if route is not None and route not in _ROUTES:
        raise AnswerError(f"would_route_to {route!r} is not tester or coder")
    raw = data.get("evidence")
    if raw is None:
        raw = []
    if not isinstance(raw, list) or not all(isinstance(e, str) for e in raw):
        raise AnswerError("evidence must be a list of strings")
    agrees = data.get("class_agrees")
    if agrees is not None and not isinstance(agrees, bool):
        raise AnswerError("class_agrees must be true or false")
    return Answer(
        category=category,
        owner=owner,
        diagnosis=diagnosis,
        evidence=tuple(e.strip() for e in raw),
        message=message,
        would_route_to=route,
        fix_if_attempt=_text(data, "fix_if_attempt"),
        class_agrees=agrees,
        class_reason=_text(data, "class_reason"),
    )
