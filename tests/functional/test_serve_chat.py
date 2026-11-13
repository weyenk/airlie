"""The chat role and its slash-command skills (AIR-36): the chat route's system prompt, the skill field, and
GET /v1/airlie/chat/skills."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from airlie.config import load_config
from airlie.serve.app import create_app
from airlie.topology.studio import specialists
from tests.support.serve import FakeUpstream
from tests.support.serve_chat import chat_cfg
from tests.unit.serve_helpers import TestClient

ROOT = Path(__file__).resolve().parents[2]
ASK = {"messages": [{"role": "user", "content": "yo"}]}


def post(tmp_path: Path, **body: object):
    up = FakeUpstream()
    r = TestClient(create_app(chat_cfg(tmp_path), transport=httpx.MockTransport(up))).post(
        "/v1/chat/completions", json={**ASK, **body}
    )
    return r, up


def system_of(up: FakeUpstream) -> str:
    return json.loads(up.requests[0].content)["messages"][0]["content"]


def test_the_chat_role_reaches_the_engine_with_its_prompt_and_the_skills_list(tmp_path: Path) -> None:
    r, up = post(tmp_path, model="chat")
    assert r.status_code == 200
    system = system_of(up)
    assert system.startswith("CHAT PROMPT") and "- /story: Make a story" in system
    assert "STORY BODY" not in system  # a skill's body is for the request that runs it


def test_a_skill_adds_its_body_to_that_request_and_the_field_is_not_forwarded(tmp_path: Path) -> None:
    r, up = post(tmp_path, model="chat", skill="story")
    assert r.status_code == 200
    sent = json.loads(up.requests[0].content)
    assert "skill" not in sent
    assert "CHAT PROMPT" in sent["messages"][0]["content"] and "STORY BODY" in sent["messages"][0]["content"]
    assert sent["messages"][1:] == ASK["messages"]


def test_an_unknown_skill_is_404_with_the_known_names(tmp_path: Path) -> None:
    r, up = post(tmp_path, model="chat", skill="plan")
    assert r.status_code == 404 and "plan" in r.json()["detail"] and "story" in r.json()["detail"]
    assert up.requests == []


def test_a_skill_on_another_model_is_refused(tmp_path: Path) -> None:
    r, up = post(tmp_path, model="chief", skill="story")
    assert r.status_code == 400 and up.requests == []


def test_the_chief_and_a_raw_model_are_byte_for_byte_unchanged(tmp_path: Path) -> None:
    """The same requests, with and without a chat role in the config: what reaches the engine is identical."""
    plain = tmp_path / "plain"
    plain.mkdir()
    seen = {}
    for label, root in (("with", tmp_path), ("without", plain)):
        cfg = chat_cfg(root)
        if label == "without":
            del cfg.roles["chat"]
        up = FakeUpstream()
        client = TestClient(create_app(cfg, transport=httpx.MockTransport(up)))
        for model in ("chief", "base"):
            assert client.post("/v1/chat/completions", json={"model": model, **ASK}).status_code == 200
        seen[label] = [r.content for r in up.requests]
    assert seen["with"] == seen["without"]
    chief = json.loads(seen["with"][0])
    assert chief["messages"][0] == {"role": "system", "content": "CHIEF PROMPT"}
    assert json.loads(seen["with"][1])["messages"] == ASK["messages"]


def test_the_skills_endpoint_lists_name_summary_and_action(tmp_path: Path) -> None:
    app = create_app(
        chat_cfg(
            tmp_path,
            {"story": "---\nsummary: S\naction: story\n---\nb\n", "tip": "---\nsummary: T\n---\nb\n"},
        )
    )
    r = TestClient(app).get("/v1/airlie/chat/skills")
    assert r.status_code == 200
    assert r.json()["skills"] == [
        {"name": "story", "summary": "S", "action": "story"},
        {"name": "tip", "summary": "T", "action": ""},
    ]


def test_a_skill_without_a_summary_is_refused_at_load_naming_the_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"nosummary\.md"):
        create_app(chat_cfg(tmp_path, {"nosummary": "just a body\n"}))


def test_the_shipped_config_serves_story_and_does_not_route_to_chat() -> None:
    cfg = load_config(ROOT / "airlie.yaml", ROOT / "no-local-overrides.yaml")
    assert "chat" not in specialists(cfg)
    app = create_app(cfg, transport=httpx.MockTransport(FakeUpstream()), api_key="k")
    skills = TestClient(app).get("/v1/airlie/chat/skills", headers={"Authorization": "Bearer k"})
    assert [s["name"] for s in skills.json()["skills"]] == [
        p.stem for p in sorted((ROOT / "skills/chat").glob("*.md"))
    ]
    by_name = {s["name"]: s for s in skills.json()["skills"]}
    assert by_name["story"]["action"] == "story" and by_name["clear"]["action"] == "clear"


def test_the_initiative_skill_loads_with_its_action() -> None:
    cfg = load_config(ROOT / "airlie.yaml", ROOT / "no-local-overrides.yaml")
    app = create_app(cfg, transport=httpx.MockTransport(FakeUpstream()), api_key="k")
    skills = TestClient(app).get("/v1/airlie/chat/skills", headers={"Authorization": "Bearer k"}).json()
    by_name = {s["name"]: s for s in skills["skills"]}
    assert by_name["initiative"]["action"] == "initiative"
    assert "Turn a loose ask into an initiative" in by_name["initiative"]["summary"]
    body = (ROOT / "skills/chat/initiative.md").read_text()
    assert "nothing starts until they do" in body and "Repositories: owner/name" in body


def test_the_why_skill_loads_with_its_action() -> None:
    cfg = load_config(ROOT / "airlie.yaml", ROOT / "no-local-overrides.yaml")
    app = create_app(cfg, transport=httpx.MockTransport(FakeUpstream()), api_key="k")
    skills = TestClient(app).get("/v1/airlie/chat/skills", headers={"Authorization": "Bearer k"})
    why = {s["name"]: s for s in skills.json()["skills"]}["why"]
    assert why["action"] == "why" and "diagnosis" in why["summary"]
    text = (ROOT / "skills/chat/why.md").read_text()
    assert "--- diagnosis (data) ---" in text  # the markers the Vue app wraps the route's Markdown in


def test_raw_catalog_model_gets_only_the_clients_system_message(tmp_path: Path) -> None:
    persona = {"role": "system", "content": "PERSONA ONLY"}
    user = {"role": "user", "content": "go"}
    fields = {  # what the bridge's llm kind sets: a raw model has no role defaults to fill them in
        "max_tokens": 1234,
        "chat_template_kwargs": {"enable_thinking": True},
        "thinking_token_budget": 567,
    }
    r, up = post(tmp_path, model="base", messages=[persona, user], **fields)
    assert r.status_code == 200
    sent = json.loads(up.requests[0].content)
    assert sent["messages"] == [persona, user]
    assert {k: sent[k] for k in fields} == fields


def test_a_non_streamed_chat_completion_waits_900_s_for_the_engine(tmp_path: Path) -> None:
    """A non-streamed reply sends no bytes until it is whole: a long thinking reply outlasts the client's
    600 s default (the bridge's llm jobs wait 900 s)."""
    r, up = post(tmp_path, model="base")
    assert r.status_code == 200
    assert up.requests[0].extensions["timeout"]["read"] == 900.0


def test_a_streamed_chat_completion_waits_900_s_for_the_engine(tmp_path: Path) -> None:
    """OpenCode and other clients stream: the upstream read must match the non-streamed 900 s cap."""
    up = FakeUpstream()
    with TestClient(create_app(chat_cfg(tmp_path), transport=httpx.MockTransport(up))).stream(
        "POST", "/v1/chat/completions", json={**ASK, "model": "base", "stream": True}
    ) as r:
        assert r.status_code == 200
        b"".join(r.iter_bytes())
    assert up.requests[0].extensions["timeout"]["read"] == 900.0
