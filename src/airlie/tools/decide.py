"""decide: ask the System One decision model (Kev) for a calibrated choice among named options."""

from __future__ import annotations

import json
from typing import Any

import httpx

from airlie.config import AirlieConfig
from airlie.router import JevClient, RouteDecision, RoutingLog, jev_client, policy, ranked
from airlie.tools.protocol import ToolContext, ToolResult
from airlie.tools.registry import ToolRegistry


class Decide:
    name = "decide"
    description = (
        "Make a calibrated choice among named options with a decision model. Use it for closed-set calls "
        "(which option fits, is this blocking, which category) — not for free-text questions."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "short key for the decision, e.g. severity"},
            "state": {"type": "string", "description": "the situation the decision is about"},
            "options": {
                "type": "object",
                "description": "option name -> one-line criterion; at least two",
                "additionalProperties": {"type": "string"},
            },
        },
        "required": ["question", "state", "options"],
    }

    def __init__(
        self,
        client: JevClient,
        *,
        ambiguous_gap: float,
        min_confidence: float,
        log: RoutingLog | None = None,
    ) -> None:
        self.client = client
        self.gap = ambiguous_gap
        self.min_conf = min_confidence
        self.log = log

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        options = args.get("options")
        if not isinstance(options, dict) or len(options) < 2:
            return ToolResult(False, "error: decide needs at least two options ({name: criterion})")
        criteria = {str(k): str(v) for k, v in options.items()}
        try:
            ans = await self.client.ask(
                str(args["state"]), str(args["question"]), "Pick the option that fits best.", criteria
            )
        except (httpx.HTTPError, ValueError) as e:
            return ToolResult(False, f"error: decision engine unavailable: {e}")
        r = [(k, p) for k, p in ranked(ans.probabilities) if k in criteria]
        d: str
        if ans.choice in criteria:
            choice = ans.choice
            d = policy(r, ans.confidence, ambiguous_gap=self.gap, min_confidence=self.min_conf)
        else:  # Kev answered outside the closed set: top in-set option, labelled invalid (as JevRouter does)
            choice = r[0][0] if r else ""
            d = "invalid"
        if self.log is not None:
            question = str(args["question"])
            self.log.record(
                f"decide:{question}",
                RouteDecision(
                    question, ans.choice, choice, d, ans.probabilities, ans.confidence, None, ans.input_tokens
                ),
            )
        out = {
            "choice": choice,
            "decision": d,
            "confidence": ans.confidence,
            "probabilities": ans.probabilities,
        }
        return ToolResult(True, json.dumps(out))


def attach_decide(
    registry: ToolRegistry, cfg: AirlieConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> bool:
    """Add `decide` when a llama_server engine serves router.model; False (and no tool) otherwise."""
    m = cfg.router.model
    # `and` here is an equivalent mutant: the KeyError below also ends in False
    if m is None or m not in cfg.models:  # pragma: no mutate
        return False
    try:
        e = cfg.engines[cfg.engine_for_model(m)]
    except KeyError:
        return False
    if e.kind != "llama_server":
        return False
    registry.add(
        Decide(
            jev_client(cfg, transport=transport),
            ambiguous_gap=cfg.router.ambiguous_gap,
            min_confidence=cfg.router.min_confidence,
        )
    )
    return True
