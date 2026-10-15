"""`airlie mcp`: the studio and the story pipeline as MCP tools, each a thin client of task serve's jobs API.
The tools are driven over MCP against the real create_app in-process. A test command the model chose is
passed on only if the person configured it, and a call the client cancels stops the job it started."""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import anyio.lowlevel
import httpx
import pytest
from mcp import StdioServerParameters
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError
from typer.testing import CliRunner

from airlie.cli import app as cli
from airlie.engine import FakeEngine, FakeReply
from airlie.serve import mcp_server
from airlie.serve.app import create_app
from airlie.serve.mcp_server import (
    InstallError,
    airlie_http,
    allowed_test_cmds,
    build_server,
    install_opencode,
    opencode_entry,
)
from airlie.story import factory
from airlie.story.state import StoryState
from tests.unit.test_fork_orchestrator import repo_with_marker_tests
from tests.unit.test_serve import FakeUpstream
from tests.unit.test_serve_jobs import app_for
from tests.unit.test_story_pipeline import STORY_ROLES
from tests.unit.test_studio_orchestrator import APPROVE, DONE, PLAN, RUN, SYNTH, WRITE_PASS, cfg_for

ROOT = Path(__file__).resolve().parents[2]
ONE = {"subtasks": [{"id": "b", "role": "coder", "brief": "write PASS"}]}
runner = CliRunner()


class Hang(FakeEngine):
    """An engine that never answers: a job on it runs until it is cancelled."""

    async def generate(self, req, on_token=None):  # type: ignore[no-untyped-def]
        await anyio.sleep(3600)


class Gate(FakeEngine):
    """Answers as scripted, but each answer waits for `open`: planning that takes as long as the test says."""

    def __init__(self, scripts: dict[str, list[FakeReply]]) -> None:
        super().__init__(scripts)
        self.open = anyio.Event()

    async def generate(self, req, on_token=None):  # type: ignore[no-untyped-def]
        await self.open.wait()
        return await super().generate(req, on_token)


class Serve(httpx.AsyncBaseTransport):
    """The real app behind an ASGI transport. Records each request (method, path, JSON body), each answer
    (method, path, status) and the ids of the jobs POSTs started; with `hold` set, the answer to a POST that
    started a job is held back until that event is set. Each request yields to the event loop first, as one
    sent over a socket does: there a cancelled call is interrupted unless it is shielded."""

    def __init__(self, app: Any) -> None:
        self.inner = httpx.ASGITransport(app=app)
        self.sent: list[tuple[str, str, Any]] = []
        self.answered: list[tuple[str, str, int]] = []
        self.jobs: list[str] = []
        self.posted = anyio.Event()
        self.hold: anyio.Event | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.sent.append((request.method, request.url.path, json.loads(request.content or b"null")))
        await anyio.lowlevel.checkpoint()
        res = await self.inner.handle_async_request(request)
        self.answered.append((request.method, request.url.path, res.status_code))
        if request.method == "POST" and res.status_code == 202:
            self.jobs.append(json.loads(await res.aread())["job_id"])
            self.posted.set()
            if self.hold is not None:
                await self.hold.wait()
        return res


@asynccontextmanager
async def connected(
    transport: httpx.AsyncBaseTransport,
    cwd: Path,
    test_cmds: Sequence[str] = (),
    env: dict[str, str] | None = None,
) -> AsyncIterator[Client]:
    """An MCP client of the airlie server, over JSON-RPC as a stdio client talks to it; the server's tools
    reach serve through `transport`."""
    async with (
        airlie_http(env or {}, transport=transport) as http,
        Client(build_server(http, test_cmds=test_cmds, cwd=cwd), mode="legacy") as client,
    ):
        yield client


def text(result: Any) -> str:
    return "\n".join(c.text for c in result.content)


def started(out: str) -> str:
    """The job id in what run or story answered."""
    assert out.startswith("Started ") and out.endswith(". Follow it with airlie_status."), out
    return out.removeprefix("Started ").removesuffix(". Follow it with airlie_status.")


