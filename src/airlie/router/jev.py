"""JevClient: POST /v1/systemone on a llama-server serving Kev (System One decision model); JevRouter."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from airlie.router.policy import policy, ranked
from airlie.router.protocol import ROLE_QUESTION, RouteDecision


@dataclass(frozen=True)
class Answer:
    choice: str
    probabilities: dict[str, float]
    confidence: float
    input_tokens: int


class JevClient:
    def __init__(
        self,
        base_url: str,
        *,
        model: str = "jev",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 60.0,
        api_key: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(transport=transport, timeout=timeout_s, headers=headers)

    async def ask(self, state: str, question: str, instructions: str, criteria: dict[str, str]) -> Answer:
        body = {
            "model": self.model,
            "state": state,
            "questions": {question: {"type": "choice", "instructions": instructions, "criteria": criteria}},
        }
        r = await self._client.post(f"{self.base_url}/systemone", json=body)
        if r.status_code != 200:
            raise httpx.HTTPStatusError(
                f"systemone returned {r.status_code}: {r.text[:200]}", request=r.request, response=r
            )
        data = r.json()
        ans = (data.get("answers") or {}).get(question)
        if not isinstance(ans, dict) or "choice" not in ans:
            raise ValueError(f"systemone answered without '{question}': {str(data)[:200]}")
        probs = {str(k): float(v) for k, v in (ans.get("probabilities") or {}).items()}
        usage = data.get("usage") or {}
        return Answer(
            str(ans["choice"]), probs, float(ans.get("confidence", 0.0)), int(usage.get("input_tokens", 0))
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class JevRouter:
    def __init__(self, client: JevClient, *, ambiguous_gap: float, min_confidence: float) -> None:
        self.client = client
        self.gap = ambiguous_gap
        self.min_conf = min_confidence

    async def route(
        self,
        brief: str,
        criteria: dict[str, str],
        hint: str | None = None,
        *,
        question: str = "role",
        instructions: str = ROLE_QUESTION,
    ) -> RouteDecision:
        try:
            ans = await self.client.ask(brief, question, instructions, criteria)
        except (httpx.HTTPError, ValueError) as e:
            if hint is None:
                raise ValueError(f"routing failed and no hint to fall back on: {e}") from e
            return RouteDecision(question, None, hint, "error", {}, 0.0, hint, detail=str(e))
        r = [(k, p) for k, p in ranked(ans.probabilities) if k in criteria]
        top = r[0][0] if r else None
        if ans.choice not in criteria:
            chosen = hint if hint in criteria else top
            if chosen is None:
                raise ValueError(f"systemone chose {ans.choice!r}, outside the closed set, and no fallback")
            return RouteDecision(
                question,
                ans.choice,
                chosen,
                "invalid",
                ans.probabilities,
                ans.confidence,
                hint,
                ans.input_tokens,
            )
        decision = policy(r, ans.confidence, ambiguous_gap=self.gap, min_confidence=self.min_conf)
        if decision == "clear":
            chosen = ans.choice
        elif decision == "ambiguous":
            top_two = {k for k, _ in r[:2]}
            chosen = hint if hint in top_two else ans.choice
        else:
            chosen = hint if hint in criteria else ans.choice
        return RouteDecision(
            question, ans.choice, chosen, decision, ans.probabilities, ans.confidence, hint, ans.input_tokens
        )
