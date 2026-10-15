"""The OpenCode front door stays in step with airlie: agents name models task serve lists, nothing can merge,
the chief cannot edit, the tool calls routes that exist, the commands name tools the tool file exports."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import yaml

from airlie.config import load_config
from airlie.serve.app import create_app

ROOT = Path(__file__).resolve().parents[2]
OC = ROOT / "docs/integrations/opencode"


def front(path: Path) -> tuple[dict[str, Any], str]:
    _, meta, body = path.read_text().split("---", 2)
    return yaml.safe_load(meta), body.strip()


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
        assert list(bash)[-2:] == ["gh pr merge*", "gh stack merge*"], p.name  # the last matching rule wins
        assert bash["gh pr merge*"] == bash["gh stack merge*"] == "deny", p.name
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
