"""POST /v1/airlie/chat/turn and the thread routes, through the real app with a scripted engine: the response
is the AI SDK UI message stream, the server keeps the thread, and a client's own history and tool list are
never used. A turn that has to be in flight while the test acts (busy, Stop, a disconnect) is driven over
raw ASGI, where the test is the client: it reads the body as it is sent and ends the call itself."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from airlie.auth.principal import SESSION_COOKIE
from airlie.auth.store import AuthStore
from airlie.manager.core import Manager
from airlie.serve.app import create_app
from tests.support.serve import FakeEngine, FakeUpstream, StuckClock
from tests.support.serve_chat import Call, Fail, Hang, ScriptedUpstream, Text, chat_cfg
from tests.unit.serve_helpers import LOOPBACK, TestClient

ELSEWHERE = ("10.0.0.5", 40000)


def turn_body(text: str = "hello", **extra: Any) -> dict[str, Any]:
    msg = {"id": "u1", "role": "user", "parts": [{"type": "text", "text": text}]}
    return {"id": "t1", "kind": "plain", "messages": [msg], **extra}


def chunks(raw: str) -> list[dict[str, Any]]:
    """The JSON chunks of an SSE body (the [DONE] sentinel is checked by the caller)."""
    frames = [f.removeprefix("data: ") for f in raw.split("\n\n") if f.strip()]
    return [json.loads(f) for f in frames if f != "[DONE]"]


def kinds(raw: str) -> list[str]:
    return [c["type"] for c in chunks(raw)]


def text_of(raw: str) -> str:
    return "".join(c["delta"] for c in chunks(raw) if c["type"] == "text-delta")


def app_for(tmp_path: Path, upstream: Callable[[httpx.Request], httpx.Response], **kw: Any):  # -> FastAPI
    return create_app(chat_cfg(tmp_path), transport=httpx.MockTransport(upstream), **kw)


def client_for(
    tmp_path: Path, up: ScriptedUpstream | None = None, **kw: Any
) -> tuple[TestClient, ScriptedUpstream]:
    up = up or ScriptedUpstream()
    return TestClient(app_for(tmp_path, up, **kw)), up


def post(c: TestClient, body: dict[str, Any] | None = None, **kw: Any) -> httpx.Response:
    return c.post("/v1/airlie/chat/turn", json=body if body is not None else turn_body(), **kw)


def tool_names(request_body: dict[str, Any]) -> set[str]:
    return {t["function"]["name"] for t in request_body.get("tools") or []}


# --- the stream and what it saves ---------------------------------------------------------------------------


def test_a_turn_streams_the_ui_message_stream_with_its_header(tmp_path: Path) -> None:
    c, _ = client_for(tmp_path, ScriptedUpstream(Text("hello there")))
    r = post(c)
    assert r.status_code == 200
    assert r.headers["x-vercel-ai-ui-message-stream"] == "v1"
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    seen = kinds(r.text)
    assert seen[:2] == ["start", "start-step"] and seen[-2:] == ["finish-step", "finish"]
    assert "text-start" in seen and "text-end" in seen
    assert text_of(r.text) == "hello there"
    assert r.text.endswith("data: [DONE]\n\n")


def test_the_turn_is_saved_and_get_returns_it(tmp_path: Path) -> None:
    c, _ = client_for(tmp_path, ScriptedUpstream(Text("hello there")))
    post(c, turn_body("what is up"))
    t = c.get("/v1/airlie/chat/threads/t1").json()
    assert (t["id"], t["kind"], t["title"], t["todos"]) == ("t1", "plain", "what is up", [])
    user, assistant = t["messages"]
    assert (user["id"], user["role"], user["parts"]) == (
        "u1",
        "user",
        [{"type": "text", "text": "what is up"}],
    )
    assert assistant["role"] == "assistant" and assistant["interrupted"] is False
    assert assistant["parts"] == [{"type": "text", "text": "hello there"}]
    assert assistant["usage"]["prompt_tokens"] == 7


def test_the_system_prompt_carries_the_bootstrap_and_the_skills_listing(tmp_path: Path) -> None:
    c, up = client_for(tmp_path)
    post(c)
    system = up.bodies[0]["messages"][0]
    assert system["role"] == "system"
    assert system["content"].startswith("CHAT PROMPT")
    assert "BOOTSTRAP BODY" in system["content"] and "TOOLS MAPPING" in system["content"]
    assert "- brainstorming: Use before any creative work" in system["content"]
    assert "- /story: Make a story" in system["content"]  # the chat role's slash commands too


def test_a_plain_chat_offers_the_model_only_the_plain_tools(tmp_path: Path) -> None:
    c, up = client_for(tmp_path)
    post(c)
    assert tool_names(up.bodies[0]) == {"web_search", "web_fetch", "skill", "todo"}


def test_a_tool_call_runs_on_the_server_and_its_output_goes_back_to_the_model(tmp_path: Path) -> None:
    c, up = client_for(tmp_path, ScriptedUpstream(Call("skill", {"name": "brainstorming"}), Text("done")))
    r = post(c)
    assert up.calls == 2
    second = up.bodies[1]["messages"]
    assert second[-1]["role"] == "tool" and "BRAINSTORM BODY" in second[-1]["content"]
    assert second[-2]["tool_calls"][0]["function"]["name"] == "skill"
    seen = kinds(r.text)
    assert seen.index("tool-input-available") < seen.index("tool-output-available") < seen.index("text-start")
    assistant = c.get("/v1/airlie/chat/threads/t1").json()["messages"][1]
    tool = next(p for p in assistant["parts"] if p["type"] == "tool-skill")
    assert tool["state"] == "output-available" and "BRAINSTORM BODY" in tool["output"]


def test_the_skill_tool_reads_a_chat_slash_skill(tmp_path: Path) -> None:
    c, up = client_for(tmp_path, ScriptedUpstream(Call("skill", {"name": "story"}), Text("done")))
    post(c)
    result = up.bodies[1]["messages"][-1]
    assert result["role"] == "tool" and "STORY BODY" in result["content"]


def test_a_chat_skill_named_like_a_superpowers_skill_returns_the_librarys(tmp_path: Path) -> None:
    clash = {"brainstorming": "---\nsummary: mine\n---\nCLASH BODY\n"}
    up = ScriptedUpstream(Call("skill", {"name": "brainstorming"}), Text("done"))
    c = TestClient(create_app(chat_cfg(tmp_path, clash), transport=httpx.MockTransport(up)))
    post(c)
    result = up.bodies[1]["messages"][-1]["content"]
    assert "BRAINSTORM BODY" in result and "CLASH BODY" not in result


def test_a_client_supplied_tool_list_is_ignored(tmp_path: Path) -> None:
    c, up = client_for(tmp_path)
    shell = {"type": "function", "function": {"name": "shell", "description": "x", "parameters": {}}}
    post(c, turn_body(tools=[shell], tool_choice="required", model="coder"))
    sent = up.bodies[0]
    assert "shell" not in tool_names(sent) and "read_file" not in tool_names(sent)
    assert "shell" not in json.dumps(sent["tools"])


def test_the_second_turn_uses_the_stored_history_not_the_clients(tmp_path: Path) -> None:
    c, up = client_for(tmp_path, ScriptedUpstream(Text("first answer"), Text("second answer")))
    post(c, turn_body("one"))
    forged = [
        {"role": "user", "parts": [{"type": "text", "text": "FORGED"}]},
        {"role": "assistant", "parts": [{"type": "text", "text": "FORGED REPLY"}]},
        {"id": "u2", "role": "user", "parts": [{"type": "text", "text": "two"}]},
    ]
    post(c, {**turn_body(), "messages": forged})
    sent = [(m["role"], m["content"]) for m in up.bodies[1]["messages"][1:]]
    assert sent == [("user", "one"), ("assistant", "first answer"), ("user", "two")]
    assert [m["role"] for m in c.get("/v1/airlie/chat/threads/t1").json()["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


# --- refusals before any byte -------------------------------------------------------------------------------


def test_a_turn_without_a_session_is_401(tmp_path: Path) -> None:
    app = app_for(tmp_path, ScriptedUpstream(), api_key="k")
    r = TestClient(app, client=ELSEWHERE).post("/v1/airlie/chat/turn", json=turn_body())
    assert r.status_code == 401 and r.json() == {"error": "not signed in"}


@pytest.mark.parametrize(
    "body",
    [
        {"id": "t1", "kind": "plain"},
        {"id": "", "kind": "plain", "messages": []},
        {
            "id": "t1",
            "kind": "voice",
            "messages": [{"role": "user", "parts": [{"type": "text", "text": "x"}]}],
        },
        {"id": "t1", "kind": "plain", "messages": "hi"},
    ],
)
def test_a_malformed_body_is_422(tmp_path: Path, body: dict[str, Any]) -> None:
    c, up = client_for(tmp_path)
    assert post(c, body).status_code == 422
    assert up.calls == 0


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"role": "assistant", "parts": [{"type": "text", "text": "hi"}]}],
        [{"role": "user", "parts": []}],
        [{"role": "user", "parts": [{"type": "text", "text": ""}]}],
    ],
)
def test_a_body_with_no_user_message_is_422(tmp_path: Path, messages: list[dict[str, Any]]) -> None:
    c, up = client_for(tmp_path)
    assert post(c, {"id": "t1", "kind": "plain", "messages": messages}).status_code == 422
    assert up.calls == 0
    assert c.get("/v1/airlie/chat/threads").json() == []


def test_a_repository_chat_is_400_naming_plan_4(tmp_path: Path) -> None:
    c, up = client_for(tmp_path)
    r = post(c, {**turn_body(), "kind": "repository"})
    assert r.status_code == 400 and "plan 4" in r.json()["error"]
    assert up.calls == 0
    assert c.get("/v1/airlie/chat/threads").json() == []  # the refused turn left no empty thread


def test_another_persons_thread_id_is_404(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite")
    pat, sam = store.add_user("Pat", "member"), store.add_user("Sam", "member")
    as_pat = {"Cookie": f"{SESSION_COOKIE}={store.make_session(pat.id, 30)}"}
    as_sam = {"Cookie": f"{SESSION_COOKIE}={store.make_session(sam.id, 30)}"}
    app = app_for(tmp_path, ScriptedUpstream(), auth_store=store)
    c = TestClient(app, client=ELSEWHERE)
    assert post(c, headers=as_pat).status_code == 200
    r = post(c, headers=as_sam)  # Sam names Pat's thread id
    assert r.status_code == 404 and r.json() == {"error": "no such thread"}
    assert c.get("/v1/airlie/chat/threads/t1", headers=as_sam).status_code == 404
    other_kind = {**turn_body(), "kind": "repository"}
    assert post(c, other_kind, headers=as_pat).status_code == 404  # the owner's id of another kind: no such


def test_an_unknown_slash_skill_is_404_before_any_byte_streams(tmp_path: Path) -> None:
    c, up = client_for(tmp_path)
    r = post(c, turn_body(skill="nope"))
    assert r.status_code == 404 and "unknown skill 'nope'" in r.json()["error"]
    assert not r.headers["content-type"].startswith("text/event-stream")
    assert up.calls == 0
    assert c.get("/v1/airlie/chat/threads").json() == []


def test_a_slash_skill_adds_its_body_to_that_turns_prompt(tmp_path: Path) -> None:
    c, up = client_for(tmp_path)
    assert post(c, turn_body(skill="story")).status_code == 200
    assert "STORY BODY" in up.bodies[0]["messages"][0]["content"]


def test_a_damaged_superpowers_tree_makes_every_turn_503_with_the_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = chat_cfg(tmp_path)
    (cfg.chat.superpowers.path / "skills" / "brainstorming" / "SKILL.md").write_text("tampered\n")
    up = ScriptedUpstream()
    with caplog.at_level("ERROR", logger="airlie.serve.app"):
        c = TestClient(create_app(cfg, transport=httpx.MockTransport(up)))
    r = post(c)
    assert r.status_code == 503
    assert (
        "chat is misconfigured" in r.json()["error"] and "changed: skills/brainstorming" in r.json()["error"]
    )
    assert up.calls == 0
    assert c.get("/v1/airlie/chat/threads").json() == []
    assert "chat is misconfigured" in caplog.text


def test_a_system_prompt_that_cannot_be_built_is_503_never_an_empty_prompt(tmp_path: Path) -> None:
    cfg = chat_cfg(tmp_path)
    up = ScriptedUpstream()
    c = TestClient(create_app(cfg, transport=httpx.MockTransport(up)))  # sound at startup ...
    skill = cfg.chat.superpowers.path / "skills" / "brainstorming" / "SKILL.md"
    skill.write_text("---\nname: brainstorming\n---\nno description\n")  # ... broken before the turn
    r = post(c)
    assert r.status_code == 503 and "chat is misconfigured" in r.json()["error"]
    assert "no description" in r.json()["error"]
    assert up.calls == 0
    assert c.get("/v1/airlie/chat/threads").json() == []


def test_too_many_attachments_is_400(tmp_path: Path) -> None:
    c, up = client_for(tmp_path)
    limit = c.get("/v1/airlie/chat/attachments").json()["max_per_message"]
    parts = [{"type": "text", "text": "look"}] + [{"type": "airlie_upload", "upload": "0" * 32}] * (limit + 1)
    r = post(c, {**turn_body(), "messages": [{"role": "user", "parts": parts}]})
    assert r.status_code == 400 and f"up to {limit} attachments" in r.json()["error"]
    assert up.calls == 0


def test_the_chat_role_not_configured_is_503(tmp_path: Path) -> None:
    cfg = chat_cfg(tmp_path)
    del cfg.roles["chat"]
    c = TestClient(create_app(cfg, transport=httpx.MockTransport(FakeUpstream())))
    r = post(c)
    assert r.status_code == 503 and "chat role is not configured" in r.json()["error"]


# --- attachments --------------------------------------------------------------------------------------------


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, "PNG")
    return buf.getvalue()


def test_a_text_attachment_reaches_the_model_and_is_kept_with_the_message(tmp_path: Path) -> None:
    c, up = client_for(tmp_path)
    up_id = c.post("/v1/airlie/uploads?name=notes.txt", content=b"the secret word is plover").json()["id"]
    attached = {"type": "airlie_upload", "upload": up_id, "name": "notes.txt"}
    msg = {"id": "u1", "role": "user", "parts": [{"type": "text", "text": "read this"}, attached]}
    assert post(c, {**turn_body(), "messages": [msg]}).status_code == 200
    user = up.bodies[0]["messages"][-1]
    assert user["role"] == "user"
    sent = json.dumps(user["content"])
    assert "read this" in sent and "the secret word is plover" in sent
    stored = c.get("/v1/airlie/chat/threads/t1").json()["messages"][0]
    assert stored["parts"] == [
        {"type": "text", "text": "read this"},
        attached,
    ]  # the reference, not the bytes


def test_an_ai_sdk_file_part_with_an_upload_url_is_an_attachment(tmp_path: Path) -> None:
    c, up = client_for(tmp_path)
    up_id = c.post("/v1/airlie/uploads?name=notes.txt", content=b"the secret word is plover").json()["id"]
    file_part = {
        "type": "file",
        "mediaType": "text/plain",
        "url": f"/v1/airlie/uploads/{up_id}",
        "filename": "notes.txt",
    }
    msg = {"id": "u1", "role": "user", "parts": [{"type": "text", "text": "read this"}, file_part]}
    assert post(c, {**turn_body(), "messages": [msg]}).status_code == 200
    assert "the secret word is plover" in json.dumps(up.bodies[0]["messages"][-1]["content"])
    stored = c.get("/v1/airlie/chat/threads/t1").json()["messages"][0]
    assert stored["parts"][1] == {"type": "airlie_upload", "upload": up_id, "name": "notes.txt"}


@pytest.mark.parametrize(
    "url", ["https://evil.example/x.png", "data:text/plain;base64,aGk=", "/v1/airlie/uploads/not-an-id"]
)
def test_a_file_part_that_is_not_an_airlie_upload_is_422(tmp_path: Path, url: str) -> None:
    c, up = client_for(tmp_path)
    msg = {"role": "user", "parts": [{"type": "text", "text": "hi"}, {"type": "file", "url": url}]}
    assert post(c, {**turn_body(), "messages": [msg]}).status_code == 422
    assert up.calls == 0


def test_a_picture_is_sent_as_an_image_part_and_stays_in_the_history_of_the_next_turn(tmp_path: Path) -> None:
    cfg = chat_cfg(tmp_path)
    cfg.roles["chat"].vision = True
    up = ScriptedUpstream()
    c = TestClient(create_app(cfg, transport=httpx.MockTransport(up)))
    pic = c.post("/v1/airlie/uploads?name=p.png", content=png()).json()["id"]
    attached = {"type": "airlie_upload", "upload": pic, "name": "p.png"}
    first = {"id": "u1", "role": "user", "parts": [{"type": "text", "text": "see"}, attached]}
    post(c, {**turn_body(), "messages": [first]})
    second = {"id": "u2", "role": "user", "parts": [{"type": "text", "text": "and now?"}]}
    post(c, {**turn_body(), "messages": [second]})

    def images(body: dict[str, Any]) -> list[Any]:
        return [
            p
            for m in body["messages"]
            if isinstance(m["content"], list)
            for p in m["content"]
            if p["type"] == "image_url"
        ]

    assert len(images(up.bodies[0])) == 1 and images(up.bodies[0])[0]["image_url"]["url"].startswith("data:")
    assert len(images(up.bodies[1])) == 1  # the earlier picture is still in what the model sees


# --- failure and the engine ---------------------------------------------------------------------------------


def test_an_engine_that_will_not_start_leaves_no_empty_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class WontStart:
        async def start(self) -> None:
            raise RuntimeError("engine client could not start")

        async def stop(self) -> None:
            pass

    monkeypatch.setattr("airlie.cli._http_engine", lambda *a, **k: WontStart())
    c, _ = client_for(tmp_path)
    c = TestClient(c.app, raise_server_exceptions=False)
    assert post(c).status_code == 500
    assert c.get("/v1/airlie/chat/threads").json() == []
    assert c.get("/v1/airlie/chat/threads/t1").status_code == 404


def test_a_failed_turn_leaves_no_two_user_messages_in_a_row_for_the_next(tmp_path: Path) -> None:
    c, up = client_for(tmp_path, ScriptedUpstream(Fail(500), Text("fine")))
    post(c, turn_body("first"))
    second = {"id": "u2", "role": "user", "parts": [{"type": "text", "text": "second"}]}
    post(c, {**turn_body(), "messages": [second]})
    sent = [(m["role"], m["content"]) for m in up.bodies[1]["messages"]]
    assert [r for r, _ in sent] == ["system", "user"]
    assert sent[1][1] == "first\n\nsecond"


def test_an_engine_failure_is_an_error_chunk_and_the_user_message_is_kept(tmp_path: Path) -> None:
    c, _ = client_for(tmp_path, ScriptedUpstream(Fail(500)))
    r = post(c, turn_body("will fail"))
    assert r.status_code == 200  # the first byte had gone: the error is a chunk
    err = next(x for x in chunks(r.text) if x["type"] == "error")
    assert "500" in err["errorText"]
    assert r.text.endswith("data: [DONE]\n\n")
    t = c.get("/v1/airlie/chat/threads/t1").json()
    assert [m["role"] for m in t["messages"]] == ["user"] and t["title"] == "will fail"
    assert post(c, turn_body("will fail")).status_code == 200  # the thread is free; a retry may reuse the id
    ids = [m["id"] for m in c.get("/v1/airlie/chat/threads/t1").json()["messages"]]
    assert len(ids) == len(set(ids)) == 3  # the first message, the retry and its answer


def managed(tmp_path: Path) -> tuple[Manager, FakeEngine]:
    engine = FakeEngine("main")
    return Manager([engine], budget_gb=100.0, clock=StuckClock()), engine


def test_the_manager_shows_chat_thread_id_as_a_holder_during_the_turn(tmp_path: Path) -> None:
    manager, _ = managed(tmp_path)
    seen: list[tuple[str, ...]] = []
    up = ScriptedUpstream(observe=lambda: seen.append(manager.status()[0].holders))
    c, _ = client_for(tmp_path, up, manager=manager)
    post(c)
    assert seen == [("chat:t1",)]
    assert manager.status()[0].holders == ()


def test_an_on_finish_hook_registered_on_the_app_receives_the_summary(tmp_path: Path) -> None:
    c, _ = client_for(tmp_path, ScriptedUpstream(Text("all done")))
    got: list[Any] = []

    async def on_finish(summary: Any) -> None:
        got.append(summary)

    c.app.state.chat_hooks.on("on_finish", on_finish)  # type: ignore[attr-defined]
    post(c)
    (summary,) = got
    assert (summary.thread_id, summary.kind, summary.text) == ("t1", "plain", "all done")
    assert summary.title == "hello" and not summary.interrupted and not summary.failed


# --- a turn in flight: busy, Stop, disconnect ---------------------------------------------------------------


class ASGIClient:
    """One request made straight to the ASGI app: the test is the client, reads each message the app sends,
    and ends the call itself (by `disconnect`)."""

    def __init__(self, app: Any, path: str, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode()
        self.sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.inbox.put_nowait({"type": "http.request", "body": raw, "more_body": False})
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())],
            "client": LOOPBACK,
            "server": ("testserver", 80),
        }
        self.task = asyncio.ensure_future(app(scope, self.inbox.get, self.sent.put))
        self.body = b""

    async def until(self, text: bytes) -> None:
        """Read what the app sends until `text` has come in the body."""
        while text not in self.body:
            message = await asyncio.wait_for(self.sent.get(), 5)
            if message["type"] == "http.response.start":
                self.status = message["status"]
            self.body += message.get("body", b"")

    def disconnect(self) -> None:
        self.inbox.put_nowait({"type": "http.disconnect"})

    def drain(self) -> None:
        """Take in everything the app has sent since."""
        while not self.sent.empty():
            self.body += self.sent.get_nowait().get("body", b"")


async def let_the_loop_run(turns: int = 20) -> None:
    """Give every task that is ready its turn, as many times as `turns`: what the app does with a message
    just handed to it (a disconnect) is done by the time this returns."""
    await asyncio.gather(*(asyncio.sleep(0) for _ in range(turns)))


def status(manager: Manager) -> Any:
    return manager.status()[0]


async def test_a_second_turn_on_a_busy_thread_is_409(tmp_path: Path) -> None:
    manager, _ = managed(tmp_path)
    app = app_for(tmp_path, ScriptedUpstream(Hang()), manager=manager)
    first = ASGIClient(app, "/v1/airlie/chat/turn", turn_body())
    await first.until(b"text-delta")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, client=LOOPBACK), base_url="http://t"
    ) as c:
        r = await c.post("/v1/airlie/chat/turn", json=turn_body("again"))
        assert r.status_code == 409 and "turn running" in r.json()["error"]
        assert (await c.delete("/v1/airlie/chat/threads/t1")).status_code == 409  # not while it runs
        other = await c.post("/v1/airlie/chat/turn", json={**turn_body(), "id": "t2"})
        assert other.status_code == 200  # a different thread is free
        await c.post("/v1/airlie/chat/threads/t1/stop")
        await asyncio.wait_for(first.task, 5)
        assert (await c.post("/v1/airlie/chat/turn", json=turn_body("after"))).status_code == 200  # released


async def test_stop_cancels_the_turn_in_progress_and_saves_the_partial(tmp_path: Path) -> None:
    manager, _ = managed(tmp_path)
    app = app_for(tmp_path, ScriptedUpstream(Hang("partial")), manager=manager)
    turn = ASGIClient(app, "/v1/airlie/chat/turn", turn_body())
    await turn.until(b"partial")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, client=LOOPBACK), base_url="http://t"
    ) as c:
        assert (await c.post("/v1/airlie/chat/threads/t1/stop")).status_code == 204
        await asyncio.wait_for(turn.task, 5)  # the stream ends by itself
        turn.drain()
        assert turn.body.rstrip().endswith(b"data: [DONE]")
        assert b'"type":"finish"' in turn.body
        t = (await c.get("/v1/airlie/chat/threads/t1")).json()
        assistant = t["messages"][1]
        assert assistant["interrupted"] is True and assistant["parts"] == [
            {"type": "text", "text": "partial"}
        ]
        assert status(manager).holders == ()
        assert (await c.post("/v1/airlie/chat/turn", json=turn_body("next"))).status_code == 200


async def test_stop_with_no_turn_running_is_204_and_another_persons_thread_is_404(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite")
    pat, sam = store.add_user("Pat", "member"), store.add_user("Sam", "member")
    as_pat = {"Cookie": f"{SESSION_COOKIE}={store.make_session(pat.id, 30)}"}
    as_sam = {"Cookie": f"{SESSION_COOKIE}={store.make_session(sam.id, 30)}"}
    app = app_for(tmp_path, ScriptedUpstream(), auth_store=store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, client=ELSEWHERE), base_url="http://t"
    ) as c:
        assert (await c.post("/v1/airlie/chat/turn", json=turn_body(), headers=as_pat)).status_code == 200
        assert (await c.post("/v1/airlie/chat/threads/t1/stop", headers=as_pat)).status_code == 204  # done
        assert (await c.post("/v1/airlie/chat/threads/t1/stop", headers=as_sam)).status_code == 404
        assert (await c.post("/v1/airlie/chat/threads/ghost/stop", headers=as_pat)).status_code == 404
        assert (await c.post("/v1/airlie/chat/threads/t1/stop")).status_code == 401


async def test_a_disconnect_lets_the_turn_finish_save_and_free_the_thread(tmp_path: Path) -> None:
    manager, _ = managed(tmp_path)
    finished: list[Any] = []

    async def on_finish(summary: Any) -> None:
        finished.append(summary)

    release = asyncio.Event()
    app = app_for(tmp_path, ScriptedUpstream(Hang("partial", then=release)), manager=manager)
    app.state.chat_hooks.on("on_finish", on_finish)
    turn = ASGIClient(app, "/v1/airlie/chat/turn", turn_body())
    await turn.until(b"partial")
    assert status(manager).holders == ("chat:t1",)
    turn.disconnect()  # the client's connection drops: an `http.disconnect`, not a closed generator
    await let_the_loop_run()
    release.set()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(turn.task, 5)
    assert status(manager).holders == ()
    [summary] = finished
    assert (summary.disconnected, summary.interrupted, summary.failed) == (True, False, False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, client=LOOPBACK), base_url="http://t"
    ) as c:
        t = (await c.get("/v1/airlie/chat/threads/t1")).json()
        assert [m["role"] for m in t["messages"]] == ["user", "assistant"]
        assert (
            t["messages"][1]["interrupted"] is False
            and t["messages"][1]["parts"][0]["text"] == "partial done"
        )
        assert (
            await c.post("/v1/airlie/chat/turn", json=turn_body("again"))
        ).status_code == 200  # thread freed


# --- the thread routes --------------------------------------------------------------------------------------


def three_threads(c: TestClient) -> None:
    for tid, kind, text in (("a", "plain", "first one"), ("b", "plain", "second one")):
        post(c, {**turn_body(text), "id": tid, "kind": kind})


def test_thread_list_returns_the_signed_in_persons_threads_newest_first(tmp_path: Path) -> None:
    c, _ = client_for(tmp_path)
    three_threads(c)
    rows = c.get("/v1/airlie/chat/threads").json()
    assert [(r["id"], r["title"], r["kind"]) for r in rows] == [
        ("b", "second one", "plain"),
        ("a", "first one", "plain"),
    ]
    assert all(isinstance(r["updated"], float) for r in rows)
    assert c.get("/v1/airlie/chat/threads?kind=repository").json() == []
    assert len(c.get("/v1/airlie/chat/threads?kind=plain").json()) == 2
    assert c.get("/v1/airlie/chat/threads?kind=voice").status_code == 422


def test_thread_get_returns_the_thread_and_404s_for_none(tmp_path: Path) -> None:
    c, _ = client_for(
        tmp_path,
        ScriptedUpstream(
            Call("todo", {"items": [{"id": "1", "text": "x", "status": "pending"}]}), Text("ok")
        ),
    )
    post(c)
    t = c.get("/v1/airlie/chat/threads/t1").json()
    assert t["todos"] == [{"id": "1", "text": "x", "status": "pending"}]
    assert set(t) == {"id", "title", "kind", "created", "updated", "todos", "messages"}
    r = c.get("/v1/airlie/chat/threads/ghost")
    assert r.status_code == 404 and r.json() == {"error": "no such thread"}


def test_thread_delete_removes_it_and_404s_for_none(tmp_path: Path) -> None:
    c, _ = client_for(tmp_path)
    post(c)
    assert c.delete("/v1/airlie/chat/threads/t1").status_code == 204
    assert c.get("/v1/airlie/chat/threads/t1").status_code == 404
    assert c.delete("/v1/airlie/chat/threads/t1").status_code == 404


def test_the_thread_routes_need_a_session(tmp_path: Path) -> None:
    app = app_for(tmp_path, ScriptedUpstream(), api_key="k")
    c = TestClient(app, client=ELSEWHERE)
    for call in (
        c.get("/v1/airlie/chat/threads"),
        c.get("/v1/airlie/chat/threads/t1"),
        c.delete("/v1/airlie/chat/threads/t1"),
        c.post("/v1/airlie/chat/threads/t1/stop"),
    ):
        assert call.status_code == 401


def test_the_thread_routes_never_show_another_persons_threads(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite")
    pat, sam = store.add_user("Pat", "member"), store.add_user("Sam", "member")
    as_pat = {"Cookie": f"{SESSION_COOKIE}={store.make_session(pat.id, 30)}"}
    as_sam = {"Cookie": f"{SESSION_COOKIE}={store.make_session(sam.id, 30)}"}
    c = TestClient(app_for(tmp_path, ScriptedUpstream(), auth_store=store), client=ELSEWHERE)
    post(c, headers=as_pat)
    assert c.get("/v1/airlie/chat/threads", headers=as_sam).json() == []
    assert c.delete("/v1/airlie/chat/threads/t1", headers=as_sam).status_code == 404
    assert len(c.get("/v1/airlie/chat/threads", headers=as_pat).json()) == 1


# --- the old route beside the new one -----------------------------------------------------------------------


def test_chat_completions_still_works_unchanged(tmp_path: Path) -> None:
    up = FakeUpstream()
    c = TestClient(create_app(chat_cfg(tmp_path), transport=httpx.MockTransport(up)))
    r = c.post(
        "/v1/chat/completions", json={"model": "chat", "messages": [{"role": "user", "content": "yo"}]}
    )
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "hi"
    sent = json.loads(up.requests[0].content)
    assert sent["messages"][0]["content"].startswith("CHAT PROMPT") and "BOOTSTRAP" not in json.dumps(sent)
