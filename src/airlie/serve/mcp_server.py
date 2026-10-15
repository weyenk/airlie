"""`airlie mcp`: the studio and the story pipeline as MCP tools over stdio, for OpenCode (2.0 runs no tool
files) and any other MCP client (Claude Code, VS Code). Each tool is a thin client of `task serve`'s jobs API:
plan (the person approves it), run, story, status, cancel. A run's test command is refused unless the person
configured it, and a story takes none (its gate is its repository's AGENTS.md); a call the client cancels
stops the job it started, and every call ends within CALL_BUDGET_S however slow serve is."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
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
TEMPORARY_CHECKOUT = "/.claude/worktrees/"  # a worktree there goes away once its branch is merged
# An MCP client may give a call 60 s (the TypeScript SDK's default). Every call ends within CALL_BUDGET_S: its
# requests end REQUEST_SLACK_S before that, each bounded by the time left, and a backstop covers the rest.
CALL_BUDGET_S = 45.0
REQUEST_SLACK_S = 0.5
POLL_MARGIN_S = 1.5  # a long poll asks serve to answer this long before its request must end
STATUS_WAIT_S = 40.0  # airlie_status's default long poll
STATUS_WAIT_MAX_S = 43.0  # the longest that fits in a call: CALL_BUDGET_S - REQUEST_SLACK_S - POLL_MARGIN_S
STOP_TIMEOUT_S = 5.0
FIELD_CHARS = 160
NO_KEY = (
    " (no API key was sent: set AIRLIE_SERVE_API_KEY where airlie mcp runs; it is sent only to an https or a"
    " loopback AIRLIE_URL)"
)
REPO = "path of the git repository to work in (default: the one airlie mcp was started in)"
STORY_REPO = (
    "for a written request, the path of the git repository to build in (default: the one airlie mcp was "
    "started in, else story.repo); for a Jira issue leave it out: its repo:<owner>/<name> label names the "
    "repository"
)
ONLY_CONFIGURED = (
    "Leave it out unless the person names one: only a command they configured "
    "(story.test_cmd, AIRLIE_TEST_CMDS) is accepted"
)


class InstallError(Exception):
    """OpenCode's config cannot be merged into as it is."""


class _NoAnswer(ToolError):
    """serve did not answer a request before the call's time was up."""


class _Call:
    """One tool call: the time by which its requests must end, and the job it started, if any."""

    def __init__(self, seconds: float) -> None:
        self.deadline = anyio.current_time() + seconds
        self.job_id: str | None = None

    def left(self) -> float:
        return self.deadline - anyio.current_time()

    def wait_left(self) -> float:
        """The longest long poll that still ends in time."""
        return max(self.left() - POLL_MARGIN_S, 0.0)


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


def _no_news(job_id: str) -> str:
    return (
        f"{job_id}: airlie did not answer in time (a call has {CALL_BUDGET_S:g} s; is it busy or hung?). "
        "Call airlie_status with this job id to follow it."
    )


