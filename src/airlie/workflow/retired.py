"""Retired story/flow symbols the builder + card-workflow still name, kept as inert stubs so the engine
swap (2026-11-19) leaves the app importing and the gate green. Each is a no-op or a clear error until the
builder and card-workflow are rebuilt on the generic engine (a follow-up plan). The check and bundle kinds
and the check catalog are gone for good; these exist only so the not-yet-rebuilt code resolves."""

from __future__ import annotations

from typing import Any

NodeType = str  # the old node-type tag: replaced by a node's `kind`
TeamRole = str
Format = Any
MEETINGS: tuple[str, ...] = ()
DEPRECATED_NODE_TYPES: tuple[str, ...] = ()
MEMORY: dict[str, Any] = {}


def check_workflows(*a: Any, **k: Any) -> list[str]:
    return []


def old_schema_problems(*a: Any, **k: Any) -> list[str]:
    return []


def transient_heads(*a: Any, **k: Any) -> list[str]:
    return []


def upgrade_v1(data: Any, *a: Any, **k: Any) -> tuple[dict[str, Any], list[str]]:
    return (data, [])


def upgrade_topology_types(data: dict[str, Any]) -> dict[str, Any]:
    return data


class StateStore:
    """The per-card run store: retired with the story engine (the new engine keeps state in memory)."""

    def __init__(self, *a: Any, **k: Any) -> None:
        pass


class Catalog:
    def describe(self) -> list[dict[str, Any]]:
        return []

    def check(self, *a: Any, **k: Any) -> list[str]:
        return []


STORY_CATALOG = Catalog()
