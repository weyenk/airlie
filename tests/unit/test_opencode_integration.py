"""The OpenCode front door stays in step with airlie: agents name models task serve lists, nothing can merge,
the chief cannot edit, delegates only to agents that cannot merge and asks before every shell command, the MCP
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
NO_LOCAL = ROOT / "no-local-overrides.yaml"  # never the person's own airlie.local.yaml


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


OPERATORS = r"&&|\|\||;|\||(?<![>&])&(?![&>])"  # what ends a simple command: && || ; | and a lone &
GROUPING = (  # compound-statement syntax, which holds commands without being one
    r"(?:^|(?<=[;&|(]))\s*for\s+\S+\s+in\s+[^;]*;\s*do\b"  # for x in ...; do
    r"|(?<=;)\s*done\b"  # ...; done
    r"|[()]"  # ( ... )
    r"|(?<![\w$])\{(?=\s)|(?<=\s)\}(?!\w)"  # { ...; }, but not ${x}
)
REDIRECT = re.compile(r"[0-9]*>>?&?\s*\S+")  # what is left of `) > f`, `} > f` or `done > f` once cut away


def simple_commands(command: str) -> list[str]:
    """The simple commands OpenCode's shell tool checks one by one: the call split at &&, ||, ;, |, a lone &
    (not the one in 2>&1 or &>) and $(...), each substitution checked as a command of its own (and left in
    its outer command as $SUB). A compound statement, (...), { ...; } or for ... do ... done, is no command
    but holds some: they are checked, and the redirect after it is part of none of them (2.0.29, live)."""
    inner: list[str] = []
    while sub := re.search(r"\$\(([^()]*)\)", command):
        inner += simple_commands(sub.group(1))
        command = command[: sub.start()] + "$SUB" + command[sub.end() :]
    parts = [c.strip() for c in re.split(f"{OPERATORS}|{GROUPING}", command)]
    return [c for c in parts if c and not REDIRECT.fullmatch(c)] + inner


def verdict(rules: dict[str, str], command: str) -> str:
    """OpenCode's verdict on a shell call: denied if any simple command in it is, allowed only if every one
    is, else it asks."""
    each = [decision(rules, c) for c in simple_commands(command)]
    return "deny" if "deny" in each else "allow" if all(v == "allow" for v in each) else "ask"


def test_agents_use_models_task_serve_lists() -> None:
    cfg = load_config(ROOT / "airlie.yaml", NO_LOCAL)
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
    assert front(OC / "agent/chief.md")[0]["permission"]["edit"] == "deny"


def test_only_coder_and_qa_run_a_shell_command_without_asking() -> None:
    """No allow list stays read-only. On OpenCode 2.0.29 `gh pr view *` took `gh pr view 1 > pr.txt` unasked,
    and a redirect after a compound statement ran unasked under the plain git allows: OpenCode matches each
    command inside by its own text and the redirect is part of none, so no rule can close it. An agent asks
    before every shell command, its denies aside, unless it is meant to run any (coder and qa)."""
    for p in (OC / "agent").glob("*.md"):
        bash = front(p)[0]["permission"]["bash"]
        assert list(bash)[0] == "*" and bash["*"] == ("allow" if p.stem in ("coder", "qa") else "ask"), p.name
        assert "allow" not in [v for k, v in bash.items() if k != "*"], p.name


def test_the_chief_delegates_only_to_agents_here_that_cannot_merge() -> None:
    meta, body = front(OC / "agent/chief.md")
    task = meta["permission"]["task"]
    assert list(task)[0] == "*" and task["*"] == "deny"  # first, so that each allow after it wins
    # OpenCode's own subagents: explore allows every shell command and denies no merge
    assert decision(task, "explore") == decision(task, "general") == "deny" and "explore" not in body
    # every agent the chief may hand work to, with or without asking the person
    reachable = [name for name in task if decision(task, name) in ("allow", "ask")]
    assert sorted(reachable) == ["architect", "coder", "docs", "qa"]
    for name in reachable:
        path = OC / "agent" / f"{name}.md"
        assert path.is_file(), name
        bash = front(path)[0]["permission"]["bash"]
        assert bash["gh api*"] == bash["gh pr merge*"] == bash["gh stack merge*"] == "deny", name
        assert list(bash)[-2:] == ["gh pr merge*", "gh stack merge*"], name


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # nothing runs unasked, the read-only forms included (no allow list stays read-only: see below)
        ("git status", "ask"),
        ("git status --short", "ask"),
        ("git diff", "ask"),
        ("git diff --stat", "ask"),
        ("git diff --cached", "ask"),
        ("git log --oneline -20", "ask"),
        ("gh pr view 3", "ask"),
        ("gh stack view --json", "ask"),
        ("gh stack view --short", "ask"),
        ("gh stack view", "ask"),  # interactive: it hangs a shell with no terminal
        # and so does every other form
        ("git diff HEAD~1", "ask"),
        ("git log --oneline -5", "ask"),
        ("git difftool -y -x touch", "ask"),
        ("git statusx", "ask"),
        ("git diff --out''put=F", "ask"),  # git reads --output=F: the quotes slipped past "*--output*"
        ("GIT_EXTERNAL_DIFF=x git diff", "ask"),
        # a redirect ran unasked under an allow: "gh pr view *" took `> pr.txt` (2.0.29, live)
        ("git diff > F", "ask"),
        ("gh pr view 1 > pr.txt", "ask"),
        ("gh pr view 1 >> x", "ask"),
        ("gh stack view --json > x", "ask"),
        # and so did one after a compound statement, under the plain git allows: OpenCode matched the command
        # inside and the redirect is part of none, so no rule could close it (2.0.29, live)
        ("(git status) > comp1.txt", "ask"),
        ("{ git status; } > comp2.txt", "ask"),
        ("for i in 1; do git status; done > comp3.txt", "ask"),
        # a call is as strong as its weakest simple command
        ("gh pr view 1 & touch F", "ask"),  # a lone & splits the call too (2.0.29, live)
        ("git diff; rm -rf x", "ask"),
        ("git log --oneline -20 | sh", "ask"),
        ("git status || sh -c x", "ask"),
        ("git diff $(touch F)", "ask"),
        ("git status && gh pr merge 1 --squash", "deny"),
        ("git diff --stat; gh api repos/o/r/pulls/1/merge -X PUT", "deny"),
        ("echo $(gh stack merge)", "deny"),
        # what writes or merges is denied outright
        ("git diff --output=x", "deny"),
        ("git log -p --output=x", "deny"),
        ("gh pr merge 3 --squash", "deny"),
        ("gh stack merge", "deny"),
        ("gh api -X PUT repos/o/r/pulls/3/merge", "deny"),
        # behind a redirect, or inside a compound statement, too
        ("git diff --output=x > y", "deny"),
        ("gh pr merge 3 --squash > /dev/null", "deny"),
        ("{ gh pr merge 3 --squash; } > /dev/null", "deny"),
    ],
)
def test_the_chief_shell_asks_before_every_command_and_denies_what_writes_or_merges(
    command: str, expected: str
) -> None:
    assert verdict(front(OC / "agent/chief.md")[0]["permission"]["bash"], command) == expected


def test_the_simulated_shell_tool_splits_a_call_into_simple_commands() -> None:
    assert simple_commands("a && b || c; d | e") == ["a", "b", "c", "d", "e"]
    assert simple_commands("git diff $(touch F $(rm x))") == ["git diff $SUB", "rm x", "touch F $SUB"]
    assert simple_commands("a & b") == ["a", "b"]  # a lone & backgrounds a command: the next one is its own
    assert simple_commands("a 2>&1 &> f >&2") == ["a 2>&1 &> f >&2"]  # these & are redirects, not splits
    # a compound statement holds commands without being one: they are checked, and the redirect after it is
    # part of none of them (2.0.29: `(git status) > f` was matched as `git status`)
    assert simple_commands("(git status) > f") == simple_commands("{ git status; } > f") == ["git status"]
    assert simple_commands("for i in 1; do git status; done > f") == ["git status"]
    assert simple_commands("(a) && { b; } 2>&1") == ["a", "b"]
    assert simple_commands("git diff > f") == ["git diff > f"]  # a simple command keeps its own redirect
    assert simple_commands("(git diff > f)") == ["git diff > f"]
    assert simple_commands("echo ${x} done for > f") == ["echo ${x} done for > f"]  # words, not syntax


def test_the_chief_asks_the_person_before_it_starts_a_build_or_a_story() -> None:
    # beside the server's own test_cmd check. OpenCode keys an MCP tool's permission by the tool's name,
    # airlie_<tool>, in code mode too: tools.airlie.run inside execute asked as airlie_run (2.0.29, live)
    permission = front(OC / "agent/chief.md")[0]["permission"]
    assert permission["airlie_run"] == permission["airlie_story"] == "ask"


def test_the_mcp_server_calls_only_routes_serve_has() -> None:
    src = (ROOT / "src/airlie/serve/mcp_server.py").read_text()
    used = set(re.findall(r'"(/v1/airlie/[a-z]+)', src))
    assert used == {"/v1/airlie/plans", "/v1/airlie/runs", "/v1/airlie/jobs", "/v1/airlie/stories"}
    cfg = load_config(ROOT / "airlie.yaml", NO_LOCAL)
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


CODE_MODE_CALL = re.compile(r"tools\.airlie\.(\w+)\(\{([^{}]*)\}\)")


def code_mode_problems(body: str, schemas: dict[str, dict[str, Any]]) -> list[str]:
    """What a prompt gets wrong about reaching airlie's tools in OpenCode 2.0, where they are reachable only
    inside the execute tool's JavaScript, as `tools.airlie.<tool>({argument, ...})`: no word on the execute
    tool, a call to a tool the server does not list, an argument its schema lacks, a required one left out,
    or an airlie_<tool> name used without that call form."""
    problems = [] if "the execute tool" in " ".join(body.split()) else ["it never names the execute tool"]
    reached: set[str] = set()
    for tool, args in CODE_MODE_CALL.findall(body):
        reached.add(tool)
        if tool not in schemas:
            problems.append(f"tools.airlie.{tool}: the server lists no such tool")
            continue
        given = {a.strip() for a in args.split(",") if a.strip()}
        known, required = set(schemas[tool]["properties"]), set(schemas[tool].get("required", []))
        problems += [f"tools.airlie.{tool}: {a} is no argument of it" for a in sorted(given - known)]
        problems += [f"tools.airlie.{tool}: {a} is required" for a in sorted(required - given)]
    named = {name.removeprefix("airlie_") for name in re.findall(r"airlie_\w+", body)}
    return problems + [
        f"airlie_{t} is named, but not reached as tools.airlie.{t}(...)" for t in sorted(named - reached)
    ]


async def test_the_chief_and_the_commands_reach_airlie_through_code_mode_with_the_schemas_names() -> None:
    """OpenCode 2.0 shows the model its own tools and one `execute` tool; airlie's MCP tools are reachable
    only inside its JavaScript (asked for `airlie_run` by name, the generalist said it had no such tool:
    2.0.29, live). So the chief and both commands say how, with the tool and argument names of the schemas
    that `airlie mcp` lists."""
    async with airlie_http({}) as http:
        listed = await build_server(http, test_cmds=[], cwd=ROOT).list_tools()
    schemas = {t.name: t.input_schema for t in listed}
    for p in (OC / "agent/chief.md", OC / "command/studio.md", OC / "command/story.md"):
        assert code_mode_problems(front(p)[1], schemas) == [], p.name
    chief_reaches = {tool for tool, _ in CODE_MODE_CALL.findall(front(OC / "agent/chief.md")[1])}
    assert chief_reaches == set(schemas) == {"plan", "run", "status", "cancel", "story"}


def test_the_code_mode_check_flags_what_a_prompt_can_get_wrong() -> None:
    schemas = {"status": {"properties": {"job_id": {}, "wait": {}}, "required": ["job_id"]}}
    good = "Use the execute tool: `await tools.airlie.status({job_id})` (airlie_status)."
    assert code_mode_problems(good, schemas) == []
    assert code_mode_problems(good.replace("the execute tool", "a tool"), schemas) == [
        "it never names the execute tool"
    ]
    assert code_mode_problems(good.replace("{job_id}", "{jobId, wait}"), schemas) == [
        "tools.airlie.status: jobId is no argument of it",
        "tools.airlie.status: job_id is required",
    ]
    assert code_mode_problems(good.replace("status({", "poll({"), schemas) == [
        "tools.airlie.poll: the server lists no such tool",
        "airlie_status is named, but not reached as tools.airlie.status(...)",
    ]
    assert code_mode_problems("Use the execute tool, then follow it with airlie_status.", schemas) == [
        "airlie_status is named, but not reached as tools.airlie.status(...)"
    ]


def test_the_chief_and_studio_follow_a_plan_still_being_made_with_airlie_status() -> None:
    """airlie_plan answers within 45 s (an MCP client may give a call only 60); a longer planning comes back
    as a job id, and airlie_status shows the plan when it ends."""
    for p in (OC / "agent/chief.md", OC / "command/studio.md"):
        body = " ".join(front(p)[1].split())
        assert "if it says it is still planning, call airlie_status with its job id" in body, p.name


def test_opencode_gets_the_tools_from_airlie_mcp_not_from_a_tool_file() -> None:
    assert not (OC / "tool").exists()  # OpenCode 2.0 runs no tool files
    cmds = yaml.safe_load((ROOT / "Taskfile.yml").read_text())["tasks"]["opencode:install"]["cmds"]
    assert not any(c.startswith("cp") and "tool" in c for c in cmds)
    assert "{{.RUN}} airlie mcp config --install" in cmds
    sample = json.loads((ROOT / "docs/integrations/opencode.json").read_text())
    entry = sample["mcp"]["airlie"]
    assert entry == opencode_entry(Path(entry["command"][4]))  # what install writes; the checkout is yours


def test_the_samples_default_model_is_the_chiefs_because_opencode_run_ignores_an_agents_own() -> None:
    """`opencode run --agent chief` ran the chief on the config's default model, not its own `model:` (2.0.29,
    live), and on the coder model it degenerated: the sample's default is the chief's, and the sample lists
    it. Agents with a `model:` of their own keep it (test_agents_use_models_task_serve_lists)."""
    sample = json.loads((ROOT / "docs/integrations/opencode.json").read_text())
    provider, model = sample["model"].split("/", 1)
    assert sample["model"] == front(OC / "agent/chief.md")[0]["model"]
    assert model in sample["provider"][provider]["models"]


def thinking_room() -> tuple[Any, dict[str, int]]:
    """The studio config (whose kv size the samples' contexts assume) and, for each role a sample may list and
    each raw model those roles run on, the max_tokens a thinking one needs: the room airlie.yaml gives it."""
    cfg = load_config(ROOT / "airlie.yaml", NO_LOCAL, topology="studio")
    thinking = {n: r for n, r in cfg.roles.items() if r.thinking and r.thinking_budget is not None}
    wanted = {n: r.max_tokens for n, r in thinking.items()}
    for model in {r.model for r in thinking.values()}:
        wanted[model] = max(r.max_tokens for r in thinking.values() if r.model == model)
    return cfg, wanted


def test_the_samples_thinking_models_have_room_for_a_reply_after_their_thinking() -> None:
    """OpenCode sends a model's limit.output as max_tokens through task serve, and max_tokens caps the
    thinking and the reply together (HDS-3, 2026-10-15): a thinking model needs the room airlie.yaml gives its
    role, and the sample's output must stay below its context."""
    _, wanted = thinking_room()
    sample = json.loads((ROOT / "docs/integrations/opencode.json").read_text())
    models = sample["provider"]["airlie"]["models"]
    listed = {
        n: tokens for n, tokens in wanted.items() if n in models
    }  # the roles and models the sample lists
    assert listed == {"architect": 16384, "research": 16384, "generalist": 16384}
    for name, tokens in listed.items():
        limit = models[name]["limit"]
        assert tokens <= limit["output"] < limit["context"], name


def test_the_vscode_samples_thinking_models_have_room_for_a_reply_after_their_thinking() -> None:
    """The same for VS Code's chat: maxOutputTokens is its max_tokens, and what it may send in
    (maxInputTokens) plus what it may ask for must fit the kv window the engine keeps."""
    cfg, wanted = thinking_room()
    groups = json.loads((ROOT / "docs/integrations/vscode-chatLanguageModels.json").read_text())
    listed = {m["id"]: m for group in groups for m in group["models"]}
    thinking = {n for n, m in listed.items() if m["thinking"]}
    assert thinking == {"generalist", "architect"} == {n for n in listed if n in wanted}
    window = cfg.engines["main"].max_kv_size
    for name in thinking:
        m = listed[name]
        assert wanted[name] <= m["maxOutputTokens"] < m["maxInputTokens"], name
        assert m["maxInputTokens"] + m["maxOutputTokens"] <= window, name


def test_a_story_that_stopped_is_a_done_job_with_the_reason_in_its_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What airlie_status's `stopped:` line rests on: serve does not fail the job of a story that stops (is
    blocked). The job is `done` and `result.blocked` holds the reason, so the state alone says nothing."""
    repo = repo_with_marker_tests(tmp_path)
    cfg = cfg_for(tmp_path, roles=STORY_ROLES)
    reason = "layer 01 (Schema) changed nothing; its worktree is kept for inspection"

    async def stopped(
        c: Any, *, board: Any, card: Any, repo: Path | None = None, test_cmd: str | None = None
    ) -> StoryState:
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
