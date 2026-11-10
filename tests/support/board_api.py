"""Shared helpers extracted from tests/unit/test_board_api.py for tests.support.board_api."""

from __future__ import annotations

from tests.support.story_workflows import MINI

NO_RUN = MINI.replace("intake: {type: check, run: story.intake,", "intake: {type: check,")
