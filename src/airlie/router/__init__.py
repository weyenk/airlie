"""Router package: ai-mode's policy, the chief router, the routing log."""

from __future__ import annotations

import httpx

from airlie.config import AirlieConfig
from airlie.router.chief import ChiefRouter
from airlie.router.log import RoutingLog
from airlie.router.policy import policy, ranked
from airlie.router.protocol import ROLE_QUESTION, RouteDecision, Router


def build_router(cfg: AirlieConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> Router:
    """The chief router: the plan's own `role` field is the decision (airlie runs no decision engine)."""
    return ChiefRouter()


__all__ = [
    "ROLE_QUESTION",
    "ChiefRouter",
    "RouteDecision",
    "Router",
    "RoutingLog",
    "build_router",
    "policy",
    "ranked",
]
