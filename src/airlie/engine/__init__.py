"""Engine seam: protocol, types, and implementations (FakeEngine for tests, HTTPEngine for real servers)."""

from airlie.engine.fake import FakeEngine, FakeReply
from airlie.engine.protocol import Engine, OnToken
from airlie.engine.types import (
    EngineCapabilities,
    EngineError,
    EngineHealth,
    EngineStats,
    FinishReason,
    GenRequest,
    GenResult,
    Message,
    ModelState,
    TokenEvent,
    ToolCall,
    ToolSchema,
)

__all__ = [
    "Engine",
    "EngineCapabilities",
    "EngineError",
    "EngineHealth",
    "EngineStats",
    "FakeEngine",
    "FakeReply",
    "FinishReason",
    "GenRequest",
    "GenResult",
    "Message",
    "ModelState",
    "OnToken",
    "TokenEvent",
    "ToolCall",
    "ToolSchema",
]
