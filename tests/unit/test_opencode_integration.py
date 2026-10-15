"""The OpenCode front door stays in step with airlie: agents name models task serve lists, nothing can merge,
the chief cannot edit, delegates only to agents that cannot merge and runs only read-only git and gh, the MCP
server calls routes that exist, and the commands name tools the server lists."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from airlie.config import load_config
from airlie.serve.app import create_app
from airlie.serve.mcp_server import airlie_http, build_server, opencode_entry
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


def opencode_glob(pattern: str) -> re.Pattern[str]:
    """A permission pattern as OpenCode 2.0 compiles it: anchored, dotAll, `*` is `.*`, `?` is `.`, and a
    trailing " *" may be left out ("git diff *" is git diff with or without arguments, not git difftool)."""
    tail = pattern.endswith(" *")
    body = pattern[:-2] if tail else pattern
    rx = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in body)
    return re.compile(f"^{rx}{'( .*)?' if tail else ''}$", re.DOTALL)


def decision(rules: dict[str, str], subject: str) -> str:
    """OpenCode's verdict on a command (or a subagent): the last rule that matches wins, and none asks."""
    hits = [verdict for pattern, verdict in rules.items() if opencode_glob(pattern).match(subject)]
    return hits[-1] if hits else "ask"


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
    assert chief["edit"] == "deny"
    views = [k for k, v in chief["bash"].items() if k.startswith("gh stack view") and v == "allow"]
    assert views == ["gh stack view --json*", "gh stack view --short*"]  # the bare view is interactive


def test_the_chief_delegates_only_to_agents_here_that_cannot_merge() -> None:
    meta, body = front(OC / "agent/chief.md")
    task = meta["permission"]["task"]
    assert list(task)[0] == "*" and task["*"] == "deny"  # first, so that each allow after it wins
    # OpenCode's own subagents: explore allows every shell command and denies no merge
    assert decision(task, "explore") == decision(task, "general") == "deny" and "explore" not in body
    allowed = [name for name in task if decision(task, name) == "allow"]
    assert sorted(allowed) == ["architect", "coder", "docs", "qa"]
    for name in allowed:
        path = OC / "agent" / f"{name}.md"
        assert path.is_file(), name
        bash = front(path)[0]["permission"]["bash"]
        assert bash["gh api*"] == bash["gh pr merge*"] == bash["gh stack merge*"] == "deny", name
        assert list(bash)[-2:] == ["gh pr merge*", "gh stack merge*"], name


@pytest.mark.parametrize(
    ("command", "verdict"),
    [
        ("git status", "allow"),
        ("git status --short", "allow"),
        ("git diff", "allow"),
        ("git diff HEAD~1", "allow"),
        ("git log --oneline -5", "allow"),
        ("gh pr view 3", "allow"),
        ("gh stack view --json", "allow"),
        ("git difftool -y -x touch", "ask"),  # "git diff *" is word-bounded: not git difftool
        ("git statusx", "ask"),
        ("gh stack view", "ask"),  # interactive: it hangs a shell with no terminal
        ("git diff --output=x", "deny"),  # writes a file
        ("git diff HEAD --output x", "deny"),
        ("git log -p --output=x", "deny"),
        ("gh pr merge 3 --squash", "deny"),
        ("gh stack merge", "deny"),
        ("gh api -X PUT repos/o/r/pulls/3/merge", "deny"),
    ],
)
def test_the_chief_shell_runs_read_only_git_and_gh_and_nothing_that_writes_or_merges(
    command: str, verdict: str
) -> None:
    assert decision(front(OC / "agent/chief.md")[0]["permission"]["bash"], command) == verdict


def test_the_chief_asks_the_person_before_it_starts_a_build_or_a_story() -> None:
    # beside the server's own test_cmd check. How OpenCode keys an MCP tool's permission is a best guess
    # (the tool's name, airlie_<tool>), checked live in Task 15
    permission = front(OC / "agent/chief.md")[0]["permission"]
    assert permission["airlie_run"] == permission["airlie_story"] == "ask"


def test_the_mcp_server_calls_only_routes_serve_has() -> None:
    src = (ROOT / "src/airlie/serve/mcp_server.py").read_text()
    used = set(re.findall(r'"(/v1/airlie/[a-z]+)', src))
    assert used == {"/v1/airlie/plans", "/v1/airlie/runs", "/v1/airlie/jobs", "/v1/airlie/stories"}
    cfg = load_config(ROOT / "airlie.yaml")
    app = create_app(cfg, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    paths = {getattr(r, "path", "") for r in app.routes}
    for u in used:
        assert any(p == u or p.startswith(u + "/") for p in paths), u


async def test_the_commands_and_the_chief_name_only_tools_the_mcp_server_lists() -> None:
    async with airlie_http({}) as http:
        listed = await build_server(http, test_cmds=[], cwd=ROOT).list_tools()
    tools = {f"airlie_{t.name}" for t in listed}  # OpenCode names an MCP tool <server>_<tool>
    assert tools == {"airlie_plan", "airlie_run", "airlie_status", "airlie_cancel", "airlie_story"}
    for p in (OC / "command").glob("*.md"):
        meta, body = front(p)
        assert meta["agent"] == "chief" and "$ARGUMENTS" in body, p.name
        assert set(re.findall(r"airlie_\w+", body)) <= tools, p.name
    assert set(re.findall(r"airlie_\w+", front(OC / "agent/chief.md")[1])) <= tools


def test_opencode_gets_the_tools_from_airlie_mcp_not_from_a_tool_file() -> None:
    assert not (OC / "tool").exists()  # OpenCode 2.0 runs no tool files
    cmds = yaml.safe_load((ROOT / "Taskfile.yml").read_text())["tasks"]["opencode:install"]["cmds"]
    assert not any(c.startswith("cp") and "tool" in c for c in cmds)
    assert "{{.RUN}} airlie mcp config --install" in cmds
    sample = json.loads((ROOT / "docs/integrations/opencode.json").read_text())
    entry = sample["mcp"]["airlie"]
    assert entry == opencode_entry(Path(entry["command"][4]))  # what install writes; the checkout is yours


def test_a_story_that_stopped_is_a_done_job_with_the_reason_in_its_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What airlie_status's `stopped:` line rests on: serve does not fail the job of a story that stops (is
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


def test_the_chief_and_story_report_a_story_that_stopped_and_how_it_resumes() -> None:
    """airlie_status shows a done job whose result is blocked as `stopped: <reason>` (test_mcp_server); the
    chief and /story report that reason, not PR links, and how the story resumes."""
    for p in (OC / "agent/chief.md", OC / "command/story.md"):
        body = front(p)[1]
        assert "result.blocked" in body and "stopped" in body and "task story -- <KEY>" in body, p.name
