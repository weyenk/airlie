"""Agent states, exactly as named in docs/wiki/build-spec/04 §2."""

from __future__ import annotations

from enum import StrEnum


class AgentState(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED_TOOL_STALL = "SUSPENDED_TOOL_STALL"