async def until_ended(client: Client, job: str) -> list[str]:
    """Every status answer for `job` until its state is no longer running; a job that never ends fails the
    test within a minute instead of holding up the suite."""
    outs: list[str] = []
    with anyio.fail_after(60):
        while not outs or outs[-1].startswith(f"{job}: running"):
            outs.append(text(await client.call_tool("status", {"job_id": job, "wait": 10})))
    return outs


def shape(schema: dict[str, Any]) -> dict[str, str]:
    """Each argument as its JSON type, `?` when it may be null (left out), `=<default>` for a default. Every
    argument is described, and the required ones are exactly those with neither."""
    out: dict[str, str] = {}
    for name, prop in schema["properties"].items():
        assert prop.get("description"), name
        types = [t["type"] for t in prop["anyOf"]] if "anyOf" in prop else [prop["type"]]
        out[name] = types[0] + ("?" if types[1:] == ["null"] else "")
        if prop.get("default") is not None:
            out[name] += f"={json.dumps(prop['default'])}"
    assert set(schema.get("required", [])) == {n for n, s in out.items() if s.isalpha()}
    return out


async def test_the_server_lists_five_tools_with_these_arguments(tmp_path: Path) -> None:
    async with connected(httpx.MockTransport(lambda r: httpx.Response(500)), tmp_path) as client:
        tools = (await client.list_tools()).tools
    assert all(t.description for t in tools)
    assert {t.name: shape(t.input_schema) for t in tools} == {
        "plan": {"goal": "string", "test_cmd": "string?", "repo": "string?"},
        "run": {"goal": "string", "plan": "object?", "test_cmd": "string?", "repo": "string?"},
        "status": {"job_id": "string", "wait": "number=40.0"},
        "cancel": {"job_id": "string"},
        "story": {"card": "string?", "text": "string?", "repo": "string?"},  # its gate is AGENTS.md's
    }


async def test_a_plan_comes_back_as_a_table_and_json_and_the_approved_plan_builds_on_a_branch(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine({"chief": [PLAN, SYNTH], "coder": [WRITE_PASS, RUN, DONE], "reviewer": [APPROVE]})
    serve = Serve(app_for(tmp_path, engine))
    async with connected(serve, repo, test_cmds=["./check.sh"]) as client:
        out = text(await client.call_tool("plan", {"goal": "fix the build", "test_cmd": "./check.sh"}))
        assert "| fix | coder | make the check pass | - |\n| note | docs | describe the repo | fix |" in out
        assert "\n\nAssumptions:\n- a1\n" in out
        plan = json.loads(out.split("Plan JSON for airlie_run:\n", 1)[1])
        plan["subtasks"] = plan["subtasks"][:1]  # the person drops the docs subtask
        # as a model copies it: the JSON text, which the server reads as the plan object
        args = {"goal": "fix the build", "plan": json.dumps(plan), "test_cmd": "./check.sh"}
        job = started(text(await client.call_tool("run", args)))
        outs = await until_ended(client, job)
    assert outs[-1].startswith(f"{job}: done\n")
    result = json.loads(outs[-1].split("\nResult:\n", 1)[1])
    assert result["ok"] is True and result["tests"] == {"studio": True}
    assert result["branch"].startswith("airlie/")
    log = "\n".join(outs)
    assert log.count("\n- run_start") == 1 and log.count("\n- run_done") == 1  # no event is shown twice
    where = str(repo.resolve())
    assert [(path, body) for method, path, body in serve.sent if method == "POST"] == [
        ("/v1/airlie/plans", {"goal": "fix the build", "repo": where, "test_cmd": "./check.sh"}),
        ("/v1/airlie/runs", {"goal": "fix the build", "repo": where, "test_cmd": "./check.sh", "plan": plan}),
    ]


async def test_planning_that_outlasts_the_window_hands_back_its_job_and_status_shows_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_server, "CALL_BUDGET_S", 3.0)  # 45 s in use: inside a client's 60 s per call
    repo = repo_with_marker_tests(tmp_path)
    answer = FakeReply(text=json.dumps({"type": "answer", "answer": "it is 42"}))
    engine = Gate({"chief": [PLAN, answer]})
    async with connected(Serve(app_for(tmp_path, engine)), repo) as client:
        with anyio.fail_after(10):  # the window ends the call, not the planning
            out = text(await client.call_tool("plan", {"goal": "fix the build"}))
        job, _, rest = out.partition(": ")
        assert rest.startswith("still planning. Call airlie_status with this job id"), out
        engine.open.set()  # the chief answers now
        done = (await until_ended(client, job))[-1]
        assert done.startswith(f"{job}: done\n") and "| fix | coder | make the check pass | - |" in done
        plan = json.loads(done.split("\nPlan JSON for airlie_run:\n", 1)[1])  # what airlie_run takes
        assert [s["id"] for s in plan["subtasks"]] == ["fix", "note"] and "Result:" not in done
        engine.open = anyio.Event()
        job, _, rest = text(await client.call_tool("plan", {"goal": "what is 6 x 7?"})).partition(": ")
        assert rest.startswith("still planning")
        engine.open.set()
        done = (await until_ended(client, job))[-1]
    assert done.startswith(f"{job}: done\n")
    assert done.endswith("\n\nThe chief answered without a plan:\n\nit is 42")


