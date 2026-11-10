"""Shared helpers extracted from tests/unit/test_story_epic.py for tests.support.story_epic."""

from __future__ import annotations

import json
from typing import Any

SPEC = """# Avatars

Why: users want faces.

## Constraints

- PNG only
- 1 MB at most

## Acceptance criteria

- A user can upload an avatar
- The avatar is shown on the profile
  and on comments

## Left open

- the size
"""


def fenced(obj: dict[str, Any]) -> str:
    return "```json\n" + json.dumps(obj) + "\n```\n"
