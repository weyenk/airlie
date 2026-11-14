"""Shared helpers extracted from tests/unit/test_serve_chat.py for tests.support.serve_chat."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from airlie.config import load_config


def superpowers_tree(root: Path) -> Path:
    """A tiny vendored-superpowers tree: the bootstrap skill, one other skill and the tools mapping."""
    for name, desc, body in (
        ("using-superpowers", "Use when starting any conversation", "BOOTSTRAP BODY"),
        ("brainstorming", "Use before any creative work", "BRAINSTORM BODY"),
    ):
        d = root / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n")
    (root / "airlie-tools.md").write_text("TOOLS MAPPING\n")
    files = sorted(p for p in root.rglob("*") if p.is_file())
    lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(root).as_posix()}" for p in files]
    (root / "MANIFEST.sha256").write_text("\n".join(lines) + "\n")
    return root


def chat_cfg(tmp_path: Path, skills: dict[str, str] | None = None):
    skills = (
        {"story": "---\nsummary: Make a story\naction: story\n---\nSTORY BODY\n"}
        if skills is None
        else skills
    )
    (tmp_path / "skills").mkdir()
    for name, text in skills.items():
        (tmp_path / "skills" / f"{name}.md").write_text(text)
    for r, text in {
        "architect": "x",
        "summarizer": "x",
        "judge": "x",
        "aggregator": "x",
        "chief": "CHIEF PROMPT",
        "chat": "---\nsummary: chat\nroutable: false\n---\nCHAT PROMPT\n",
    }.items():
        (tmp_path / f"{r}.md").write_text(text)
    roles = {
        r: {"prompt": f"{r}.md", "model": "base"}
        for r in ("architect", "summarizer", "judge", "aggregator", "chief")
    }
    roles["chat"] = {"prompt": "chat.md", "model": "base", "skills": "skills"}
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://engine.test/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": roles,
        "observability": {"state_dir": str(tmp_path / "state")},
        "chat": {"superpowers": {"path": str(superpowers_tree(tmp_path / "superpowers"))}},
    }
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class Call:
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Fail:
    status: int


@dataclass(frozen=True)
class Hang:
    """One text chunk, then nothing until the request is cancelled; or, with `then`, until it is set, after
    which the reply ends with " done"."""

    text: str = "hi"
    then: asyncio.Event | None = None


@dataclass(frozen=True)
class Slow:
    """A text reply streamed word by word, `pause_s` apart (on the real clock: an e2e test watches a reply
    while it is still streaming)."""

    text: str
    pause_s: float = 0.5


def frame(delta: dict[str, Any], finish: str | None = None) -> bytes:
    choice = {"index": 0, "delta": delta, "finish_reason": finish}
    return b"data: " + json.dumps({"choices": [choice]}).encode() + b"\n\n"


USAGE_FRAME = b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\ndata: [DONE]\n\n'


class HangStream(httpx.AsyncByteStream):
    def __init__(self, first: bytes, then: asyncio.Event | None = None) -> None:
        self.first = first
        self.then = then

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.first
        if self.then is None:
            await asyncio.Event().wait()
            return
        await self.then.wait()
        yield frame({"content": " done"}) + frame({}, "stop")
        yield USAGE_FRAME


class SlowStream(httpx.AsyncByteStream):
    def __init__(self, text: str, pause_s: float) -> None:
        self.words = text.split(" ")
        self.pause_s = pause_s

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for i, word in enumerate(self.words):
            if i:
                await asyncio.sleep(self.pause_s)
            yield frame({"content": word if i == 0 else f" {word}"})
        yield frame({}, "stop") + USAGE_FRAME


class ScriptedUpstream:
    """The engine's streamed chat/completions, one scripted reply per request (then "hi"), the way mlx-vlm
    streams it: content deltas, a tool call in one delta, a usage frame. `bodies` are the requests; `observe`
    is called as each arrives."""

    def __init__(
        self, *replies: Text | Call | Fail | Hang | Slow, observe: Callable[[], Any] | None = None
    ) -> None:
        self.replies = list(replies)
        self.bodies: list[dict[str, Any]] = []
        self.observe = observe
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        if self.observe is not None:
            self.observe()
        self.calls += 1
        reply = self.replies.pop(0) if self.replies else Text("hi")
        sse = {"content-type": "text/event-stream"}
        usage = b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\ndata: [DONE]\n\n'
        match reply:
            case Fail(status):
                return httpx.Response(status, json={"error": "scripted failure"})
            case Hang(text, then):
                return httpx.Response(200, stream=HangStream(frame({"content": text}), then), headers=sse)
            case Slow(text, pause_s):
                return httpx.Response(200, stream=SlowStream(text, pause_s), headers=sse)
            case Call(name, args):
                call = {"index": 0, "id": f"call_{self.calls}", "type": "function"}
                call["function"] = {"name": name, "arguments": json.dumps(args)}
                body = frame({"tool_calls": [call]}) + frame({}, "tool_calls") + usage
            case Text(text):
                body = frame({"content": text}) + frame({}, "stop") + usage
        return httpx.Response(200, content=body, headers=sse)