async def test_status_reports_a_story_that_stopped_as_stopped_with_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    cfg = cfg_for(tmp_path, roles=STORY_ROLES)
    reason = "layer 01 (Schema) changed nothing;\n  its worktree is kept for inspection"

    async def stopped(
        c: Any, *, board: Any, card: Any, repo: Path | None = None, test_cmd: str | None = None
    ) -> StoryState:
        path = factory.stories_dir(c) / card.key
        return StoryState(path=path, card=card.to_dict(), repo=str(repo), stage="layers", blocked=reason)

    monkeypatch.setattr(factory, "run_one", stopped)
    serve = Serve(create_app(cfg, transport=httpx.MockTransport(FakeUpstream())))
    async with connected(serve, repo) as client:
        job = started(text(await client.call_tool("story", {"text": "avatars"})))
        out = (await until_ended(client, job))[-1]
    one_line = "layer 01 (Schema) changed nothing; its worktree is kept for inspection"
    assert out.splitlines()[0] == f"{job}: stopped: {one_line}"
    assert json.loads(out.split("\nResult:\n", 1)[1])["blocked"] == reason
    # no test_cmd: a story's gate is its repository's AGENTS.md
    assert serve.sent[0] == ("POST", "/v1/airlie/stories", {"text": "avatars", "repo": str(repo.resolve())})


