"""The kinds of notification a person can switch on or off."""

from __future__ import annotations

from typing import Literal

NotificationKind = Literal[
    "chat_reply", "model_ready", "confirmation", "story_blocked", "story_published", "story_mockups"
]

KINDS: tuple[NotificationKind, ...] = (
    "chat_reply",
    "model_ready",
    "confirmation",
    "story_blocked",
    "story_published",
    "story_mockups",
)
