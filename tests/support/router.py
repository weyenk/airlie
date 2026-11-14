"""Shared helpers extracted from tests/unit/test_router.py for tests.support.router."""

from __future__ import annotations

import json

import httpx


def decisions(answers: dict[str, float], confidence: float, choice: str | None = None):  # type: ignore[no-untyped-def]
    """A MockTransport handler answering /v1/decisions with the given probabilities."""
    seen: list[dict] = []  # type: ignore[type-arg]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/decisions"
        body = json.loads(request.content)
        seen.append(body)
        q = next(iter(body["questions"]))
        top = choice or max(answers, key=lambda k: answers[k])
        return httpx.Response(
            200,
            json={
                "answers": {
                    q: {
                        "type": "choice",
                        "value": top,
                        "probabilities": answers,
                        "metadata": {"confidence": confidence},
                    }
                },
                "usage": {"input_tokens": 42},
            },
        )

    return handler, seen


def _decision_cfg(tmp_path, *, decision: bool = True):  # type: ignore[no-untyped-def]
    import yaml

    from airlie.config import load_config

    (tmp_path / "p.md").write_text("x")
    data = {
        "version": 1,
        "engines": {
            "main": {"kind": "mlx_vlm", "port": 8080, "models": ["team"]},
            "dec": {"kind": "mlx_vlm", "port": 8084, "model": "clef", "decision": decision},
        },
        "models": {
            "team": {"repo": "org/t", "revision": "r"},
            "clef": {"repo": "org/c", "revision": "r", "path": "/models/clef"},
        },
        "roles": {
            r: {"prompt": "p.md", "model": "team"} for r in ("architect", "summarizer", "judge", "aggregator")
        },
        "router": {"kind": "jev" if decision else "chief", "model": "clef"},
    }
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")