async def test_cancel_stops_a_running_job_and_says_when_one_was_not_running(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    async with connected(Serve(app_for(tmp_path, Hang({}))), repo) as client:
        job = started(text(await client.call_tool("run", {"goal": "g", "plan": ONE})))
        assert text(await client.call_tool("status", {"job_id": job, "wait": 0})).startswith(
            f"{job}: running"
        )
        assert text(await client.call_tool("cancel", {"job_id": job})) == f"Cancelled {job}."
        out = text(await client.call_tool("status", {"job_id": job, "wait": 5}))
        assert out.startswith(f"{job}: cancelled")
        assert text(await client.call_tool("cancel", {"job_id": job})) == f"{job} was not running."
        ghost = await client.call_tool("cancel", {"job_id": "ghost"})
    assert ghost.is_error and "airlie DELETE /v1/airlie/jobs/ghost: 404" in text(ghost)


async def test_a_plan_call_the_client_cancels_stops_the_job_it_was_following(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    app = app_for(tmp_path, Hang({}))
    serve = Serve(app)
    async with connected(serve, repo) as client:
        async with anyio.create_task_group() as tg:
            tg.start_soon(client.call_tool, "plan", {"goal": "g"})
            with anyio.fail_after(10):
                while not any(method == "GET" for method, _, _ in serve.sent):  # it follows its job
                    await anyio.sleep(0.01)
            tg.cancel_scope.cancel()  # Esc: the client cancels the call (notifications/cancelled)
        stop = ("DELETE", f"/v1/airlie/jobs/{serve.jobs[0]}", 200)
        with anyio.fail_after(10):
            while stop not in serve.answered:
                await anyio.sleep(0.01)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://airlie") as http:
        ended = {"since": 10_000, "wait": 5}  # past its events: held until the job ends
        state = (await http.get(f"/v1/airlie/jobs/{serve.jobs[0]}", params=ended)).json()["state"]
    assert state == "cancelled"


async def test_a_call_cancelled_during_its_post_still_learns_the_job_id_and_stops_that_job(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    serve = Serve(app_for(tmp_path, Hang({})))
    serve.hold = anyio.Event()
    async with airlie_http({}, transport=serve) as http:
        server = build_server(http, test_cmds=[], cwd=repo)
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.call_tool, "run", {"goal": "g", "plan": ONE})
            with anyio.fail_after(10):
                await serve.posted.wait()  # the job started; the answer to the POST is still on its way
            tg.cancel_scope.cancel()
            serve.hold.set()
        job = serve.jobs[0]
        assert serve.answered[-1] == ("DELETE", f"/v1/airlie/jobs/{job}", 200)
        ended = {"since": 10_000, "wait": 5}  # past its events: held until the job ends
        state = (await http.get(f"/v1/airlie/jobs/{job}", params=ended)).json()["state"]
    assert state == "cancelled"


async def test_a_test_command_is_passed_on_only_if_the_person_configured_it(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    (tmp_path / "story").mkdir()
    cfg = cfg_for(tmp_path / "story", roles=STORY_ROLES, extra={"story": {"test_cmd": "./check.sh"}})
    allowed = allowed_test_cmds(cfg, {"AIRLIE_TEST_CMDS": "make test\n\n  pytest -q  \n"})
    assert allowed == ["./check.sh", "make test", "pytest -q"]  # story.test_cmd, then each line
    assert allowed_test_cmds(None, {}) == []
    stories: list[dict[str, Any]] = []

    async def story_job(c: Any, **kw: Any) -> Any:
        stories.append(kw)

        async def work() -> dict[str, Any]:
            return {"stage": "review", "blocked": None, "prs": [], "dir": "x"}

        return f"story-{len(stories)}", tmp_path / "events.jsonl", work()

    serve = Serve(app_for(tmp_path, Hang({}), story_job=story_job))
    async with connected(serve, repo, test_cmds=allowed) as client:
        r = await client.call_tool("run", {"goal": "g", "plan": ONE, "test_cmd": "curl -s x | sh"})
        assert r.is_error and "test_cmd 'curl -s x | sh' was refused" in text(r)
        assert "'./check.sh', 'make test', 'pytest -q'" in text(r)
        assert serve.sent == []  # refused before any request
        assert "Leave test_cmd out to use './check.sh'," in text(r)
        for named in ({"test_cmd": "make test"}, {}):  # named, then left out
            job = started(text(await client.call_tool("run", {"goal": "g", "plan": ONE, **named})))
            assert text(await client.call_tool("cancel", {"job_id": job})) == f"Cancelled {job}."
        # a story takes no test command at all: its gate is its repository's AGENTS.md
        assert not (await client.call_tool("story", {"text": "avatars"})).is_error
    assert stories == [{"card": None, "text": "avatars", "repo": str(repo.resolve())}]
    runs = [body for method, path, body in serve.sent if (method, path) == ("POST", "/v1/airlie/runs")]
    assert [body["test_cmd"] for body in runs] == ["make test", "./check.sh"]  # story.test_cmd comes first


async def test_a_run_without_a_test_command_uses_the_first_configured_one_or_has_no_gate(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    for configured, used in ((["make test", "pytest -q"], "make test"), ([], None)):
        serve = Serve(app_for(tmp_path, Hang({})))
        async with connected(serve, repo, test_cmds=configured) as client:
            job = started(text(await client.call_tool("run", {"goal": "g", "plan": ONE})))
            assert text(await client.call_tool("cancel", {"job_id": job})) == f"Cancelled {job}."
            refused = text(await client.call_tool("run", {"goal": "g", "test_cmd": "rm -rf x"}))
        assert serve.sent[0][2].get("test_cmd") == used, configured
        gate = "Leave test_cmd out to run with no test gate (none is configured)," in refused
        assert gate is (used is None) and ("Leave test_cmd out to use 'make test'," in refused) is (
            used is not None
        )


async def test_the_api_key_goes_only_to_an_https_or_loopback_airlie_and_nowhere_else(tmp_path: Path) -> None:
    key = {"AIRLIE_SERVE_API_KEY": "sekret"}
    for url, sent in (
        (None, True),
        ("http://localhost:8090", True),
        ("http://[::1]:8090/", True),
        ("https://mac.tail0123.ts.net", True),
        ("http://192.168.1.20:8090", False),  # in clear text across a network
        ("http://mac.local:8090", False),
    ):
        async with airlie_http({**key, "AIRLIE_URL": url} if url else key) as http:
            assert http.headers.get("authorization") == ("Bearer sekret" if sent else None), url
    app = app_for(tmp_path, FakeEngine({}), api_key="sekret")
    withheld = {**key, "AIRLIE_URL": "http://192.168.1.20:8090"}
    for env, answer in ((key, ": 404 "), ({}, ": 401 "), (withheld, ": 401 ")):
        async with connected(httpx.ASGITransport(app=app), tmp_path, env=env) as client:
            out = text(await client.call_tool("status", {"job_id": "ghost", "wait": 0}))
        assert answer in out, env
        assert ("AIRLIE_SERVE_API_KEY" in out) is (answer == ": 401 "), env  # a 401 says why
    hops: list[str] = []

    def elsewhere(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://elsewhere.example/steal"})

    async with connected(httpx.MockTransport(elsewhere), tmp_path, env=key) as client:
        r = await client.call_tool("cancel", {"job_id": "x"})
    assert r.is_error and ": 307" in text(r) and hops == ["http://127.0.0.1:8090/v1/airlie/jobs/x"]


async def test_status_waits_40_seconds_by_default_and_never_past_the_45_a_call_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # an MCP client may give a call only 60 s (the TypeScript SDK's default)
    asked: list[float] = []

    def serve(request: httpx.Request) -> httpx.Response:
        asked.append(float(request.url.params["wait"]))
        running = {"job_id": "j", "kind": "run", "state": "running", "events": [], "next": 0}
        return httpx.Response(200, json=running)

    async with connected(httpx.MockTransport(serve), tmp_path) as client:
        for args in ({}, {"wait": 9999}, {"wait": -5}, {"wait": 30}):
            await client.call_tool("status", {"job_id": "j", **args})
    assert asked[0] == 40 and asked[1] == pytest.approx(43, abs=0.1) and asked[2:] == [0, 30]
    monkeypatch.setattr(mcp_server, "CALL_BUDGET_S", 10.0)  # a shorter call: serve must answer within it
    async with connected(httpx.MockTransport(serve), tmp_path) as client:
        await client.call_tool("status", {"job_id": "j", "wait": 30})
    assert asked[-1] == pytest.approx(10 - 0.5 - 1.5, abs=0.1)  # the time left, less the margins


class Hung(httpx.AsyncBaseTransport):
    """A serve that never answers; with `starts`, a POST that starts a job is answered (job j1) first."""

    def __init__(self, starts: bool) -> None:
        self.starts = starts

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.starts and request.method == "POST":
            return httpx.Response(202, json={"job_id": "j1", "kind": "plan"})
        await anyio.sleep(3600)
        raise AssertionError("a hung serve never answers")


@pytest.mark.parametrize(
    ("tool", "args", "starts", "says"),
    [
        ("plan", {"goal": "g"}, False, "did not answer POST /v1/airlie/plans in time"),
        ("plan", {"goal": "g"}, True, "j1: airlie did not answer in time"),  # with its job id
        ("run", {"goal": "g"}, False, "did not answer POST /v1/airlie/runs in time"),
        ("story", {"text": "t"}, False, "did not answer POST /v1/airlie/stories in time"),
        ("status", {"job_id": "j1", "wait": 40}, False, "j1: airlie did not answer in time"),
        ("cancel", {"job_id": "j1"}, False, "j1: airlie did not answer the cancel in time"),
    ],
)
async def test_each_tool_answers_within_the_call_budget_when_serve_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, args: dict[str, Any], starts: bool, says: str
) -> None:
    monkeypatch.setattr(mcp_server, "CALL_BUDGET_S", 1.0)  # 45 s in use: the same arithmetic, scaled down
    repo = repo_with_marker_tests(tmp_path)
    async with connected(Hung(starts), repo) as client:
        t0 = anyio.current_time()
        with anyio.fail_after(10):  # a tool that waits on the hung serve fails here, not after its hour
            out = text(await client.call_tool(tool, args))
        took = anyio.current_time() - t0
    assert says in out and took < 1.0 + 0.25, (took, out)


async def test_an_airlie_that_is_not_running_is_reported_with_how_to_start_it(tmp_path: Path) -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    env = {"AIRLIE_URL": "http://127.0.0.1:8091/"}
    async with connected(httpx.MockTransport(refused), tmp_path, env=env) as client:
        r = await client.call_tool("status", {"job_id": "x"})
    assert (
        r.is_error
        and "airlie is not reachable at http://127.0.0.1:8091; start it with `task serve`" in text(r)
    )


async def test_the_repository_is_the_work_tree_airlie_mcp_runs_in_unless_repo_names_one(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    (repo / "src").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    bodies: list[dict[str, Any]] = []

    def serve(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(202, json={"job_id": "j", "kind": "story"})

    async with airlie_http({}, transport=httpx.MockTransport(serve)) as http:
        inside = build_server(http, test_cmds=[], cwd=repo / "src")
        outside = build_server(http, test_cmds=[], cwd=elsewhere)
        # a blank argument is a missing one: models fill optional arguments with ""
        await inside.call_tool("story", {"text": "a", "card": "", "repo": ""})
        await outside.call_tool("story", {"text": "a", "repo": "../repo/src"})  # from where airlie mcp runs
        await outside.call_tool("story", {"text": "a"})  # no repository here: serve uses story.repo
        # an issue's repository is its repo: label: the one airlie mcp runs in is not sent in its place
        await inside.call_tool("story", {"card": "AIR-7", "repo": " "})
        await inside.call_tool("story", {"card": "AIR-7", "repo": "."})  # unless one is named
        for server, args in ((outside, {"goal": "g"}), (inside, {"goal": "g", "repo": str(elsewhere)})):
            with pytest.raises(ToolError, match="is in no git repository; pass repo"):
                await server.call_tool("run", args)
        for args in ({"card": "AIR-7", "text": "a"}, {"card": " ", "text": ""}):
            with pytest.raises(ToolError, match="pass exactly one of card or text"):
                await inside.call_tool("story", args)
    where = str(repo.resolve())
    assert bodies == [
        {"text": "a", "repo": where},
        {"text": "a", "repo": where},
        {"text": "a"},
        {"card": "AIR-7"},
        {"card": "AIR-7", "repo": where},
    ]


def test_install_adds_airlie_to_opencodes_config_after_a_backup_and_only_when_it_changes(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".config/opencode/opencode.json"
    project = tmp_path / "airlie"
    entry = opencode_entry(project)
    assert entry == {
        "type": "local",
        "command": [
            *("uv", "run", "--quiet", "--project", str(project), "airlie", "mcp"),
            *("--config", str(project / "airlie.yaml")),  # airlie's own config, never a project's
        ],
        "environment": {"AIRLIE_URL": "http://127.0.0.1:8090"},
    }
    assert install_opencode(path, project).startswith("created ")  # no config yet
    assert json.loads(path.read_text()) == {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {"airlie": entry},
    }
    assert list(path.parent.glob("*.bak")) == []
    mine = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {"airlie": {"name": "airlie Ü", "options": {"apiKey": "k"}}},
        "mcp": {"docs": {"type": "remote", "url": "https://mcp.example"}},
        "model": "airlie/coder",
    }
    raw = json.dumps(mine)
    path.write_text(raw)
    path.chmod(0o640)  # whatever the person set is kept (a new file beside it would be 0600)
    assert install_opencode(path, project).startswith("added ")
    merged = path.read_text(encoding="utf-8")
    assert json.loads(merged) == {**mine, "mcp": {**mine["mcp"], "airlie": entry}} and "Ü" in merged
    (backup,) = path.parent.glob("opencode.json.*.bak")
    assert backup.read_text() == raw
    assert stat.S_IMODE(backup.stat().st_mode) == stat.S_IMODE(path.stat().st_mode) == 0o640
    assert "unchanged" in install_opencode(path, project)  # nothing to change: no write, no backup
    assert path.read_text(encoding="utf-8") == merged and len(list(path.parent.glob("*.bak"))) == 1
    assert [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_install_merges_into_the_entry_the_person_has_and_keeps_what_they_set(tmp_path: Path) -> None:
    path = tmp_path / "opencode.json"
    project = tmp_path / "airlie"
    command = opencode_entry(project)["command"]
    theirs = {
        **opencode_entry(tmp_path / "old-checkout"),
        "environment": {
            "AIRLIE_URL": "http://127.0.0.1:9000",
            "AIRLIE_TEST_CMDS": "make test",
            "AIRLIE_SERVE_API_KEY": "k",
        },
        "enabled": False,
        "timeout": 120000,
    }
    path.write_text(json.dumps({"mcp": {"airlie": theirs}}))
    assert install_opencode(path, project).startswith("updated ")
    assert json.loads(path.read_text())["mcp"]["airlie"] == {**theirs, "command": command}  # only the command
    assert "unchanged" in install_opencode(path, project)
    bare = {
        "type": "remote",
        "url": "https://x.example/mcp",
        "environment": {"AIRLIE_TEST_CMDS": "make test"},
    }
    path.write_text(json.dumps({"mcp": {"airlie": bare}}))
    assert install_opencode(path, project).startswith("updated ")
    environment = {
        "AIRLIE_TEST_CMDS": "make test",
        "AIRLIE_URL": "http://127.0.0.1:8090",
    }  # added: it was missing
    assert json.loads(path.read_text())["mcp"]["airlie"] == {
        **bare,
        "type": "local",
        "command": command,
        "environment": environment,
    }


def test_a_write_that_fails_never_truncates_the_config(tmp_path: Path) -> None:
    path = tmp_path / "opencode.json"
    raw = '{"provider": {"x": {"name": "\\udcff"}}}'  # valid JSON, but its lone surrogate is no UTF-8
    path.write_text(raw)
    with pytest.raises(UnicodeEncodeError):
        install_opencode(path, tmp_path / "airlie")
    assert path.read_text() == raw  # whole: the new text went to a file beside it, which is gone
    assert sorted(p.name for p in tmp_path.iterdir() if not p.name.endswith(".bak")) == ["opencode.json"]


def test_install_follows_a_symlinked_config_and_keeps_the_link(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles/opencode.json"
    real.parent.mkdir()
    real.write_text('{"model": "airlie/coder"}')
    link = tmp_path / ".config/opencode/opencode.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    install_opencode(link, tmp_path / "airlie")
    assert link.is_symlink() and json.loads(real.read_text())["mcp"]["airlie"] == opencode_entry(
        tmp_path / "airlie"
    )


@pytest.mark.parametrize("linked", [False, True], ids=["file", "symlink to it"])
def test_install_leaves_a_read_only_config_as_it_is(tmp_path: Path, linked: bool) -> None:
    """A new file moved into place would replace even a file the person made read-only."""
    real = tmp_path / "dotfiles/opencode.json"
    real.parent.mkdir()
    real.write_text('{"mcp": {}}\n')
    real.chmod(0o444)
    path = tmp_path / "opencode.json" if linked else real
    if linked:
        path.symlink_to(real)
    with pytest.raises(InstallError) as e:
        install_opencode(path, tmp_path / "airlie")
    assert str(e.value).startswith(f'{path} is read-only, so airlie left it as it is: add "airlie": {{')
    assert real.read_text() == '{"mcp": {}}\n' and stat.S_IMODE(real.stat().st_mode) == 0o444
    assert [p.name for p in real.parent.iterdir()] == ["opencode.json"]  # no backup, no new file beside it
    real.chmod(0o644)
    install_opencode(path, tmp_path / "airlie")
    real.chmod(0o444)
    assert "unchanged" in install_opencode(path, tmp_path / "airlie")  # nothing to write: nothing refused


def test_install_from_a_temporary_worktree_says_to_run_it_from_the_main_checkout(tmp_path: Path) -> None:
    out = install_opencode(tmp_path / "a.json", tmp_path / "airlie/.claude/worktrees/feature-x")
    assert "Warning:" in out and "disappears once its branch is merged" in out and "main checkout" in out
    assert "Warning" not in install_opencode(tmp_path / "b.json", tmp_path / "airlie")


@pytest.mark.parametrize(
    "content",
    [
        b'{"mcp": {}, // OpenCode reads comments, airlie does not\n}',
        b"[1, 2]",
        b'{"mcp": ["x"]}',
        b"\xff{",
        b'{"mcp": {"airlie": "uv run airlie mcp"}}',
        b'{"mcp": {"airlie": {"environment": ["AIRLIE_URL=x"]}}}',
    ],
)
def test_install_leaves_a_config_that_is_no_json_object_as_it_is(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "opencode.json"
    path.write_bytes(content)
    with pytest.raises(InstallError, match="so airlie left it as it is") as e:
        install_opencode(path, tmp_path / "airlie")
    assert str(e.value).startswith(str(path)) and '"airlie": {"type": "local"' in str(e.value)  # by hand
    assert path.read_bytes() == content and list(tmp_path.glob("*.bak")) == []


def test_mcp_config_prints_the_entry_and_install_writes_it_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))  # never the real ~/.config/opencode
    assert mcp_server.CHECKOUT == ROOT  # the checkout uv starts airlie mcp from
    entry = opencode_entry(ROOT)
    printed = runner.invoke(cli, ["mcp", "config"])
    assert printed.exit_code == 0 and json.loads(printed.stdout) == {"mcp": {"airlie": entry}}
    path = tmp_path / ".config/opencode/opencode.json"
    r = runner.invoke(cli, ["mcp", "config", "--install"])
    assert r.exit_code == 0, r.output
    assert ("main checkout" in r.output) is (
        "/.claude/worktrees/" in str(ROOT)
    )  # warned from a worktree only
    assert json.loads(path.read_text())["mcp"]["airlie"] == entry
    for broken, why in (("{oops", "is not valid JSON"), ('{"x": "\\udcff"}', "UnicodeEncodeError")):
        path.write_text(broken)
        r = runner.invoke(cli, ["mcp", "config", "--install"])
        assert r.exit_code == 1 and why in r.output and "so airlie left it as it is" in r.output, r.output
        assert "Traceback" not in r.output and path.read_text() == broken
    path.write_text("{}")
    path.parent.chmod(0o500)  # neither a backup nor the new file can be written beside it
    try:
        r = runner.invoke(cli, ["mcp", "config", "--install"])
    finally:
        path.parent.chmod(0o700)
    assert r.exit_code == 1 and "PermissionError" in r.output and "so airlie left it as it is" in r.output
    assert "Traceback" not in r.output and path.read_text() == "{}"


async def test_airlie_mcp_serves_the_five_tools_over_stdio(tmp_path: Path) -> None:
    cfg_for(tmp_path)
    args = ["-m", "airlie.cli", "mcp", "--config", str(tmp_path / "cfg/airlie.yaml")]
    params = StdioServerParameters(command=sys.executable, args=args, cwd=str(tmp_path))
    with anyio.fail_after(60):
        async with Client(params, mode="legacy") as client:
            names = {t.name for t in (await client.list_tools()).tools}
    assert names == {"plan", "run", "status", "cancel", "story"}
