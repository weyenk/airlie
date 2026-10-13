"""Record raw vllm-mlx responses as test fixtures (M1 Task 2). Run against a hand-started server:

    uv run vllm-mlx serve <snapshot> --port 8099 --continuous-batching --reasoning-parser qwen3 \
        --enable-auto-tool-choice --tool-call-parser qwen --offline
    uv run python scripts/record_fixtures.py --base-url http://127.0.0.1:8099/v1 --model <served name>

Writes the bodies verbatim into tests/fixtures/vllm_mlx/. HTTPEngine is written against these, not docs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "vllm_mlx"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8099/v1")
    ap.add_argument("--model", required=True)
    a = ap.parse_args()
    root = a.base_url.removesuffix("/v1")
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
    base = {"model": a.model, "stream": True, "stream_options": {"include_usage": True}, "temperature": 0}
    stream(
        "plain",
        {
            **base,
            "max_tokens": 32,
            "chat_template_kwargs": {"enable_thinking": False},
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
            "chat_template_kwargs": {"enable_thinking": True},
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
            "chat_template_kwargs": {"enable_thinking": False},
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
            "messages": [{"role": "user", "content": "Read the file README.md using the tool."}],
        },
    )
    # second turn after a tool result: does usage report cached prompt tokens?
    stream(
        "resume",
        {
            **base,
            "max_tokens": 32,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {"role": "system", "content": "Reply in one short sentence."},
                {"role": "user", "content": "Name three primary colours."},
                {"role": "assistant", "content": "Red, blue and yellow."},
                {"role": "user", "content": "And one secondary colour?"},
            ],
        },
    )
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
            "chat_template_kwargs": {"enable_thinking": False},
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
            "chat_template_kwargs": {"enable_thinking": False},
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
