"""Phase 4b gate, Step 3: a text-only model answers `pong` through its mlx_vlm engine, by exact model path.

uv run python scripts/gate_pong.py small generalist
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import typer

from airlie.config import load_config

ROOT = Path(__file__).resolve().parents[1]


def main(engines: list[str]) -> None:
    cfg = load_config(ROOT / "airlie.yaml")
    for name in engines:
        e = cfg.engines[name]
        model = e.served_name(e.models[0])
        headers = {"Authorization": f"Bearer {e.api_key}"} if e.api_key else {}
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
            "max_tokens": 8,
            "enable_thinking": False,
        }
        r = httpx.post(f"{e.base_url}/chat/completions", json=body, headers=headers, timeout=300)
        j = r.json()
        text = j["choices"][0]["message"].get("content") if r.status_code == 200 else None
        typer.echo(json.dumps({"engine": name, "model": model, "status": r.status_code, "text": text}))


if __name__ == "__main__":
    typer.run(main)
