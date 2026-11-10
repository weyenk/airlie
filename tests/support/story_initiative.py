"""Shared helpers extracted from tests/unit/test_story_initiative.py for tests.support.story_initiative."""

from __future__ import annotations


def epic_obj(eid: str = "E1", kind: str = "core", **over: object) -> dict[str, object]:
    """One epic as product writes it (D5)."""
    base: dict[str, object] = {
        "id": eid,
        "kind": kind,
        "title": f"Epic {eid}",
        "why": "The board feels slow.",
        "goals": ["feels fast"],
        "non_goals": ["a redesign"],
        "anti_goals": ["losing a card"],
        "decisions": [{"what": "keep the board", "why": "people know it"}],
        "constraints": ["no new dependency"],
        "criteria": [{"id": "EC1", "text": "a card moves in one keystroke"}],
        "repositories": ["o/r"],
        "stories": [{"title": "keys", "size": "S", "scope": "keyboard moves"}],
        "risks": ["focus bugs"],
        "viability": {"verdict": "viable", "reasons": ["the app is small"]},
        "needs_mockups": False,
        "depends_on": [],
        "open": [],
    }
    return {**base, **over}


def epics_obj(*epics: dict[str, object]) -> dict[str, object]:
    return {"type": "epics", "summary": "Make the board fast.", "epics": list(epics or [epic_obj()])}
