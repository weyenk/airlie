"""LHR authoring tools: stub API, session, create→photo→preview→publish."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from airlie.config import load_config
from airlie.config.schema import AttachmentsSpec
from airlie.serve.app import create_app
from airlie.serve.uploads import UploadStore
from airlie.tools.lhr import LhrCreateDraft, LhrPreview, LhrPublish, LhrUploadPhoto
from airlie.tools.lhr_session import LhrSessionStore
from airlie.tools.protocol import ToolContext
from tests.unit.serve_helpers import TestClient
from tests.unit.test_serve import FakeUpstream


class StubAuthoring:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.drafts: dict[str, dict[str, Any]] = {}
        self._n = 0
        self.authenticated = True

    async def request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        if not self.authenticated:
            return 401, {"error": "not signed in"}
        self.calls.append((method, path, json_body))
        if method == "POST" and path == "/drafts":
            self._n += 1
            did = f"d{self._n}"
            post_type = (json_body or {}).get("type", "recipe")
            title = (json_body or {}).get("title", "")
            self.drafts[did] = {
                "id": did,
                "type": post_type,
                "draft": {
                    "kind": "post",
                    "postType": post_type,
                    "title": title,
                    "ingredients": [],
                    "steps": [],
                    "sections": [],
                    "photos": [],
                    "kitchenwareIds": [],
                    "affiliateLinkIds": [],
                    "pendingAffiliateLinks": [],
                    "pendingIngredientLinks": [],
                    "variants": [],
                },
            }
            return 201, {"draftId": did}
        if method == "GET" and path == "/drafts/d1":
            return 200, self.drafts["d1"]
        if method == "POST" and path == "/drafts/d1/photo":
            row = self.drafts["d1"]
            draft = row["draft"]
            cap = (json_body or {}).get("caption")
            draft["photos"] = [{"url": "https://cdn.example/p.jpg", "caption": cap}]
            return 200, row
        if method == "POST" and path == "/drafts/d1/preview-token":
            return 200, {"token": "abc", "expiresAt": 9999999999999}
        if method == "POST" and path == "/drafts/d1/publish":
            return 200, {"slug": "my-post", "url": "/posts/my-post/"}
        return 404, {"error": "stub miss", "method": method, "path": path}


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(tmp_path, output_cap_chars=50_000, role="lhr")


def test_tools_refuse_without_session(tmp_path: Path, ctx: ToolContext) -> None:
    stub = StubAuthoring()
    stub.authenticated = False
    tool = LhrCreateDraft(stub)
    res = asyncio.run(tool.run({"type": "recipe"}, ctx))
    assert not res.ok and "not signed in" in res.output.lower()


def test_create_photo_preview_publish(tmp_path: Path, ctx: ToolContext) -> None:
    stub = StubAuthoring()
    store = LhrSessionStore(tmp_path)
    store.set_token("test-token")

    create = LhrCreateDraft(stub)
    r1 = asyncio.run(create.run({"type": "recipe", "title": "Soup"}, ctx))
    assert r1.ok and "draftId" in r1.output

    uploads = UploadStore(tmp_path / "uploads", AttachmentsSpec())
    uid = "a" * 32
    (tmp_path / "uploads").mkdir(parents=True, exist_ok=True)
    (tmp_path / "uploads" / uid).write_bytes(b"\xff\xd8\xff fake jpeg")
    meta = {
        "id": uid,
        "name": "pic.jpg",
        "mime": "image/jpeg",
        "size": 10,
        "kind": "image",
        "owner": "u",
        "at": 0.0,
    }
    (tmp_path / "uploads" / f"{uid}.json").write_text(json.dumps(meta))

    photo = LhrUploadPhoto(stub, uploads, lambda u: True)
    r2 = asyncio.run(photo.run({"draft_id": "d1", "upload_id": uid, "caption": "bowl"}, ctx))
    assert r2.ok

    preview = LhrPreview(stub, site_base_url="https://site.test")
    r3 = asyncio.run(preview.run({"draft_id": "d1"}, ctx))
    assert r3.ok and "Photos: 1" in r3.output and "https://site.test/preview/d1" in r3.output

    publish = LhrPublish(stub)
    r4 = asyncio.run(publish.run({"draft_id": "d1"}, ctx))
    assert r4.ok and "my-post" in r4.output

    methods = [c[0] for c in stub.calls]
    assert methods == ["POST", "POST", "GET", "POST", "POST"]


def _mini_cfg(tmp_path: Path) -> Path:
    (tmp_path / "prompts" / "roles").mkdir(parents=True)
    for name in ("chat", "lhr", "coder", "summarizer", "judge", "aggregator"):
        (tmp_path / "prompts" / "roles" / f"{name}.md").write_text(f"# {name}\n")
    (tmp_path / "skills" / "chat").mkdir(parents=True)
    (tmp_path / "skills" / "chat" / "lhr-publish.md").write_text("---\nsummary: pub\n---\nBODY\n")
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://engine.test/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": {
            "coder": {"prompt": "prompts/roles/coder.md", "model": "base"},
            "summarizer": {"prompt": "prompts/roles/summarizer.md", "model": "base"},
            "judge": {"prompt": "prompts/roles/judge.md", "model": "base"},
            "aggregator": {"prompt": "prompts/roles/aggregator.md", "model": "base"},
            "chat": {"prompt": "prompts/roles/chat.md", "model": "base", "skills": "skills/chat"},
            "lhr": {
                "prompt": "prompts/roles/lhr.md",
                "model": "base",
                "tools": [
                    "lhr_create_draft",
                    "lhr_update_section",
                    "lhr_set_recipe",
                    "lhr_upload_photo",
                    "lhr_preview",
                    "lhr_publish",
                    "lhr_list_drafts",
                ],
                "max_steps": 4,
            },
        },
        "observability": {"state_dir": str(tmp_path / "state")},
        "lhr": {"office_url": "http://office.test"},
    }
    path = tmp_path / "airlie.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_lhr_publish_skill_runs_tool_loop_not_plain_proxy(tmp_path: Path) -> None:
    """Chat with skill lhr-publish uses HTTPEngine + tools; stub engine returns final text."""
    cfg_path = _mini_cfg(tmp_path)
    cfg = load_config(cfg_path)

    class EngineStub(FakeUpstream):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            body = json.loads(request.content)
            if body.get("stream"):
                if body.get("tools"):
                    sse = (
                        b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"c1",'
                        b'"type":"function","function":{"name":"lhr_create_draft","arguments":"{\\"type\\":\\"recipe\\"}"}}]},'
                        b'"finish_reason":null}]}\n\n'
                        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n'
                        b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
                        b"data: [DONE]\n\n"
                    )
                else:
                    sse = (
                        b'data: {"choices":[{"index":0,"delta":{"content":"done"},"finish_reason":null}]}\n\n'
                        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                        b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":3}}\n\n'
                        b"data: [DONE]\n\n"
                    )
                return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})
            if body.get("tools"):
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "",
                                    "tool_calls": [
                                        {
                                            "id": "c1",
                                            "type": "function",
                                            "function": {
                                                "name": "lhr_create_draft",
                                                "arguments": '{"type":"recipe"}',
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3},
                },
            )

    LhrSessionStore(cfg.observability.state_dir).set_token("tok")

    engine = EngineStub()
    n_engine = [0]

    def routing(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        if host == "office.test":
            if request.method == "POST" and request.url.path.endswith("/drafts"):
                return httpx.Response(201, json={"draftId": "d9"})
            return httpx.Response(404, json={"error": "nope"})
        n_engine[0] += 1
        return engine(request)

    app = create_app(cfg, transport=httpx.MockTransport(routing), api_key="k")
    client = TestClient(app)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "chat",
            "skill": "lhr-publish",
            "messages": [{"role": "user", "content": "publish soup"}],
        },
        headers={"Authorization": "Bearer k"},
    )
    assert r.status_code == 200, r.text
    assert len(engine.requests) >= 2
    first = json.loads(engine.requests[0].content)
    assert first.get("tools")
    assert any(t["function"]["name"] == "lhr_create_draft" for t in first["tools"])


def test_repo_config_knows_lhr_tools() -> None:
    root = Path(__file__).resolve().parents[2]
    cfg = load_config(root / "airlie.yaml", local_path=root / "no-such-local.yaml")
    assert "lhr" in cfg.roles
    names = set(cfg.roles["lhr"].tools or [])
    assert "lhr_publish" in names and "lhr_create_draft" in names
    assert cfg.validate_references() == []