def allowed_test_cmds(cfg: AirlieConfig | None, env: Mapping[str, str]) -> list[str]:
    """The test commands the person configured, and so the only ones run passes on: story.test_cmd, then
    each line of AIRLIE_TEST_CMDS (blank lines skipped, surrounding spaces dropped). The first is the one a
    run uses when the model names none."""
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
    timeout = httpx.Timeout(CALL_BUDGET_S, connect=10.0)  # each request also gets its call's time left
    return httpx.AsyncClient(
        base_url=base, headers=headers, transport=transport, timeout=timeout, follow_redirects=False
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
        call: _Call,
        body: dict[str, Any] | None = None,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One request, ended by the call's deadline whatever serve does: a cancel scope bounds it, not only
        httpx's per-phase timeouts. _NoAnswer when the time ran out."""
        base = str(self.http.base_url).rstrip("/")
        payload = None if body is None else {k: v for k, v in body.items() if v is not None}
        res: httpx.Response | None = None
        left = call.left()
        if left > 0:
            with anyio.move_on_after(left):
                try:
                    res = await self.http.request(method, path, json=payload, params=params, timeout=left)
                except httpx.TimeoutException:
                    pass
                except httpx.RequestError as e:
                    why = f"{type(e).__name__}: {e}"
                    raise ToolError(
                        f"airlie is not reachable at {base}; start it with `task serve` ({why})"
                    ) from None
        if res is None:
            maybe = "; the job may have started anyway" if method == "POST" else ""
            raise _NoAnswer(
                f"airlie at {base} did not answer {method} {path} in time (a call has {CALL_BUDGET_S:g} s; "
                f"is it busy or hung?){maybe}"
            )
        if not res.is_success:
            hint = NO_KEY if res.status_code == 401 and "authorization" not in res.request.headers else ""
            raise ToolError(f"airlie {method} {path}: {res.status_code} {res.text[:800]}{hint}")
        try:
            out = res.json() if res.content else {}
        except ValueError:
            raise ToolError(f"airlie {method} {path}: the answer is not JSON: {res.text[:200]}") from None
        return out if isinstance(out, dict) else {}

    async def start(self, path: str, body: dict[str, Any], call: _Call) -> str:
        """POST a job and keep its id on `call`. The POST is shielded from the client's cancel, so the id
        always comes back (the call's deadline still ends it); a cancel that came meanwhile lands right after
        it, and the tool then stops the job."""
        with anyio.CancelScope(shield=True):
            call.job_id = job_id = str((await self.call("POST", path, call, body))["job_id"])
        await anyio.lowlevel.checkpoint()
        return job_id

    async def follow(self, job_id: str, wait: float, call: _Call) -> dict[str, Any]:
        """The job's state and its events since the last call: a long poll of up to `wait` seconds."""
        since = self.cursors.get(job_id, 0)
        r = await self.call("GET", _job_path(job_id), call, params={"since": since, "wait": wait})
        self.cursors[job_id] = int(r.get("next", since))
        return r

    async def stop(self, job_id: str) -> None:
        """Best-effort DELETE of the job a cancelled call started (airlie gets 5 seconds). The call is over,
        so a job that may still be running can only be logged."""
        with anyio.CancelScope(shield=True):
            try:
                await self.call("DELETE", _job_path(job_id), _Call(STOP_TIMEOUT_S))
            except ToolError as e:
                log.warning("airlie job %s may still be running: %s", job_id, e)


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
    the new events, then the result or the error once the job has ended (a plan as airlie_plan shows it)."""
    result = r.get("result")
    blocked = result.get("blocked") if r.get("state") == "done" and isinstance(result, dict) else None
    state = f"stopped: {_one_line(str(blocked))}" if blocked else str(r.get("state"))
    out = [f"{job_id}: {state}", *(_event_line(e) for e in r.get("events") or [])]
    if isinstance(result, dict) and r.get("kind") == "plan":
        out += ["", _plan_outcome(result)]
    elif result is not None:
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


def _plan_outcome(result: dict[str, Any]) -> str:
    """A finished plan job as the person and airlie_run need it: the table and the plan's JSON, or the
    chief's answer when it answered instead of planning."""
    if result.get("type") == "answer":
        return f"The chief answered without a plan:\n\n{result.get('answer')}"
    plan = result.get("plan") or {}
    return f"{_plan_table(plan)}\n\nPlan JSON for airlie_run:\n{json.dumps(plan, ensure_ascii=False)}"


def build_server(
    http: httpx.AsyncClient, *, test_cmds: Sequence[str], cwd: Path, no_config: str = ""
) -> MCPServer[Any]:
    """The MCP server "airlie" with the tools plan, run, status, cancel and story (in OpenCode: airlie_plan
    and so on), each a client of serve through `http`. `test_cmds` are the only test commands run passes on,
    and the first is what it uses when the model names none; the git work tree of `cwd` (where the client
    started airlie mcp) is the default repository, but for a Jira issue's story; `no_config` says why
    story.test_cmd could not be read, when it could not."""
    serve = _Serve(http)
    allowed = list(test_cmds)
    run_default = allowed[0] if allowed else None
    here = cwd.resolve()
    server: MCPServer[Any] = MCPServer("airlie")

    def checked(test_cmd: str | None, *, left_out: str) -> str | None:
        """A test command the person configured, or None when the model named none. airlie runs it in a shell
        on this machine, so any other is refused here, before a request is sent; `left_out` says what leaving
        it out does."""
        cmd = _given(test_cmd)
        if cmd is None or cmd in allowed:
            return cmd
        configured = ", ".join(map(repr, allowed)) or "none"
        unread = f"; story.test_cmd could not be read: {no_config}" if no_config else ""
        raise ToolError(
            f"test_cmd {cmd!r} was refused: airlie runs only a test command the person configured "
            f"(story.test_cmd in airlie.yaml, or a line of AIRLIE_TEST_CMDS where airlie mcp runs). "
            f"Configured: {configured}{unread}. Leave test_cmd out to {left_out}, or ask the person to "
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

    async def bounded(work: Callable[[_Call], Awaitable[str]]) -> str:
        """Run a tool's work within CALL_BUDGET_S: its requests end by the call's deadline, and the backstop
        covers anything else. A call the client cancels stops the job it started."""
        call = _Call(CALL_BUDGET_S - REQUEST_SLACK_S)
        with anyio.move_on_after(CALL_BUDGET_S) as backstop:
            try:
                return await work(call)
            except anyio.get_cancelled_exc_class():
                if call.job_id is not None and not backstop.cancel_called:
                    await serve.stop(call.job_id)
                raise
        if call.job_id is not None:
            return _no_news(call.job_id)
        raise ToolError(
            f"airlie did not answer in time (a call has {CALL_BUDGET_S:g} s; is it busy or hung?)."
        )

    @server.tool(name="plan", structured_output=False)
    async def plan_tool(
        goal: Annotated[str, Field(description="what to build or change, in the person's words")],
        test_cmd: Annotated[
            str | None, Field(description="command that runs the repository's tests (planning only lists it)")
        ] = None,
        repo: Annotated[str | None, Field(description=REPO)] = None,
    ) -> str:
        """Ask the airlie studio's chief for a plan (subtasks, roles, dependencies) for a goal in the
        repository. Nothing is built. Waits about 40 seconds; if the chief is still planning then, it
        returns the job id: call airlie_status with it until the plan comes. Show the plan to the person and
        wait for their approval before airlie_run."""
        body = {"goal": goal, "repo": repository(repo, needed=True), "test_cmd": _given(test_cmd)}

        async def work(call: _Call) -> str:
            job_id = await serve.start("/v1/airlie/plans", body, call)
            try:
                r = await serve.follow(job_id, call.wait_left(), call)
                while r.get("state") == "running" and call.wait_left() > 0:
                    r = await serve.follow(job_id, call.wait_left(), call)
            except _NoAnswer:
                return _no_news(job_id)
            if r.get("state") == "running":  # the job goes on; airlie_status shows the plan when it ends
                return (
                    f"{job_id}: still planning. Call airlie_status with this job id; when planning ends it "
                    "shows the plan (or the chief's answer)."
                )
            if r.get("state") != "done":
                raise ToolError(f"planning {r.get('state')}: {r.get('error') or ''}")
            return _plan_outcome(r.get("result") or {})

        return await bounded(work)

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
            str | None,
            Field(
                description="command that runs the tests (default: story.test_cmd, else the first line of "
                "AIRLIE_TEST_CMDS; with neither, the run has no test gate). " + ONLY_CONFIGURED
            ),
        ] = None,
        repo: Annotated[str | None, Field(description=REPO)] = None,
    ) -> str:
        """Build in the repository with the airlie studio, on a new branch. Pass the plan the person approved
        (the JSON from airlie_plan, edited as agreed); without a plan the chief plans and runs at once.
        Returns a job id: follow it with airlie_status."""
        left_out = f"use {run_default!r}" if run_default else "run with no test gate (none is configured)"
        cmd = checked(test_cmd, left_out=left_out) or run_default
        body = {"goal": goal, "repo": repository(repo, needed=True), "test_cmd": cmd, "plan": plan}

        async def work(call: _Call) -> str:
            return _started(await serve.start("/v1/airlie/runs", body, call))

        return await bounded(work)

    @server.tool(name="status", structured_output=False)
    async def status_tool(
        job_id: Annotated[str, Field(description="the id airlie_plan, airlie_run or airlie_story returned")],
        wait: Annotated[float, Field(description="seconds to wait for news, at most 43")] = STATUS_WAIT_S,
    ) -> str:
        """Follow an airlie job (a plan, a studio run or a story): what happened since the last call, and the
        result once it ends (a plan as airlie_plan shows it). Waits up to `wait` seconds (default 40, at most
        43) for news; call it again until the state is done, stopped, failed or cancelled. A story that stops
        ends as a done job whose result has `blocked` set; the state then reads "stopped: <reason>": it did
        not finish."""

        async def work(call: _Call) -> str:
            try:
                r = await serve.follow(job_id, min(max(wait, 0.0), STATUS_WAIT_MAX_S, call.wait_left()), call)
            except _NoAnswer:
                return _no_news(job_id)
            return _status_text(job_id, r)

        return await bounded(work)

    @server.tool(name="cancel", structured_output=False)
    async def cancel_tool(job_id: Annotated[str, Field(description="the job to stop")]) -> str:
        """Stop a running airlie job (a studio run or a story). Work already committed stays on its branch."""

        async def work(call: _Call) -> str:
            try:
                r = await serve.call("DELETE", _job_path(job_id), call)
            except _NoAnswer:
                return (
                    f"{job_id}: airlie did not answer the cancel in time (a call has {CALL_BUDGET_S:g} s); "
                    "the job may still be running: see airlie_status."
                )
            return f"Cancelled {job_id}." if r.get("cancelled") else f"{job_id} was not running."

        return await bounded(work)

    @server.tool(name="story", structured_output=False)
    async def story_tool(
        card: Annotated[str | None, Field(description="Jira issue key (AIR-7) or its link")] = None,
        text: Annotated[str | None, Field(description="the request, when there is no card")] = None,
        repo: Annotated[str | None, Field(description=STORY_REPO)] = None,
    ) -> str:
        """Run the story pipeline: a Jira issue (key like AIR-7, or its link) or a written request is refined
        into a story, split into layers, built test-first one branch per layer, and opened as stacked draft
        PRs (the issue moves to In Review). An issue is built in the repository its repo:<owner>/<name> label
        names; every layer must pass the Test gate of that repository's AGENTS.md. Only a human merges.
        Returns a job id for airlie_status."""
        card, text = _given(card), _given(text)
        if (card is None) == (text is None):
            raise ToolError("pass exactly one of card or text")
        # an issue's repository is its label: the work tree airlie mcp runs in is sent only when named
        where = repository(repo, needed=False) if text is not None or _given(repo) else None
        body = {"card": card, "text": text, "repo": where}

        async def work(call: _Call) -> str:  # the POST may wait for Jira: it gets the whole call's time
            return _started(await serve.start("/v1/airlie/stories", body, call))

        return await bounded(work)

    return server


