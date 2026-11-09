"""Record raw vllm-mlx responses as test fixtures (M1 Task 2). Run against a hand-started server:

    uv run vllm-mlx serve <snapshot> --port 8099 --continuous-batching --reasoning-parser qwen3 \
        --enable-auto-tool-choice --tool-call-parser qwen --offline
    uv run python scripts/record_fixtures.py --base-url http://127.0.0.1:8099/v1 --model <served name>

Writes the bodies verbatim into tests/fixtures/<--out>/. HTTPEngine is written against these, not docs.

For mlx-vlm's own server, start it with
`python -m mlx_vlm.server --model <snapshot> --host 127.0.0.1 --port 8094` and run with
`--out mlx_vlm --dialect mlx_vlm --model <snapshot>` (the exact --model string)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8099/v1")
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", default="vllm_mlx", help="fixture subdirectory under tests/fixtures")
    ap.add_argument("--tool-parser", default="qwen3_coder", help="mlx_vlm only: per-request tool_parser")
    ap.add_argument("--dialect", choices=["vllm_mlx", "mlx_vlm"], default="vllm_mlx")
    a = ap.parse_args()
    root = a.base_url.removesuffix("/v1")
    OUT = FIXTURES / a.out
    vlm = a.dialect == "mlx_vlm"

    def thinking(on: bool) -> dict:
        # mlx-vlm reads a top-level enable_thinking; vllm-mlx reads chat_template_kwargs.
        return {"enable_thinking": on} if vlm else {"chat_template_kwargs": {"enable_thinking": on}}

    OUT.mkdir(parents=True, exist_ok=True)
    c = httpx.Client(timeout=300)

    def stream(name: str, body: dict) -> None:
        with c.stream("POST", f"{a.base_url}/chat/completions", json=body) as r:
            raw = b"".join(r.iter_raw())
        (OUT / f"{name}.sse").write_bytes(raw)
        print(f"{name}: {r.status_code} {len(raw)} bytes")

    def once(name: str, method: str, url: str, body: dict | None = None) -> None:
        r = c.request(method, url, json=body)
        (OUT / f"{name}.json").write_bytes(r.content)
        print(f"{name}: {r.status_code} {len(r.content)} bytes")

    once("health", "GET", f"{root}/health")
    once("models", "GET", f"{a.base_url}/models")
    if vlm:
        once("metrics", "GET", f"{root}/v1/metrics")
    base = {"model": a.model, "stream": True, "stream_options": {"include_usage": True}, "temperature": 0}
    stream(
        "plain",
        {
            **base,
            "max_tokens": 32,
            **thinking(False),
            "messages": [
                {"role": "system", "content": "Reply in one short sentence."},
                {"role": "user", "content": "Name three primary colours."},
            ],
        },
    )
    stream(
        "reasoning",
        {
            **base,
            "max_tokens": 256,
            **thinking(True),
            **({"thinking_budget": 128} if vlm else {}),
            "messages": [
                {"role": "user", "content": "What is 17 * 23? Think briefly, then answer with the number."}
            ],
        },
    )
    stream(
        "tool_call",
        {
            **base,
            "max_tokens": 128,
            **thinking(False),
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a file",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    },
                }
            ],
            "tool_choice": "auto",
            **({"tool_parser": a.tool_parser} if vlm else {}),
            "messages": [{"role": "user", "content": "Read the file README.md using the tool."}],
        },
    )
    # second turn after a tool result: does usage report cached prompt tokens?
    stream(
        "resume",
        {
            **base,
            "max_tokens": 32,
            **thinking(False),
            "messages": [
                {"role": "system", "content": "Reply in one short sentence."},
                {"role": "user", "content": "Name three primary colours."},
                {"role": "assistant", "content": "Red, blue and yellow."},
                {"role": "user", "content": "And one secondary colour?"},
            ],
        },
    )
    if not vlm:
        once(
            "logprobs",
            "POST",
            f"{a.base_url}/chat/completions",
            {
                "model": a.model,
                "stream": False,
                "max_tokens": 1,
                "temperature": 0,
                "logprobs": True,
                "top_logprobs": 5,
                **thinking(False),
                "messages": [
                    {"role": "system", "content": "Answer with exactly one word: YES or NO."},
                    {"role": "user", "content": "Is water wet?"},
                ],
            },
        )
    once(
        "nonstream",
        "POST",
        f"{a.base_url}/chat/completions",
        {
            "model": a.model,
            "stream": False,
            "max_tokens": 16,
            "temperature": 0,
            **thinking(False),
            "messages": [{"role": "user", "content": "Say hello."}],
        },
    )
    once(
        "error_bad_model",
        "POST",
        f"{a.base_url}/chat/completions",
        {"model": "does-not-exist", "messages": [{"role": "user", "content": "x"}]},
    )
    print(json.dumps({"written": sorted(p.name for p in OUT.iterdir())}, indent=1))


if __name__ == "__main__":
    main()
