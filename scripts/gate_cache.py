"""Phase 4b gate, Step 4: prefix-cache hits and MTP drafts on one engine while four other requests stream.

    uv run python scripts/gate_cache.py --url http://127.0.0.1:8080/v1 --model generalist        # vllm-mlx
    uv run python scripts/gate_cache.py --engine main                                            # mlx_vlm

A two-turn conversation is sent twice (turn 1, then turn 1 + its reply + turn 2) while four unrelated requests
stream on the same engine. Every request streams with `stream_options.include_usage`; the script prints, per
request, the cached prompt tokens (`usage.prompt_tokens_details.cached_tokens`, also `timings.cache_n`)
and the drafts (`timings.draft_n`/`draft_n_accepted` on mlx-vlm, `generation_metadata.mtp_*` on vllm-mlx).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer

ROOT = Path(__file__).resolve().parents[1]
app = typer.Typer(add_completion=False)

# A long, fixed system prompt so the shared prefix is worth caching (a few thousand tokens).
SYSTEM = "You are a careful senior engineer. Answer briefly.\n\n" + "\n".join(
    f"Rule {i}: when a function named handler_{i} touches the table orders_{i % 7}, it must log the row id, "
    f"check the tenant {i % 13}, and return within {i * 3 % 50 + 10} ms."
    for i in range(160)
)
TURN1 = "Which rule numbers mention tenant 5? List at most five numbers, nothing else."
TURN2 = "Now name the table those rules touch, one word per rule."
BACKGROUND = [
    "Write a 300-word story about a lighthouse keeper.",
    "Explain how TCP congestion control works, in detail.",
    "List 40 prime numbers with one sentence about each.",
    "Describe the water cycle step by step for a ten-year-old.",
]


async def stream(h: httpx.AsyncClient, url: str, body: dict[str, Any]) -> dict[str, Any]:
    body = {**body, "stream": True, "stream_options": {"include_usage": True}}
    text: list[str] = []
    out: dict[str, Any] = {"usage": None, "timings": None, "generation_metadata": None}
    t0 = time.perf_counter()
    async with h.stream("POST", f"{url}/chat/completions", json=body) as r:
        if r.status_code != 200:
            return {"error": f"{r.status_code}: {(await r.aread())[:300]!r}"}
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            f = json.loads(data)
            for k in ("usage", "timings", "generation_metadata"):
                if f.get(k):
                    out[k] = f[k]
            for c in f.get("choices") or []:
                if c.get("delta", {}).get("content"):
                    text.append(c["delta"]["content"])
    out["text"] = "".join(text)
    out["seconds"] = round(time.perf_counter() - t0, 2)
    return out


def summary(tag: str, r: dict[str, Any]) -> dict[str, Any]:
    if "error" in r:
        return {"req": tag, "error": r["error"]}
    u = r.get("usage") or {}
    t = r.get("timings") or {}
    g = r.get("generation_metadata") or {}
    return {
        "req": tag,
        "prompt_tokens": u.get("prompt_tokens"),
        "cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
        "cache_n": t.get("cache_n"),
        "draft_n": t.get("draft_n"),
        "draft_n_accepted": t.get("draft_n_accepted"),
        "mtp_drafts": g.get("mtp_drafts"),
        "mtp_accepted": g.get("mtp_accepted"),
        "completion_tokens": u.get("completion_tokens"),
        "seconds": r.get("seconds"),
        "text": r.get("text", "")[:80],
    }


@app.command()
def main(
    engine: Annotated[str | None, typer.Option(help="an engine of this worktree's airlie.yaml")] = None,
    url: Annotated[str | None, typer.Option()] = None,
    model: Annotated[str | None, typer.Option()] = None,
    out: Annotated[Path | None, typer.Option()] = None,
    rounds: int = 2,
) -> None:
    key = None
    if engine is not None:
        from airlie.config import load_config

        e = load_config(ROOT / "airlie.yaml").engines[engine]
        url, model, key = e.base_url, e.served_name(e.models[0]), e.api_key
    assert url and model
    extra = {"chat_template_kwargs": {"enable_thinking": False}, "temperature": 0}
    if engine is not None:
        extra = {"enable_thinking": False, "temperature": 0}

    async def run() -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        rows: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=600, headers=headers) as h:
            for rnd in range(1, rounds + 1):
                bg = [
                    asyncio.create_task(
                        stream(
                            h,
                            url,
                            {
                                "model": model,
                                "messages": [{"role": "user", "content": p}],
                                "max_tokens": 400,
                                **extra,
                            },
                        )
                    )
                    for p in BACKGROUND
                ]
                await asyncio.sleep(0.5)
                msgs: list[dict[str, Any]] = [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": TURN1},
                ]
                r1 = await stream(h, url, {"model": model, "messages": msgs, "max_tokens": 64, **extra})
                rows.append(summary(f"round{rnd}-turn1", r1))
                msgs += [
                    {"role": "assistant", "content": r1.get("text", "")},
                    {"role": "user", "content": TURN2},
                ]
                r2 = await stream(h, url, {"model": model, "messages": msgs, "max_tokens": 64, **extra})
                rows.append(summary(f"round{rnd}-turn2", r2))
                for i, t in enumerate(bg):
                    rows.append(summary(f"round{rnd}-bg{i}", await t))
        return rows

    rows = asyncio.run(run())
    for r in rows:
        typer.echo(json.dumps(r))
    if out is not None:
        out.write_text(json.dumps({"url": url, "model": model, "rows": rows}, indent=1) + "\n")


if __name__ == "__main__":
    app()
