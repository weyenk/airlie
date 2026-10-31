"""The chat role and its slash-command skills (AIR-36): the chat route's system prompt, the skill field, and
GET /v1/airlie/chat/skills."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from airlie.config import load_config
from airlie.serve.app import create_app
from airlie.topology.studio import specialists
from tests.unit.serve_helpers import TestClient
from tests.unit.test_serve import FakeUpstream

ROOT = Path(__file__).resolve().parents[2]
ASK = {"messages": [{"role": "user", "content": "yo"}]}


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
        "coder": "x",
        "summarizer": "x",
        "judge": "x",
        "aggregator": "x",
        "chief": "CHIEF PROMPT",
        "chat": "---\nsummary: chat\nroutable: false\n---\nCHAT PROMPT\n",
    }.items():
        (tmp_path / f"{r}.md").write_text(text)
    roles = {
        r: {"prompt": f"{r}.md", "model": "base"}
        for r in ("coder", "summarizer", "judge", "aggregator", "chief")
    }
    roles["chat"] = {"prompt": "chat.md", "model": "base", "skills": "skills"}
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://engine.test/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": roles,
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


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
    r, up = post(tmp_path, model="base", messages=[persona, user])
    assert r.status_code == 200
    assert json.loads(up.requests[0].content)["messages"] == [persona, user]