def guard_config(config: Path | None) -> tuple[AirlieConfig | None, str]:
    """The airlie config whose story.test_cmd airlie_run accepts, or None and why it did not load: --config
    (what OpenCode's entry passes: this checkout's airlie.yaml), else AIRLIE_CONFIG or an airlie.yaml above
    the directory airlie mcp runs in, else this checkout's own."""
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
    """OpenCode's `mcp.airlie`: uv starts airlie mcp from this checkout, whatever project OpenCode is in, with
    this checkout's airlie.yaml (never one that happens to be above the project OpenCode runs in)."""
    config = str(project / DEFAULT_CONFIG)
    return {
        "type": "local",
        "command": ["uv", "run", "--quiet", "--project", str(project), "airlie", "mcp", "--config", config],
        "environment": {"AIRLIE_URL": DEFAULT_URL},
    }


def opencode_config() -> Path:
    """OpenCode's global config, where its provider settings are."""
    return Path.home() / ".config" / "opencode" / "opencode.json"


def install_opencode(path: Path, project: Path) -> str:
    """Put airlie's MCP server into OpenCode's config at `path`, keeping everything else, and say what was
    done. An `mcp.airlie` the person already has is merged into, not replaced: `type` and `command` are set,
    and every other key and environment variable stays (AIRLIE_URL is added only when it is missing). A
    missing file is created. A file that changes is first copied beside itself to
    `<name>.<YYYYmmdd-HHMMSS>.bak` (its mode kept: it may hold keys), then replaced whole by a file written
    beside it, so a write that fails never truncates it. A file that is not a JSON object, or whose entry is
    not one, or that is read-only, is refused (InstallError) and left as it is. Installing from a temporary
    worktree warns."""
    entry = opencode_entry(project)
    warning = (
        f"\nWarning: {project} is a temporary worktree checkout; it disappears once its branch is merged, "
        "and OpenCode then cannot start airlie mcp. Run `task opencode:install` from the main checkout."
        if TEMPORARY_CHECKOUT in f"{project}/"
        else ""
    )
    by_hand = f'add "airlie": {json.dumps(entry)} to its "mcp" object yourself'
    if not path.exists():
        _replace_json(path, {"$schema": OPENCODE_SCHEMA, "mcp": {"airlie": entry}})
        return f"created {path} with the airlie MCP server{warning}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise InstallError(f"{path} is not valid JSON ({e}), so airlie left it as it is: {by_hand}") from None
    servers = data.get("mcp", {}) if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise InstallError(
            f'{path} is not a JSON object with an "mcp" object, so airlie left it as it is: {by_hand}'
        )
    old = servers.get("airlie")
    if old is None:
        new = entry
    elif isinstance(old, dict) and isinstance(old.get("environment", {}), dict):
        environment = {**entry["environment"], **old.get("environment", {})}
        new = {**old, "type": entry["type"], "command": entry["command"], "environment": environment}
    else:
        raise InstallError(
            f'{path}: its "mcp.airlie" (or that entry\'s "environment") is not a JSON object, so airlie left '
            f"it as it is: {by_hand}"
        )
    if new == old:
        return f"{path} unchanged: it already starts the airlie MCP server{warning}"
    if not os.access(path.resolve(), os.W_OK):  # the move into place would replace even a read-only file
        raise InstallError(f"{path} is read-only, so airlie left it as it is: {by_hand}")
    backup = _backup(path)
    data["mcp"] = {**servers, "airlie": new}
    _replace_json(path, data)
    done = "added the airlie MCP server to" if old is None else "updated the airlie MCP server in"
    return f"{done} {path} (backup: {backup}){warning}"


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


def _replace_json(path: Path, data: Any) -> None:
    """Write `data` to a new file beside the file `path` names (a symlink is followed), then move it over
    that file in one step: a write that fails leaves the old file whole. The old file's mode is kept."""
    real = path.resolve()
    real.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=real.parent, prefix=f".{real.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        if real.exists():
            shutil.copymode(real, tmp)
        os.replace(tmp, real)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
