"""A chat's model and persona: new chats are seeded with the chat role and its default model (or the first
allowed one), `GET /v1/airlie/chat/options` lists what may be picked, and `PATCH .../threads/{id}` changes a
chat's pick after checking it against the allowlist and the configured roles. Live model ids are faked."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from airlie.auth.principal import SESSION_COOKIE
from airlie.auth.store import AuthStore
from airlie.config.schema import ModelSpec
from airlie.models.allowlist import ModelAllowlist
from airlie.models.live import LiveModelsError
from airlie.serve.app import create_app
from tests.support.serve_chat import ScriptedUpstream, chat_cfg
from tests.unit.serve_helpers import TestClient

ELSEWHERE = ("10.0.0.5", 40000)


def make(tmp_path: Path, live: list[str] | Exception, off: tuple[str, ...] = ()) -> TestClient:
    """A client off the Mac signed in as Pat, over a server whose engine serves `live` and whose allowlist
    has `off` switched off."""
    allow = ModelAllowlist(tmp_path / "state" / "models.sqlite")
    for model in off:
        allow.set_allowed(model, False)
    allow.close()

    async def live_ids() -> list[str]:
        if isinstance(live, Exception):
            raise live
        return list(live)

    conf = chat_cfg(tmp_path)
    conf.models["base"] = ModelSpec(label="Base model")
    store = AuthStore(tmp_path / "auth.sqlite")
    pat = store.add_user("Pat", "member")
    app = create_app(
        conf,
        transport=httpx.MockTransport(ScriptedUpstream()),
        auth_store=store,
        model_live_ids=live_ids,
    )
    cookie = {"Cookie": f"{SESSION_COOKIE}={store.make_session(pat.id, 30)}"}
    return TestClient(app, client=ELSEWHERE, headers=cookie)


def new_thread(c: TestClient) -> dict:
    return c.post("/v1/airlie/chat/threads", json={"kind": "plain"}).json()


def test_new_thread_seeds_chat_persona_and_the_roles_default_model(tmp_path: Path) -> None:
    c = make(tmp_path, ["other", "base"])

    t = new_thread(c)

    assert t["persona"] == "chat"
    assert t["model"] == "base"  # the chat role's model, though it is not first among the live ones


def test_new_thread_takes_the_first_allowed_model_when_the_default_is_switched_off(tmp_path: Path) -> None:
    c = make(tmp_path, ["base", "other", "third"], off=("base", "other"))

    assert new_thread(c)["model"] == "third"


def test_new_thread_takes_the_roles_default_when_the_engine_cannot_be_asked(tmp_path: Path) -> None:
    c = make(tmp_path, LiveModelsError("engine down"))

    assert new_thread(c)["model"] == "base"


def test_the_thread_and_its_status_and_list_carry_model_and_persona(tmp_path: Path) -> None:
    c = make(tmp_path, ["base"])
    t = new_thread(c)

    got = c.get(f"/v1/airlie/chat/threads/{t['id']}").json()
    status = c.get(f"/v1/airlie/chat/threads/{t['id']}/status").json()
    listed = c.get("/v1/airlie/chat/threads").json()

    assert (got["model"], got["persona"]) == ("base", "chat")
    assert (status["model"], status["persona"]) == ("base", "chat")
    assert (listed[0]["model"], listed[0]["persona"]) == ("base", "chat")


def test_options_lists_allowed_models_with_labels_and_the_roles(tmp_path: Path) -> None:
    c = make(tmp_path, ["base", "other", "drop"], off=("drop",))

    body = c.get("/v1/airlie/chat/options").json()

    assert body["models"] == [{"id": "base", "label": "Base model"}, {"id": "other", "label": "other"}]
    assert {"id": "chat", "label": "chat"} in body["personas"]
    assert {p["id"] for p in body["personas"]} == {
        "architect",
        "summarizer",
        "judge",
        "aggregator",
        "chief",
        "chat",
    }


def test_options_needs_a_signed_in_person(tmp_path: Path) -> None:
    c = make(tmp_path, ["base"])
    c.headers.pop("Cookie")

    assert c.get("/v1/airlie/chat/options").status_code == 401


def test_patch_changes_model_and_persona_and_keeps_them(tmp_path: Path) -> None:
    c = make(tmp_path, ["base", "other"])
    t = new_thread(c)

    r = c.patch(f"/v1/airlie/chat/threads/{t['id']}", json={"model": "other", "persona": "judge"})

    assert r.status_code == 200
    assert (r.json()["model"], r.json()["persona"]) == ("other", "judge")
    got = c.get(f"/v1/airlie/chat/threads/{t['id']}").json()
    assert (got["model"], got["persona"]) == ("other", "judge")


def test_patch_one_field_leaves_the_other(tmp_path: Path) -> None:
    c = make(tmp_path, ["base", "other"])
    t = new_thread(c)

    r = c.patch(f"/v1/airlie/chat/threads/{t['id']}", json={"persona": "judge"})

    assert (r.json()["model"], r.json()["persona"]) == ("base", "judge")


def test_patch_refuses_a_model_that_is_not_allowed(tmp_path: Path) -> None:
    c = make(tmp_path, ["base", "drop"], off=("drop",))
    t = new_thread(c)

    for model in ("not-allowed", "drop"):
        r = c.patch(f"/v1/airlie/chat/threads/{t['id']}", json={"model": model})
        assert r.status_code == 400
        assert model in r.json()["error"]
    assert c.get(f"/v1/airlie/chat/threads/{t['id']}").json()["model"] == "base"


def test_patch_refuses_an_unknown_persona(tmp_path: Path) -> None:
    c = make(tmp_path, ["base"])
    t = new_thread(c)

    r = c.patch(f"/v1/airlie/chat/threads/{t['id']}", json={"persona": "nobody"})

    assert r.status_code == 400
    assert c.get(f"/v1/airlie/chat/threads/{t['id']}").json()["persona"] == "chat"


@pytest.mark.parametrize("body", [{}, {"model": ""}, {"extra": 1}])
def test_patch_refuses_a_body_that_changes_nothing_or_is_malformed(tmp_path: Path, body: dict) -> None:
    c = make(tmp_path, ["base"])
    t = new_thread(c)

    assert c.patch(f"/v1/airlie/chat/threads/{t['id']}", json=body).status_code == 422


def test_patch_of_a_thread_that_is_not_there_is_404(tmp_path: Path) -> None:
    c = make(tmp_path, ["base"])

    assert c.patch("/v1/airlie/chat/threads/nope", json={"persona": "chat"}).status_code == 404


def test_patch_while_a_turn_runs_is_409(tmp_path: Path) -> None:
    c = make(tmp_path, ["base"])
    t = new_thread(c)
    c.app.state.chat_busy.take(t["id"])  # type: ignore[attr-defined]

    assert c.patch(f"/v1/airlie/chat/threads/{t['id']}", json={"persona": "chat"}).status_code == 409


def test_patch_is_the_owners_alone(tmp_path: Path) -> None:
    c = make(tmp_path, ["base"])
    t = new_thread(c)
    other = AuthStore(tmp_path / "auth.sqlite")
    sam = other.add_user("Sam", "member")
    c.headers["Cookie"] = f"{SESSION_COOKIE}={other.make_session(sam.id, 30)}"

    assert c.patch(f"/v1/airlie/chat/threads/{t['id']}", json={"persona": "judge"}).status_code == 404


def test_patch_hints_the_live_feed(tmp_path: Path) -> None:
    c = make(tmp_path, ["base"])
    t = new_thread(c)
    seen: list[tuple[str, str | None]] = []
    feed = c.app.state.feed  # type: ignore[attr-defined]
    publish = feed.publish
    feed.publish = lambda topic, key=None: (seen.append((topic, key)), publish(topic, key))[1]

    c.patch(f"/v1/airlie/chat/threads/{t['id']}", json={"persona": "judge"})

    assert ("chat", None) in seen


def turn(thread_id: str, **extra: object) -> dict:
    msg = {"id": "u1", "role": "user", "parts": [{"type": "text", "text": "hello"}]}
    return {"id": thread_id, "kind": "plain", "messages": [msg], **extra}


def test_turn_refuses_a_saved_model_that_was_switched_off_since(tmp_path: Path) -> None:
    c = make(tmp_path, ["base", "other"])
    t = new_thread(c)
    assert c.patch(f"/v1/airlie/chat/threads/{t['id']}", json={"model": "other"}).status_code == 200
    allow = ModelAllowlist(tmp_path / "state" / "models.sqlite")
    allow.set_allowed("other", False)
    allow.close()

    r = c.post("/v1/airlie/chat/turn", json=turn(t["id"]))

    assert r.status_code == 400
    assert "other" in r.json()["error"]
    assert c.get(f"/v1/airlie/chat/threads/{t['id']}").json()["model"] == "other"  # kept until a new pick
    assert c.get(f"/v1/airlie/chat/threads/{t['id']}").json()["messages"] == []
    assert c.patch(f"/v1/airlie/chat/threads/{t['id']}", json={"model": "base"}).status_code == 200
    assert c.post("/v1/airlie/chat/turn", json=turn(t["id"])).status_code == 200


def test_turn_with_a_new_allowed_pick_runs_though_the_saved_model_was_switched_off(tmp_path: Path) -> None:
    c = make(tmp_path, ["base", "other"])
    t = new_thread(c)
    c.patch(f"/v1/airlie/chat/threads/{t['id']}", json={"model": "other"})
    allow = ModelAllowlist(tmp_path / "state" / "models.sqlite")
    allow.set_allowed("other", False)
    allow.close()

    r = c.post("/v1/airlie/chat/turn", json=turn(t["id"], model="base"))

    assert r.status_code == 200
