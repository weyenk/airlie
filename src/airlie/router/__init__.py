"""Router package: Kev (System One) client + ai-mode's policy, chief fallback, routing log."""

from __future__ import annotations

import httpx

from airlie.config import AirlieConfig
from airlie.router.chief import ChiefRouter
from airlie.router.jev import Answer, JevClient, JevRouter
from airlie.router.log import RoutingLog
from airlie.router.policy import policy, ranked
from airlie.router.protocol import ROLE_QUESTION, RouteDecision, Router


def jev_client(cfg: AirlieConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> JevClient:
    """The client for the llama_server engine that serves router.model."""
    if cfg.router.model is None:
        raise ValueError("router.model is not set")
    e = cfg.engines[cfg.engine_for_model(cfg.router.model)]
    return JevClient(
        e.base_url or "", model=e.served_name(cfg.router.model), transport=transport, api_key=e.api_key
    )


def build_router(cfg: AirlieConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> Router:
    if cfg.router.kind == "jev":
        return JevRouter(
            jev_client(cfg, transport=transport),
            ambiguous_gap=cfg.router.ambiguous_gap,
            min_confidence=cfg.router.min_confidence,
        )
    return ChiefRouter()


__all__ = [
    "ROLE_QUESTION",
    "Answer",
    "ChiefRouter",
    "JevClient",
    "JevRouter",
    "RouteDecision",
    "Router",
    "RoutingLog",
    "build_router",
    "jev_client",
    "policy",
    "ranked",
]
