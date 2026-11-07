"""The workflows API the builder talks to (docs/superpowers/specs/2026-11-07-workflow-builder-design.md):
the repo's workflows and the board's drafts listed and read, drafts saved, deleted, validated and published,
and the catalog of registry ids the builder's dropdowns offer."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI

from airlie.board import BoardStore
from airlie.board.model import WorkflowDraft
from airlie.prompts.hub import load
from airlie.serve import app as app_module
from airlie.serve.app import create_app
from airlie.serve.workflows_api import workflows_router
from airlie.story.flow import CATALOG
from airlie.story.publish import CannotStart, Publisher
from airlie.story.workflow_publish import NothingToPublish
from tests.unit.serve_helpers import TestClient
from tests.unit.test_story_pipeline import WORKFLOWS
from tests.unit.test_story_workflows import MINI, MINI_EACH, MINI_NOTE, NOTE, TINY
from tests.unit.test_studio_orchestrator import cfg_for

KEY = "k"
AUTH = {"Authorization": f"Bearer {KEY}"}
BROKEN = "workflows: {broken: {start: x, nodes: {}}}\n"
PR = "https://github.com/o/r/pull/7"


def cfg(tmp_path: Path):  # type: ignore[no-untyped-def]
    """The shipped story and layer workflows, as airlie.yaml includes them."""
    workflows = {
        name: spec
        for part in ("story", "layer")
        for name, spec in yaml.safe_load((WORKFLOWS / f"{part}.yaml").read_text())["workflows"].items()
    }
    return cfg_for(tmp_path, extra={"workflows": workflows})


def client(tmp_path: Path) -> tuple[TestClient, Any]:
    c = cfg(tmp_path)
    return TestClient(create_app(c, api_key=KEY)), c


def store(c: Any) -> BoardStore:
    return BoardStore(c.observability.state_dir.expanduser() / "board.sqlite", prefix=c.board.prefix)


def bare(c: Any, s: BoardStore, publisher: Any = None) -> tuple[TestClient, list[tuple[str, str | None]]]:
    """The router alone over `s`, with what it tells the feed recorded."""
    seen: list[tuple[str, str | None]] = []

    async def unwired(draft: WorkflowDraft) -> tuple[int, str]:
        raise AssertionError("not published here")

    app = FastAPI()
    app.include_router(
        workflows_router(c, s, lambda topic, key: seen.append((topic, key)), publisher or unwired)
    )
    return TestClient(app), seen


def test_the_list_shows_the_repo_workflows_and_the_drafts(tmp_path: Path) -> None:
    http, c = client(tmp_path)
    store(c).save_draft("mini", type="bug", yaml=MINI)
    store(c).save_draft("broken", type=None, yaml=BROKEN)
    with http:
        r = http.get("/v1/workflows", headers=AUTH)
    assert r.status_code == 200
    rows = {w["name"]: w for w in r.json()["workflows"]}
    assert rows["story"] == {
        "name": "story", "type": "story", "source": "file", "bundle": False, "problems": [], "pr": None
    }  # fmt: skip
    assert rows["layer"]["bundle"] is True and rows["layer"]["type"] is None
    assert (
        rows["mini"]["source"] == "draft" and rows["mini"]["type"] == "bug" and rows["mini"]["problems"] == []
    )
    assert rows["broken"]["problems"] == ["workflow broken: start names unknown node 'x'"]


def test_a_draft_saved_is_read_back_with_its_prompts_and_told_to_the_feed(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    http, seen = bare(c, store(c))
    with http:
        put = http.put(
            "/v1/workflows/drafts/mini", json={"type": "bug", "yaml": MINI, "prompts": {"mini-note": NOTE}}
        )
        got = http.get("/v1/workflows/mini")
    assert put.status_code == 200
    assert put.json() == got.json()
    assert got.json() == {
        "name": "mini",
        "type": "bug",
        "source": "draft",
        "yaml": MINI,
        "layout": {},
        "problems": [],
        "prompts": {"mini-note": NOTE},
        "pr": None,
    }
    assert seen == [("workflows", "mini")]


def test_a_draft_naming_its_own_prompt_file_has_no_problems_and_one_naming_none_has_one(
    tmp_path: Path,
) -> None:
    c = cfg(tmp_path)
    http, _ = bare(c, store(c))
    unknown = "workflow mini: node note: unknown prompt file 'mini-note'"
    with http:
        own = http.put(
            "/v1/workflows/drafts/mini",
            json={"type": "bug", "yaml": MINI_NOTE, "prompts": {"mini-note": NOTE}},
        )
        checked = http.post("/v1/workflows/validate", json={"yaml": MINI_NOTE}).json()  # the saved draft's
        sent = http.post("/v1/workflows/validate", json={"yaml": MINI_NOTE, "prompts": {}}).json()
        listed = {w["name"]: w for w in http.get("/v1/workflows").json()["workflows"]}
        none = http.put("/v1/workflows/drafts/mini", json={"type": "bug", "yaml": MINI_NOTE})
    assert own.status_code == 200 and own.json()["problems"] == []
    assert checked == {"problems": []} and sent == {"problems": [unknown]}
    assert listed["mini"]["problems"] == []
    assert none.status_code == 200 and none.json()["problems"] == [unknown]


def test_a_draft_prompt_file_named_like_a_hub_prompt_is_a_problem(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    http, _ = bare(c, store(c))
    clash = {"brief-survey-task": NOTE}
    taken = "workflow mini: prompt file 'brief-survey-task' is already a hub prompt"
    with http:
        put = http.put("/v1/workflows/drafts/mini", json={"type": "bug", "yaml": MINI, "prompts": clash})
        checked = http.post("/v1/workflows/validate", json={"yaml": MINI, "prompts": clash}).json()
    assert put.status_code == 200 and put.json()["problems"] == [taken]
    assert checked == {"problems": [taken]}


def test_a_draft_with_problems_is_saved_and_returned_with_them(tmp_path: Path) -> None:
    http, c = client(tmp_path)
    with http:
        put = http.put("/v1/workflows/drafts/mini", json={"type": "bug", "yaml": MINI_EACH}, headers=AUTH)
    assert put.status_code == 200
    assert put.json()["problems"] and "tiny" in put.json()["problems"][0]
    assert store(c).draft("mini").yaml == MINI_EACH


def test_a_file_workflow_reads_as_its_file_text_else_its_spec(tmp_path: Path) -> None:
    http, c = client(tmp_path)
    shipped = (WORKFLOWS / "story.yaml").read_text()
    own = Path(c.source_path).parent / "config" / "workflows"
    own.mkdir(parents=True)
    (own / "story.yaml").write_text(shipped)
    with http:
        story = http.get("/v1/workflows/story", headers=AUTH).json()
        layer = http.get("/v1/workflows/layer", headers=AUTH).json()
        missing = http.get("/v1/workflows/nope", headers=AUTH)
    assert story["yaml"] == shipped and story["source"] == "file" and story["type"] == "story"
    assert story["prompts"] == {} and story["problems"] == [] and story["pr"] is None
    dumped = yaml.safe_load(layer["yaml"])["workflows"]
    assert list(dumped) == ["layer"] and dumped["layer"]["start"] == c.workflows["layer"].start
    assert missing.status_code == 404


def test_a_draft_may_not_take_a_file_workflows_name(tmp_path: Path) -> None:
    http, c = client(tmp_path)
    story = MINI.replace("mini:", "story:")
    with http:
        r = http.put("/v1/workflows/drafts/story", json={"type": "story", "yaml": story}, headers=AUTH)
    assert r.status_code == 409
    assert store(c).drafts() == []


def test_a_draft_may_not_take_a_name_the_routes_use(tmp_path: Path) -> None:
    http, c = client(tmp_path)
    with http:
        for name in ("catalog", "validate", "drafts"):
            text = MINI.replace("mini:", f"{name}:")
            put = http.put(f"/v1/workflows/drafts/{name}", json={"type": "bug", "yaml": text}, headers=AUTH)
            checked = http.post("/v1/workflows/validate", json={"yaml": text}, headers=AUTH)
            assert put.status_code == 422 and put.json()["detail"] == f"{name!r} is reserved"
            assert checked.status_code == 422 and checked.json()["detail"] == f"{name!r} is reserved"
    assert store(c).drafts() == []


def test_a_draft_that_is_not_yaml_or_names_another_workflow_is_refused(tmp_path: Path) -> None:
    http, c = client(tmp_path)
    with http:
        bad = http.put("/v1/workflows/drafts/mini", json={"type": None, "yaml": "a: [1,"}, headers=AUTH)
        other = http.put("/v1/workflows/drafts/other", json={"type": None, "yaml": MINI}, headers=AUTH)
        flat = http.put(
            "/v1/workflows/drafts/mini", json={"type": None, "yaml": "workflows: {mini: 3}"}, headers=AUTH
        )
    assert (bad.status_code, other.status_code, flat.status_code) == (422, 422, 422)
    assert store(c).drafts() == []


def test_validate_returns_the_check_problem_and_saves_nothing(tmp_path: Path) -> None:
    http, c = client(tmp_path)
    store(c).save_draft("tiny", type=None, yaml=TINY)
    bad = MINI.replace("start: intake", "start: nowhere")
    with http:
        problems = http.post("/v1/workflows/validate", json={"yaml": bad}, headers=AUTH).json()
        bundled = http.post("/v1/workflows/validate", json={"yaml": MINI_EACH}, headers=AUTH).json()
        broken = http.post("/v1/workflows/validate", json={"yaml": "a: [1,"}, headers=AUTH)
    assert problems == {"problems": ["workflow mini: start names unknown node 'nowhere'"]}
    assert bundled == {"problems": []}  # the draft bundle it names resolves
    assert broken.status_code == 200 and len(broken.json()["problems"]) == 1
    assert [d.name for d in store(c).drafts()] == ["tiny"]


def test_a_draft_is_deleted(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    store(c).save_draft("mini", type="bug", yaml=MINI)
    http, seen = bare(c, store(c))
    with http:
        gone = http.delete("/v1/workflows/drafts/mini")
        again = http.delete("/v1/workflows/drafts/mini")
        read = http.get("/v1/workflows/mini")
    assert (gone.status_code, again.status_code, read.status_code) == (204, 404, 404)
    assert seen == [("workflows", "mini")]


def test_serve_refuses_what_it_cannot_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(*args: object, **kw: object) -> tuple[int, str]:
        raise AssertionError("refused before anything is published")

    monkeypatch.setattr(app_module, "publish_workflow", never)
    http, c = client(tmp_path)
    store(c).save_draft("broken", type=None, yaml=BROKEN)
    with http:
        none = http.post("/v1/workflows/nope/publish", headers=AUTH)
        broken = http.post("/v1/workflows/broken/publish", headers=AUTH)
    assert none.status_code == 404
    assert broken.status_code == 422 and "unknown node 'x'" in str(broken.json())


def test_serve_publishes_through_the_story_publisher_from_its_own_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[Any, Any, str, Any]] = []

    async def published(
        cfg: Any, publisher: Publisher, draft: WorkflowDraft, *, home: Path
    ) -> tuple[int, str]:
        sent.append((cfg, publisher, draft.name, home))
        return 7, PR

    monkeypatch.setattr(app_module, "publish_workflow", published)
    monkeypatch.setattr(app_module, "home_checkout", lambda: tmp_path / "home")
    http, c = client(tmp_path)
    store(c).save_draft("mini", type="bug", yaml=MINI)
    with http:
        r = http.post("/v1/workflows/mini/publish", headers=AUTH)
    assert r.status_code == 200 and r.json() == {"number": 7, "url": PR}
    [(cfg_, publisher, name, home)] = sent
    assert cfg_ is c and name == "mini" and home == tmp_path / "home"
    assert (publisher.publish, publisher.remote, publisher.app) == ("single", c.story.remote, None)
    assert store(c).draft("mini").published_pr == 7


def test_serve_without_its_own_checkout_cannot_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "home_checkout", lambda: None)
    http, c = client(tmp_path)
    store(c).save_draft("mini", type="bug", yaml=MINI)
    with http:
        r = http.post("/v1/workflows/mini/publish", headers=AUTH)
    assert r.status_code == 503 and "checkout" in r.json()["detail"]
    assert store(c).draft("mini").published_pr is None


def test_a_publish_that_fails_is_a_bad_gateway_and_a_vanished_draft_a_404(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    s = store(c)
    s.save_draft("mini", type="bug", yaml=MINI)
    s.save_draft("gone", type="bug", yaml=MINI.replace("  mini:", "  gone:"))

    s.save_draft("same", type="bug", yaml=MINI.replace("  mini:", "  same:"))

    async def publisher(draft: WorkflowDraft) -> tuple[int, str]:
        if draft.name == "mini":
            raise CannotStart("`gh pr create` could not start: no gh")
        if draft.name == "same":
            raise NothingToPublish("nothing to publish: main already holds it")
        s.delete_draft(draft.name)  # deleted while its PR was being opened
        return 7, PR

    http, seen = bare(c, s, publisher)
    with http:
        failed = http.post("/v1/workflows/mini/publish")
        same = http.post("/v1/workflows/same/publish")
        vanished = http.post("/v1/workflows/gone/publish")
    assert failed.status_code == 502 and failed.json()["detail"] == "`gh pr create` could not start: no gh"
    assert same.status_code == 409 and same.json()["detail"] == "nothing to publish: main already holds it"
    assert vanished.status_code == 404 and vanished.json()["detail"] == "draft vanished while publishing"
    assert s.draft("mini").published_pr is None and seen == []


async def test_publishes_of_one_workflow_run_one_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    running: dict[str, int] = {}
    most: dict[str, int] = {}

    async def published(
        cfg: Any, publisher: Publisher, draft: WorkflowDraft, *, home: Path
    ) -> tuple[int, str]:
        running[draft.name] = running.get(draft.name, 0) + 1
        most[draft.name] = max(most.get(draft.name, 0), running[draft.name])
        most["all"] = max(most.get("all", 0), sum(n for k, n in running.items()))
        await asyncio.sleep(0.02)
        running[draft.name] -= 1
        return 7, PR

    monkeypatch.setattr(app_module, "publish_workflow", published)
    monkeypatch.setattr(app_module, "home_checkout", lambda: tmp_path)
    publish = app_module._workflow_publisher(cfg(tmp_path))
    one, two = (draft_named("mini"), draft_named("other"))
    await asyncio.gather(publish(one), publish(one), publish(two), publish(two), publish(one))
    # one at a time per workflow (they share a worktree and a branch); different workflows side by side
    assert most == {"mini": 1, "other": 1, "all": 2}


def draft_named(name: str) -> WorkflowDraft:
    return WorkflowDraft(
        name=name, type="bug", yaml=MINI, prompts={}, created_at=0.0, updated_at=0.0,
        published_pr=None, published_url=None,
    )  # fmt: skip


def test_publishing_a_draft_records_its_pr(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    s = store(c)
    s.save_draft("mini", type="bug", yaml=MINI)
    sent: list[str] = []

    async def publisher(draft: WorkflowDraft) -> tuple[int, str]:
        sent.append(draft.name)
        return 7, PR

    http, seen = bare(c, s, publisher)
    with http:
        r = http.post("/v1/workflows/mini/publish")
        listed = {w["name"]: w for w in http.get("/v1/workflows").json()["workflows"]}
        read = http.get("/v1/workflows/mini").json()
    assert r.status_code == 200 and r.json() == {"number": 7, "url": PR}
    assert sent == ["mini"] and seen == [("workflows", "mini")]
    assert listed["mini"]["pr"] == read["pr"] == {"number": 7, "url": PR}
    assert s.draft("mini").published_pr == 7


def test_the_catalog_lists_roles_types_registry_ids_and_rule_files(tmp_path: Path) -> None:
    http, c = client(tmp_path)
    with http:
        got = http.get("/v1/workflows/catalog", headers=AUTH).json()
    assert got["roles"] == sorted(c.roles)
    assert got["types"] == ["ask", "conversation", "write", "meeting", "check", "gate", "parallel", "each"]
    assert "story.refine" in got["prompts"]
    assert {k: got[k] for k in CATALOG} == {k: list(v) for k, v in CATALOG.items()}
    assert got["prompt_files"] == sorted(p.id for p in load().values() if p.kind == "rule")
    assert got["prompt_files"]


def test_the_workflows_api_needs_a_principal(tmp_path: Path) -> None:
    http, _ = client(tmp_path)
    with http:
        assert http.get("/v1/workflows", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert http.get("/v1/workflows/catalog", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_a_jira_board_has_no_drafts_to_serve(tmp_path: Path) -> None:
    c = cfg_for(tmp_path, extra={"story": {"board": "jira"}})
    with TestClient(create_app(c, api_key=KEY)) as http:
        assert http.get("/v1/workflows", headers=AUTH).status_code == 404
