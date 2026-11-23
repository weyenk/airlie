"""Retired run-system symbols (the hand-written pipeline and its epics/initiatives), kept as inert stubs so
the engine swap (2026-11-19) leaves `airlie.story` and its importers resolving; the run system is rebuilt on
the generic engine in a follow-up plan. Instantiating them raises, so a run request is told plainly it is
not built yet."""

from __future__ import annotations

from typing import Any, NoReturn

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


# layer_build still names these helpers from the deleted pipeline; they raise if a legacy path runs.
def _cut_off(*_a: Any, **_k: Any) -> NoReturn:
    raise StoryError("the story run system is retired (engine swap pending its rebuild)")


def _ending(*_a: Any, **_k: Any) -> NoReturn:
    raise StoryError("the story run system is retired (engine swap pending its rebuild)")


def _gate_detail(*_a: Any, **_k: Any) -> NoReturn:
    raise StoryError("the story run system is retired (engine swap pending its rebuild)")


def _kept(*_a: Any, **_k: Any) -> NoReturn:
    raise StoryError("the story run system is retired (engine swap pending its rebuild)")


def _redact_urls(*_a: Any, **_k: Any) -> NoReturn:
    raise StoryError("the story run system is retired (engine swap pending its rebuild)")


def _rows(*_a: Any, **_k: Any) -> NoReturn:
    raise StoryError("the story run system is retired (engine swap pending its rebuild)")
