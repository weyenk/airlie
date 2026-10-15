"""The OpenCode front door stays in step with airlie: agents name models task serve lists, nothing can merge,
the chief cannot edit, the tool calls routes that exist, the commands name tools the tool file exports."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from airlie.config import load_config
from airlie.serve.app import create_app
from airlie.story import factory
from airlie.story.state import StoryState
from tests.unit.test_fork_orchestrator import repo_with_marker_tests
from tests.unit.test_serve import FakeUpstream
from tests.unit.test_serve_jobs import finish
from tests.unit.test_story_pipeline import STORY_ROLES
from tests.unit.test_studio_orchestrator import cfg_for

ROOT = Path(__file__).resolve().parents[2]
OC = ROOT / "docs/integrations/opencode"


def front(path: Path) -> tuple[dict[str, Any], str]:
    _, meta, body = path.read_text().split("---", 2)
    return yaml.safe_load(meta), body.strip()


def tool_body(ts: str, name: str) -> str:
    """The source of `export const <name> = tool(` up to the next export."""
    return ts.split(f"export const {name} = tool(", 1)[1].split("export const", 1)[0]


def test_agents_use_models_task_serve_lists() -> None:
    cfg = load_config(ROOT / "airlie.yaml")
    served = {*cfg.roles, *cfg.models}
    agents = {p.stem: front(p)[0] for p in (OC / "agent").glob("*.md")}
    assert set(agents) == {"chief", "architect", "coder", "qa", "docs"}
    assert agents["chief"]["mode"] == "primary" and agents["chief"]["model"] == "airlie/generalist"
    for name, meta in agents.items():
        provider, model = meta["model"].split("/", 1)
        assert provider == "airlie" and model in served and meta.get("description"), name
        if name != "chief":
            assert meta["mode"] == "subagent" and model == name  # task serve supplies the role's persona


def test_no_agent_can_merge_and_the_chief_cannot_edit() -> None:
    for p in (OC / "agent").glob("*.md"):
        bash = front(p)[0]["permission"]["bash"]
        # the last matching rule wins, so the denies come last; gh api merges too (PUT .../pulls/N/merge)
        assert list(bash)[-3:] == ["gh api*", "gh pr merge*", "gh stack merge*"], p.name
        assert bash["gh api*"] == bash["gh pr merge*"] == bash["gh stack merge*"] == "deny", p.name
    chief = front(OC / "agent/chief.md")[0]["permission"]
    assert chief["edit"] == "deny" and chief["task"]["*"] == "deny"
    allowed = {k for k, v in chief["task"].items() if v == "allow"}
    assert allowed == {"architect", "coder", "qa", "docs", "explore"}
    views = [k for k, v in chief["bash"].items() if k.startswith("gh stack view") and v == "allow"]
    assert views == ["gh stack view --json*", "gh stack view --short*"]  # the bare view is interactive


def test_the_tool_calls_only_routes_serve_has() -> None:
    ts = (OC / "tool/airlie.ts").read_text()
    used = set(re.findall(r'["`](/v1/airlie/[a-z]+)', ts))
    assert used == {"/v1/airlie/plans", "/v1/airlie/runs", "/v1/airlie/jobs", "/v1/airlie/stories"}
    cfg = load_config(ROOT / "airlie.yaml")
    app = create_app(cfg, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    paths = {getattr(r, "path", "") for r in app.routes}
    for u in used:
        assert any(p == u or p.startswith(u + "/") for p in paths), u


def test_commands_run_on_the_chief_and_name_exported_tools() -> None:
    ts = (OC / "tool/airlie.ts").read_text()
    tools = {f"airlie_{m}" for m in re.findall(r"^export const (\w+) = tool\(", ts, re.M)}
    assert tools == {"airlie_plan", "airlie_run", "airlie_status", "airlie_cancel", "airlie_story"}
    for p in (OC / "command").glob("*.md"):
        meta, body = front(p)
        assert meta["agent"] == "chief" and "$ARGUMENTS" in body, p.name
        assert set(re.findall(r"airlie_\w+", body)) <= tools, p.name


def test_a_test_command_the_model_chose_needs_the_persons_ok() -> None:
    ts = (OC / "tool/airlie.ts").read_text()
    assert 'permission: "bash"' in ts
    for name in ("run", "story"):  # the tools whose test_cmd airlie runs
        body = ts.split(f"export const {name} = tool(", 1)[1].split("export const", 1)[0]
        assert body.index("await approve(args.test_cmd, ctx)") < body.index('await call("POST"'), name


def test_a_story_that_stopped_is_a_done_job_with_the_reason_in_its_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the tool's `stopped:` status line rests on: serve does not fail the job of a story that stops (is
    blocked). The job is `done` and `result.blocked` holds the reason, so the state alone says nothing."""
    repo = repo_with_marker_tests(tmp_path)
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"test_cmd": "./check.sh"}})
    reason = "layer 01 (Schema) changed nothing; its worktree is kept for inspection"

    async def stopped(c: Any, *, board: Any, card: Any, repo: Path, test_cmd: str) -> StoryState:
        path = factory.stories_dir(c) / card.key
        return StoryState(path=path, card=card.to_dict(), repo=str(repo), stage="layers", blocked=reason)

    monkeypatch.setattr(factory, "run_one", stopped)
    with TestClient(create_app(cfg, transport=httpx.MockTransport(FakeUpstream()))) as client:
        job = client.post("/v1/airlie/stories", json={"text": "avatars", "repo": str(repo)}).json()["job_id"]
        done = finish(client, job)
    assert done["state"] == "done" and done["result"]["blocked"] == reason and done["result"]["prs"] == []


def test_a_story_that_stopped_is_reported_as_stopped_not_done() -> None:
    """The status line of a done job whose result is blocked says `stopped: <reason>`; the chief and /story
    report that reason (not PR links) and how the story resumes."""
    status = tool_body((OC / "tool/airlie.ts").read_text(), "status")
    assert 'r.state === "done"' in status and "r.result?.blocked" in status and "`stopped: " in status
    for p in (OC / "agent/chief.md", OC / "command/story.md"):
        body = front(p)[1]
        assert "result.blocked" in body and "stopped" in body and "task story -- <KEY>" in body, p.name


def test_an_abort_stops_the_job_the_tool_started() -> None:
    """OpenCode aborts a tool call on Esc. airlie_plan waits for its job; airlie_run and airlie_story only
    start one. Each POSTs without the abort signal, so the job's id always comes back, and stops that job
    (best-effort; the call is reported cancelled, not unreachable) when the call was aborted meanwhile."""
    ts = (OC / "tool/airlie.ts").read_text()
    for name in ("plan", "run", "story"):
        body = tool_body(ts, name)
        assert re.search(r'await call\("POST", "/v1/airlie/\w+", body\)', body), name  # no signal
        assert f'throw await cancelled("airlie_{name}", ' in body and "ctx.abort.aborted" in body, name
    assert 'call("DELETE"' in ts.split("async function cancelled(", 1)[1].split("\n}\n", 1)[0]
