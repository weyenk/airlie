"""Retired run-system symbols (the hand-written pipeline and its epics/initiatives), kept as inert stubs so
the engine swap (2026-11-19) leaves `airlie.story` and its importers resolving; the run system is rebuilt on
the generic engine in a follow-up plan. Instantiating them raises, so a run request is told plainly it is
not built yet."""

from __future__ import annotations

from typing import Any

from airlie.story.model import StoryError


def _retired() -> None:
    raise StoryError("the story run system is retired (engine swap pending its rebuild)")


def epics_dir(cfg: Any) -> Any:
    _retired()


class StoryPipeline:
    def __init__(self, *a: Any, **k: Any) -> None:
        _retired()


class EpicPipeline:
    def __init__(self, *a: Any, **k: Any) -> None:
        _retired()


class InitiativePipeline:
    def __init__(self, *a: Any, **k: Any) -> None:
        _retired()
