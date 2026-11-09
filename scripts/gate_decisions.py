"""Phase 4b gate, Step 6: airlie's role question over twenty recorded briefs through one decision engine.

    uv run python scripts/gate_decisions.py ask decision --out kev.json          # Kev (/v1/systemone)
    uv run python scripts/gate_decisions.py ask decision_mlx --out mlx.json      # mlx_vlm (/v1/decisions)
    uv run python scripts/gate_decisions.py compare kev.json mlx.json

The briefs are the labelled routing set (evals/routing.jsonl, the cases behind docs/bench/routing.json),
twenty of them taken round-robin across the expected roles so every role is asked about. The criteria are the
config's routable roles minus the chief and the reviewer, as `airlie route eval` uses. Each engine is asked
through `JevClient` with the exact served model string, and the first case's raw reply is kept in the output.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Annotated

import httpx
import typer

from airlie.bench.route_eval import load_cases
from airlie.config import load_config
from airlie.router import ROLE_QUESTION, JevClient
from airlie.skills import routable_roles

ROOT = Path(__file__).resolve().parents[1]
app = typer.Typer(add_completion=False)


def pick(n: int) -> list[tuple[str, str]]:
    by_role: dict[str, list[str]] = defaultdict(list)
    for c in load_cases(ROOT / "evals" / "routing.jsonl"):
        if c.question == "role":
            by_role[c.expect].append(c.task)
    out: list[tuple[str, str]] = []
    i = 0
    while len(out) < n and any(i < len(v) for v in by_role.values()):
        for role in sorted(by_role):
            if i < len(by_role[role]) and len(out) < n:
                out.append((by_role[role][i], role))
        i += 1
    return out


@app.command()
def ask(
    engine: str,
    out: Annotated[Path, typer.Option()],
    n: int = 20,
    systemone: Annotated[
        str | None, typer.Option(help="ask Kev on this llama-server /v1 URL instead (model kev4b)")
    ] = None,
) -> None:
    cfg = load_config(ROOT / "airlie.yaml")
    e = cfg.engines[engine]
    if systemone is not None:  # Kev after the config moved `decision` to mlx_vlm
        base, model, endpoint, key = systemone, "kev4b", "/systemone", None
    else:
        base, model, key = e.base_url or "", e.served_name(e.models[0]), e.api_key
        endpoint = "/decisions" if e.kind == "mlx_vlm" else "/systemone"
    criteria = routable_roles(cfg, exclude={cfg.studio.chief_role, cfg.studio.review.role})
    cases = pick(n)

    async def run() -> dict[str, object]:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        body = {
            "model": model,
            "state": cases[0][0],
            "questions": {"role": {"type": "choice", "instructions": ROLE_QUESTION, "criteria": criteria}},
        }
        async with httpx.AsyncClient(timeout=120, headers=headers) as h:
            raw = (await h.post(f"{base.rstrip('/')}{endpoint}", json=body)).json()
        client = JevClient(base, model=model, api_key=key, endpoint=endpoint)
        rows = []
        for task, expect in cases:
            t0 = time.perf_counter()
            a = await client.ask(task, "role", ROLE_QUESTION, criteria)
            rows.append(
                {
                    "task": task,
                    "expect": expect,
                    "choice": a.choice,
                    "confidence": a.confidence,
                    "probabilities": a.probabilities,
                    "input_tokens": a.input_tokens,
                    "seconds": round(time.perf_counter() - t0, 3),
                }
            )
        await client.aclose()
        return {
            "engine": engine,
            "model": model,
            "endpoint": endpoint,
            "criteria": criteria,
            "raw": raw,
            "rows": rows,
        }

    data = asyncio.run(run())
    out.write_text(json.dumps(data, indent=1) + "\n")
    rows = data["rows"]
    assert isinstance(rows, list)
    right = sum(r["choice"] == r["expect"] for r in rows)
    typer.echo(f"{engine} ({model}): {right}/{len(rows)} match the label; saved {out}")


@app.command()
def compare(base: Path, other: Path) -> None:
    a = json.loads(base.read_text())["rows"]
    b = json.loads(other.read_text())["rows"]
    pairs = list(zip(a, b, strict=True))
    agree = sum(x["choice"] == y["choice"] for x, y in pairs)
    delta = sum(abs(x["confidence"] - y["confidence"]) for x, y in pairs) / len(pairs)
    typer.echo(
        f"agreement {agree}/{len(pairs)} = {100 * agree / len(pairs):.0f}%; mean |Δconfidence| {delta:.3f}"
    )
    for x, y in pairs:
        mark = "=" if x["choice"] == y["choice"] else "≠"
        typer.echo(
            f"{mark} expect={x['expect']:<12} base={x['choice']:<12} {x['confidence']:.2f}  "
            f"other={y['choice']:<12} {y['confidence']:.2f}  {x['task'][:60]}"
        )


if __name__ == "__main__":
    app()
