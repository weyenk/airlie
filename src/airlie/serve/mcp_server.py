"""`airlie mcp`: the studio and the story pipeline as MCP tools over stdio, for OpenCode (2.0 runs no tool
files) and any other MCP client (Claude Code, VS Code). Each tool is a thin client of `task serve`'s jobs API:
plan (the person approves it), run, story, status, cancel. A test command the model chose is refused unless
the person configured it, and a call the client cancels stops the job it started."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import shutil
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import anyio
import anyio.lowlevel
import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from airlie.config import AirlieConfig, ConfigError, load_config
from airlie.config.load import DEFAULT_CONFIG, find_config

log = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:8090"
CHECKOUT = Path(__file__).resolve().parents[3]  # the airlie checkout this runs from (uv installs it editable)
OPENCODE_SCHEMA = "https://opencode.ai/config.json"
POLL_TIMEOUT = httpx.Timeout(300.0, connect=10.0)  # above the longest wait a tool asks for (240 s)
POST_TIMEOUT_S = 60.0  # a POST is shielded from cancellation, so it is bounded
STOP_TIMEOUT_S = 5.0
FIELD_CHARS = 160
NO_KEY = (
    " (no API key was sent: set AIRLIE_SERVE_API_KEY where airlie mcp runs; it is sent only to an https or a"
    " loopback AIRLIE_URL)"
)
REPO = "path of the git repository to work in (default: the one airlie mcp was started in)"
ONLY_CONFIGURED = (
    "Leave it out unless the person names one: only a command they configured "
    "(story.test_cmd, AIRLIE_TEST_CMDS) is accepted"
)


class InstallError(Exception):
    """OpenCode's config cannot be merged into as it is."""


def _given(value: str | None) -> str | None:
    """An optional string argument, or None when it is missing or blank: models fill those with ""."""
    return value.strip() if value is not None and value.strip() else None


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _work_tree(start: Path) -> Path | None:
    """The git work tree `start` is in (the nearest directory with a .git), or None."""
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return None


def _job_path(job_id: str) -> str:
    return f"/v1/airlie/jobs/{quote(job_id, safe='')}"


def _started(job_id: str) -> str:
    return f"Started {job_id}. Follow it with airlie_status."


def allowed_test_cmds(cfg: AirlieConfig | None, env: Mapping[str, str]) -> list[str]:
    """The test commands the person configured, and so the only ones run and story pass on: story.test_cmd,
    then each line of AIRLIE_TEST_CMDS (blank lines skipped, surrounding spaces dropped)."""
    configured = [
        cfg.story.test_cmd if cfg is not None else None,
        *env.get("AIRLIE_TEST_CMDS", "").splitlines(),
    ]
    return list(dict.fromkeys(c for c in map(_given, configured) if c is not None))


def _private(base: httpx.URL) -> bool:
    """https, or a loopback address: a key sent there does not cross a network in clear text."""
    if base.scheme == "https" or base.host == "localhost":
        return True
    try:
        return ipaddress.ip_address(base.host).is_loopback
    except ValueError:
        return False


