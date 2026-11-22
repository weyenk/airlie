"""A turn runs on the thread's own model and persona: the upstream request names the thread's model, the
system prompt is platform, kind note, persona, and sampling comes from the persona's role while the tools
stay the kind's. A turn may name its pick (checked as PATCH checks it) and `/status` reports the model."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from airlie.auth.principal import SESSION_COOKIE
from airlie.auth.store import AuthStore
from airlie.chat.hooks import PLAIN_NOTE
from airlie.config.schema import ModelSpec
from airlie.serve.app import create_app
from tests.support.serve_chat import ScriptedUpstream, chat_cfg
from tests.unit.serve_helpers import TestClient

ELSEWHERE = ("10.0.0.5", 40000)


def make(tmp_path: Path) -> tuple[TestClient, ScriptedUpstream]:
    """A client off the Mac signed in as Pat; the engine serves `base` and `other`, and the `chief` role has
    a prompt and tools of its own."""
    conf = chat_cfg(tmp_path)
    conf.models["other"] = ModelSpec(label="Other model")
    conf.engines["main"].models.append("other")
    conf.roles["chief"].tools = ["shell"]  # a persona's own tools must never reach a chat
    conf.roles["chief"].max_tokens = 123
    up = ScriptedUpstream()

    async def live_ids() -> list[str]:
        return ["base", "other"]

    store = AuthStore(tmp_path / "auth.sqlite")
    pat = store.add_user("Pat", "member")
    app = create_app(conf, transport=httpx.MockTransport(up), auth_store=store, model_live_ids=live_ids)
    cookie = {"Cookie": f"{SESSION_COOKIE}={store.make_session(pat.id, 30)}"}
    return TestClient(app, client=ELSEWHERE, headers=cookie), up


def body(**extra: Any) -> dict[str, Any]:
    msg = {"id": "u1", "role": "user", "parts": [{"type": "text", "text": "hello"}]}
    return {"id": "t1", "kind": "plain", "messages": [msg], **extra}


def system_of(req: dict[str, Any]) -> str:
    return next(m["content"] for m in req["messages"] if m["role"] == "system")


def tool_names(req: dict[str, Any]) -> list[str]:
    return [t["function"]["name"] for t in req.get("tools") or []]


def test_a_turn_runs_on_the_models_the_thread_was_patched_to(tmp_path: Path) -> None:
    c, up = make(tmp_path)
    t = c.post("/v1/airlie/chat/threads", json={"kind": "plain"}).json()
    assert c.patch(f"/v1/airlie/chat/threads/{t['id']}", json={"model": "other"}).status_code == 200

    r = c.post("/v1/airlie/chat/turn", json=body() | {"id": t["id"]})

    assert r.status_code == 200
    assert up.bodies[0]["model"] == "other"


def test_a_new_thread_runs_on_the_chat_roles_model(tmp_path: Path) -> None:
    c, up = make(tmp_path)

    c.post("/v1/airlie/chat/turn", json=body())

    assert up.bodies[0]["model"] == "base"


def test_a_turn_naming_a_model_and_persona_changes_the_thread_and_runs_on_them(tmp_path: Path) -> None:
    c, up = make(tmp_path)

    r = c.post("/v1/airlie/chat/turn", json=body(model="other", persona="chief"))

    assert r.status_code == 200
    assert up.bodies[0]["model"] == "other"
    assert "CHIEF PROMPT" in system_of(up.bodies[0])
    got = c.get("/v1/airlie/chat/threads/t1").json()
    assert (got["model"], got["persona"]) == ("other", "chief")


def test_a_turn_naming_a_model_or_persona_that_is_refused_is_a_400_and_makes_no_thread(
    tmp_path: Path,
) -> None:
    c, up = make(tmp_path)

    assert c.post("/v1/airlie/chat/turn", json=body(model="nope")).status_code == 400
    assert c.post("/v1/airlie/chat/turn", json=body(persona="nope")).status_code == 400

    assert up.bodies == []
    assert c.get("/v1/airlie/chat/threads/t1").status_code == 404


def test_the_system_prompt_runs_platform_then_kind_note_then_persona(tmp_path: Path) -> None:
    c, up = make(tmp_path)

    c.post("/v1/airlie/chat/turn", json=body(persona="chief"))

    text = system_of(up.bodies[0])
    assert text.index("BOOTSTRAP BODY") < text.index(PLAIN_NOTE) < text.index("CHIEF PROMPT")
    assert "CHAT PROMPT" not in text


def test_sampling_comes_from_the_persona_role(tmp_path: Path) -> None:
    c, up = make(tmp_path)

    c.post("/v1/airlie/chat/turn", json=body(persona="chief"))

    assert up.bodies[0]["max_tokens"] == 123


def test_tools_come_from_the_kind_never_from_the_persona(tmp_path: Path) -> None:
    c, up = make(tmp_path)
    c.post("/v1/airlie/chat/turn", json=body(id="a"))
    c.post("/v1/airlie/chat/turn", json=body(id="b", persona="chief"))

    chat_tools, chief_tools = tool_names(up.bodies[0]), tool_names(up.bodies[1])

    assert chat_tools
    assert chief_tools == chat_tools
    assert "shell" not in chief_tools


def test_status_reports_the_threads_model(tmp_path: Path) -> None:
    c, _ = make(tmp_path)
    t = c.post("/v1/airlie/chat/threads", json={"kind": "plain"}).json()
    c.patch(f"/v1/airlie/chat/threads/{t['id']}", json={"model": "other"})

    engine = c.get(f"/v1/airlie/chat/threads/{t['id']}/status").json()["engine"]

    assert (engine["model"], engine["label"]) == ("other", "Other model")