def airlie_http(
    env: Mapping[str, str], transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """The client of `task serve` at AIRLIE_URL (default http://127.0.0.1:8090). AIRLIE_SERVE_API_KEY goes
    along as a Bearer token only to that base (no redirect is followed), and only when it is https or
    loopback."""
    base = httpx.URL(env.get("AIRLIE_URL") or DEFAULT_URL)
    key = env.get("AIRLIE_SERVE_API_KEY")
    headers = {"authorization": f"Bearer {key}"} if key and _private(base) else {}
    return httpx.AsyncClient(
        base_url=base, headers=headers, transport=transport, timeout=POLL_TIMEOUT, follow_redirects=False
    )


class _Serve:
    """`task serve`'s jobs API as the tools use it; `cursors` keeps, per job, how many events were shown."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http
        self.cursors: dict[str, int] = {}

    async def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        base = str(self.http.base_url).rstrip("/")
        payload = None if body is None else {k: v for k, v in body.items() if v is not None}
        bounded: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        try:
            res = await self.http.request(method, path, json=payload, params=params, **bounded)
        except httpx.TimeoutException as e:
            raise ToolError(
                f"airlie at {base} did not answer {method} {path} in time ({type(e).__name__})"
            ) from None
        except httpx.RequestError as e:
            raise ToolError(
                f"airlie is not reachable at {base}; start it with `task serve` ({type(e).__name__}: {e})"
            ) from None
        if not res.is_success:
            hint = NO_KEY if res.status_code == 401 and "authorization" not in res.request.headers else ""
            raise ToolError(f"airlie {method} {path}: {res.status_code} {res.text[:800]}{hint}")
        try:
            out = res.json() if res.content else {}
        except ValueError:
            raise ToolError(f"airlie {method} {path}: the answer is not JSON: {res.text[:200]}") from None
        return out if isinstance(out, dict) else {}

    async def start(self, path: str, body: dict[str, Any]) -> str:
        """POST a job and return its id. The POST is shielded from cancellation, so the id always comes back;
        when the call was cancelled meanwhile, the job is stopped before the cancellation goes on."""
        with anyio.CancelScope(shield=True):
            job_id = str((await self.call("POST", path, body, timeout=POST_TIMEOUT_S))["job_id"])
        try:
            await anyio.lowlevel.checkpoint()  # a cancellation that came during the POST lands here
        except anyio.get_cancelled_exc_class():
            await self.stop(job_id)
            raise
        return job_id

    async def follow(self, job_id: str, wait: float) -> dict[str, Any]:
        """The job's state and its events since the last call: a long poll of up to `wait` seconds."""
        since = self.cursors.get(job_id, 0)
        r = await self.call("GET", _job_path(job_id), params={"since": since, "wait": wait})
        self.cursors[job_id] = int(r.get("next", since))
        return r

    async def stop(self, job_id: str) -> None:
        """Best-effort DELETE of the job a cancelled call started (airlie gets 5 seconds). The call is over,
        so a job that may still be running can only be logged."""
        with anyio.move_on_after(STOP_TIMEOUT_S, shield=True):
            try:
                await self.call("DELETE", _job_path(job_id))
            except ToolError as e:
                log.warning("airlie job %s may still be running: %s", job_id, e)
            return
        log.warning("airlie job %s may still be running: airlie did not answer the stop in time", job_id)


def _event_line(event: dict[str, Any]) -> str:
    """One event on one line: its kind, then each field that says something (each cut at 160 characters)."""
    fields = [
        f"{k}={_one_line(v if isinstance(v, str) else json.dumps(v))[:FIELD_CHARS]}"
        for k, v in event.items()
        if k not in ("ts", "run_id", "kind") and v not in (None, "", [])
    ]
    return " ".join(["-", str(event.get("kind")), *fields])


def _status_text(job_id: str, r: dict[str, Any]) -> str:
    """`<job>: <state>` (a done job whose result is blocked is a story that stopped: `stopped: <reason>`),
    the new events, then the result or the error once the job has ended."""
    result = r.get("result")
    blocked = result.get("blocked") if r.get("state") == "done" and isinstance(result, dict) else None
    state = f"stopped: {_one_line(str(blocked))}" if blocked else str(r.get("state"))
    out = [f"{job_id}: {state}", *(_event_line(e) for e in r.get("events") or [])]
    if result is not None:
        out += ["", "Result:", json.dumps(result, indent=1, ensure_ascii=False)]
    if r.get("error"):
        out += ["", f"Error: {r['error']}"]
    return "\n".join(out)


def _plan_table(plan: dict[str, Any]) -> str:
    """The plan as the person reads it: a table of its subtasks, then its assumptions."""
    rows = [
        f"| {s.get('id')} | {s.get('role') or '-'} | {_one_line(str(s.get('brief')))} | "
        f"{', '.join(s.get('depends_on') or []) or '-'} |"
        for s in plan.get("subtasks") or []
    ]
    assumptions = [f"- {a}" for a in plan.get("assumptions") or []]
    tail = ["", "Assumptions:", *assumptions] if assumptions else []
    return "\n".join(["| id | role | brief | depends on |", "| --- | --- | --- | --- |", *rows, *tail])


def build_server(
    http: httpx.AsyncClient, *, test_cmds: Sequence[str], cwd: Path, no_config: str = ""
) -> MCPServer[Any]:
    """The MCP server "airlie" with the tools plan, run, status, cancel and story (in OpenCode: airlie_plan
    and so on), each a client of serve through `http`. `test_cmds` are the only test commands run and story
    pass on; the git work tree of `cwd` (where the client started airlie mcp) is the default repository;
    `no_config` says why story.test_cmd could not be read, when it could not."""
    serve = _Serve(http)
    allowed = list(test_cmds)
    here = cwd.resolve()
    server: MCPServer[Any] = MCPServer("airlie")

    def checked(test_cmd: str | None) -> str | None:
        """A test command the person configured, or None for serve's default. airlie runs it in a shell on
        this machine, so any other is refused here, before a request is sent."""
        cmd = _given(test_cmd)
        if cmd is None or cmd in allowed:
            return cmd
        configured = ", ".join(map(repr, allowed)) or "none"
        unread = f"; story.test_cmd could not be read: {no_config}" if no_config else ""
        raise ToolError(
            f"test_cmd {cmd!r} was refused: airlie runs only a test command the person configured "
            f"(story.test_cmd in airlie.yaml, or a line of AIRLIE_TEST_CMDS where airlie mcp runs). "
            f"Configured: {configured}{unread}. Leave test_cmd out to use the default, or ask the person to "
            "configure the command."
        )

    def repository(repo: str | None, *, needed: bool) -> str | None:
        """The git work tree to work in: the one `repo` is in (a relative path counts from where airlie mcp
        runs), else the one airlie mcp runs in. None when that is no repository and the tool can do without
        one: serve then uses story.repo."""
        named = _given(repo)
        start = (here / Path(named).expanduser()).resolve() if named else here
        tree = _work_tree(start)
        if tree is None and (named is not None or needed):
            raise ToolError(
                f"{start} is in no git repository; pass repo, the path of the repository to work in"
            )
        return None if tree is None else str(tree)

    @server.tool(name="plan", structured_output=False)
    async def plan_tool(
        goal: Annotated[str, Field(description="what to build or change, in the person's words")],
        test_cmd: Annotated[
            str | None, Field(description="command that runs the repository's tests (planning only lists it)")
        ] = None,
        repo: Annotated[str | None, Field(description=REPO)] = None,
    ) -> str:
        """Ask the airlie studio's chief for a plan (subtasks, roles, dependencies) for a goal in the
        repository. Nothing is built. Show the plan to the person and wait for their approval before
        airlie_run."""
        body = {"goal": goal, "repo": repository(repo, needed=True), "test_cmd": _given(test_cmd)}
        job_id = await serve.start("/v1/airlie/plans", body)
        try:
            r = await serve.follow(job_id, 60)
            while r.get("state") == "running":
                r = await serve.follow(job_id, 60)
        except anyio.get_cancelled_exc_class():
            await serve.stop(job_id)
            raise
        if r.get("state") != "done":
            raise ToolError(f"planning {r.get('state')}: {r.get('error') or ''}")
        result = r.get("result") or {}
        if result.get("type") == "answer":
            return f"The chief answered without a plan:\n\n{result.get('answer')}"
        plan_json = json.dumps(result["plan"], ensure_ascii=False)
        return f"{_plan_table(result['plan'])}\n\nPlan JSON for airlie_run:\n{plan_json}"

    @server.tool(name="run", structured_output=False)
    async def run_tool(
        goal: Annotated[str, Field(description="the goal the plan was made for")],
        plan: Annotated[
            dict[str, Any] | None,
            Field(
                description="the plan the person approved: the JSON airlie_plan returned, edited as agreed"
            ),
        ] = None,
        test_cmd: Annotated[
            str | None, Field(description="command that runs the tests. " + ONLY_CONFIGURED)
        ] = None,
        repo: Annotated[str | None, Field(description=REPO)] = None,
    ) -> str:
        """Build in the repository with the airlie studio, on a new branch. Pass the plan the person approved
        (the JSON from airlie_plan, edited as agreed); without a plan the chief plans and runs at once.
        Returns a job id: follow it with airlie_status."""
        cmd = checked(test_cmd)
        body = {"goal": goal, "repo": repository(repo, needed=True), "test_cmd": cmd, "plan": plan}
        return _started(await serve.start("/v1/airlie/runs", body))

    @server.tool(name="status", structured_output=False)
    async def status_tool(
        job_id: Annotated[str, Field(description="the id airlie_run or airlie_story returned")],
        wait: Annotated[float, Field(description="seconds to wait for news, at most 240")] = 120,
    ) -> str:
        """Follow an airlie job (a studio run or a story): what happened since the last call, and the result
        once it ends. Waits up to `wait` seconds (default 120) for news; call it again until the state is
        done, stopped, failed or cancelled. A story that stops ends as a done job whose result has `blocked`
        set; the state then reads "stopped: <reason>": it did not finish."""
        return _status_text(job_id, await serve.follow(job_id, min(max(wait, 0.0), 240.0)))

    @server.tool(name="cancel", structured_output=False)
    async def cancel_tool(job_id: Annotated[str, Field(description="the job to stop")]) -> str:
        """Stop a running airlie job (a studio run or a story). Work already committed stays on its branch."""
        r = await serve.call("DELETE", _job_path(job_id))
        return f"Cancelled {job_id}." if r.get("cancelled") else f"{job_id} was not running."

    @server.tool(name="story", structured_output=False)
    async def story_tool(
        card: Annotated[str | None, Field(description="Jira issue key (AIR-7) or its link")] = None,
        text: Annotated[str | None, Field(description="the request, when there is no card")] = None,
        test_cmd: Annotated[
            str | None,
            Field(description="command every layer must pass (default: story.test_cmd). " + ONLY_CONFIGURED),
        ] = None,
        repo: Annotated[str | None, Field(description=REPO + ", else story.repo")] = None,
    ) -> str:
        """Run the story pipeline in the repository: a Jira issue (key like AIR-7, or its link) or a written
        request is refined into a story, split into layers, built test-first one branch per layer, and opened
        as stacked draft PRs (the issue moves to In Review). Only a human merges. Returns a job id for
        airlie_status."""
        card, text = _given(card), _given(text)
        if (card is None) == (text is None):
            raise ToolError("pass exactly one of card or text")
        cmd = checked(test_cmd)
        body = {"card": card, "text": text, "repo": repository(repo, needed=False), "test_cmd": cmd}
        return _started(await serve.start("/v1/airlie/stories", body))

    return server


def guard_config(config: Path | None) -> tuple[AirlieConfig | None, str]:
    """The airlie config whose story.test_cmd the tools accept, or None and why it did not load: --config,
    else AIRLIE_CONFIG or an airlie.yaml above the directory airlie mcp runs in, else this checkout's own (a
    client starts airlie mcp in its own project, not here)."""
    try:
        path = config or find_config()
    except ConfigError:
        path = CHECKOUT / DEFAULT_CONFIG
    try:
        return load_config(path), ""
    except ConfigError as e:
        return None, str(e)


async def serve_stdio(config: Path | None = None) -> None:
    """`airlie mcp`: serve the tools over stdin and stdout until the client closes them."""
    cfg, why = guard_config(config)
    async with airlie_http(os.environ) as http:
        test_cmds = allowed_test_cmds(cfg, os.environ)
        await build_server(http, test_cmds=test_cmds, cwd=Path.cwd(), no_config=why).run_stdio_async()


def opencode_entry(project: Path) -> dict[str, Any]:
    """OpenCode's `mcp.airlie`: uv starts airlie mcp from this checkout, whatever project OpenCode is in."""
    return {
        "type": "local",
        "command": ["uv", "run", "--quiet", "--project", str(project), "airlie", "mcp"],
        "environment": {"AIRLIE_URL": DEFAULT_URL},
    }


def opencode_config() -> Path:
    """OpenCode's global config, where its provider settings are."""
    return Path.home() / ".config" / "opencode" / "opencode.json"


def install_opencode(path: Path, project: Path) -> str:
    """Merge `mcp.airlie` into OpenCode's config at `path`, keeping everything else, and say what was done. A
    missing file is created; one that already has the entry is left alone; any other is first copied beside
    itself to `<name>.<YYYYmmdd-HHMMSS>.bak`, its mode kept (it may hold keys). A file that is not a JSON
    object is refused (InstallError) and left as it is."""
    entry = opencode_entry(project)
    by_hand = f'add "airlie": {json.dumps(entry)} to its "mcp" object yourself'
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, {"$schema": OPENCODE_SCHEMA, "mcp": {"airlie": entry}})
        return f"created {path} with the airlie MCP server"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise InstallError(f"{path} is not valid JSON ({e}), so airlie left it as it is: {by_hand}") from None
    servers = data.get("mcp", {}) if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise InstallError(
            f'{path} is not a JSON object with an "mcp" object, so airlie left it as it is: {by_hand}'
        )
    if servers.get("airlie") == entry:
        return f"{path} already starts the airlie MCP server"
    backup = _backup(path)
    data["mcp"] = {**servers, "airlie": entry}
    _write_json(path, data)
    return f"added the airlie MCP server to {path} (backup: {backup})"


def _backup(path: Path) -> Path:
    """Copy `path` beside itself, mode kept, under a timestamped name no earlier backup has."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.{stamp}.bak")
    n = 0
    while backup.exists():
        n += 1
        backup = path.with_name(f"{path.name}.{stamp}-{n}.bak")
    shutil.copy2(path, backup)
    return backup


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
