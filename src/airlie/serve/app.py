"""`task serve`: the client-agnostic front door. Any OpenAI-compatible client (Claude Code, Qwen Code, curl)
can talk to a role by name; `/v1/airlie/run` launches a parallel team run. The jobs API starts a studio plan
(`/v1/airlie/plans`), a studio run (`/v1/airlie/runs`) or a story (`/v1/airlie/stories`) in the background; a
client follows the job by long polls on `GET /v1/airlie/jobs/{id}?since=N&wait=S` and cancels it with
`DELETE`."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from airlie.auth.api import PUBLIC_PATHS, auth_router
from airlie.auth.principal import NotInvited, Principal, Resolver
from airlie.auth.store import AuthStore
from airlie.board import BoardStore
from airlie.board.api import board_router
from airlie.config import AirlieConfig
from airlie.metrics import series as metrics_series
from airlie.metrics import summary as metrics_summary
from airlie.prompts import text as hub_text
from airlie.report import RunReport
from airlie.serve.feed import Feed, event_stream
from airlie.serve.flow import StoryNotFound, epic_flow, initiative_flow, job_detail, story_flow
from airlie.serve.jobs import Job, Jobs
from airlie.serve.listing import epics, initiatives, recent_runs, repo_names, repos, stories, story_page
from airlie.serve.run_flow import run_flow, run_job_detail
from airlie.serve.running import running as in_progress
from airlie.serve.ticker import LineFor, LineTicker
from airlie.serve.tree import RunNotFound, run_tree
from airlie.serve.uploads import MEDIA, Upload, UploadStore, UploadTooLarge, has_uploads, resolve
from airlie.serve.watch import Watcher
from airlie.skills import Skill, load_chat_skills, prompt_text
from airlie.story.board import BoardError
from airlie.story.diagnosis import diagnosis_json, latest, read_diagnosis
from airlie.story.epic import REPOSITORY
from airlie.story.mockups import OPTION_FILE, mockup_dir, record_pick
from airlie.story.model import StoryBusy, StoryError, StoryRunning, StorySetupError
from airlie.story.phase import set_paused
from airlie.story.pipeline import build_commit
from airlie.story.state import STAGES
from airlie.story.stats import line_stats, parse_since
from airlie.teams.worktree import rev_parse
from airlie.topology.studio import PlanOutcome, StudioOrchestrator, check_plan
from airlie.topology.studio_plan import Plan, PlanError, pin, plan_from_dict, plan_to_dict

_STORY_DIR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
"""A story key as a directory name under stories/: a route that serves a path under the state directory
matches it by name, so a key never walks out of it."""

NEXT_DIR = Path(__file__).parent / "web_next"
"""The Vue app's build (`task web:build`, a Vite build with base `/`): `GET /` and every path that is not the
API serves it. Not in git; without it the app's paths answer 503 and say how to build it."""

NEXT_NOT_BUILT = "the web app is not built: run task web:build"
IMMUTABLE = "public, max-age=31536000, immutable"
"""The cache header of a built file under assets/: Vite hashes their names, so a changed file is a new URL."""


class ToolCallStreamRepair:
    """Give every streamed tool-call delta an `id` and `type`. vllm-mlx sometimes streams a call's deltas
    without them (seen 2026-10-14 when Qwen3-Coder opened a call with a bare `<function=...>`), and the
    Vercel AI SDK behind OpenCode rejects such a chunk ("Expected 'id' to be a string").

    A choice that ends with finish_reason "tool_calls" but sent no call with a name (the engine's parser
    dropped a malformed one) ends with "stop" instead: OpenCode 2.0 fails the turn otherwise ("OpenAI Chat
    tool call delta is missing id or name"), while a "stop" reaches it as a reply with no tool call."""

    def __init__(self) -> None:
        self.ids: dict[tuple[int, int], str] = {}
        self.named: set[int] = set()  # the choices that sent a tool call with a name (every call has an id)

    def frame(self, frame: bytes) -> bytes:
        text = frame.decode("utf-8", "replace")
        if not text.startswith("data: {") or '"tool_calls"' not in text:
            return frame
        try:
            obj = json.loads(text[6:])
        except ValueError:
            return frame
        changed = False
        for choice in obj.get("choices") or []:
            n = int(choice.get("index", 0))
            for tc in (choice.get("delta") or {}).get("tool_calls") or []:
                key = (n, int(tc.get("index", 0)))
                if tc.get("id"):
                    self.ids.setdefault(key, str(tc["id"]))
                else:
                    tc["id"] = self.ids.setdefault(key, f"call_{uuid.uuid4().hex[:24]}")
                    changed = True
                if not tc.get("type"):
                    tc["type"] = "function"
                    changed = True
                if (tc.get("function") or {}).get("name"):
                    self.named.add(n)
            if choice.get("finish_reason") == "tool_calls" and n not in self.named:
                choice["finish_reason"] = "stop"
                changed = True
        return f"data: {json.dumps(obj)}".encode() if changed else frame


def _no_call_stops(reply: Any) -> Any:
    """A whole (not streamed) reply, repaired as ToolCallStreamRepair repairs a stream: a call without an id
    or a type gets one, and the finish_reason "tool_calls" of a choice that holds no call with a name becomes
    "stop"."""
    choices = reply.get("choices") if isinstance(reply, dict) else None
    for choice in choices or []:
        calls = (choice.get("message") or {}).get("tool_calls") or []
        for call in calls:
            if not call.get("id"):
                call["id"] = f"call_{uuid.uuid4().hex[:24]}"
            if not call.get("type"):
                call["type"] = "function"
        named = [c for c in calls if (c.get("function") or {}).get("name")]
        if choice.get("finish_reason") == "tool_calls" and not named:
            choice["finish_reason"] = "stop"
    return reply


class RunRequest(BaseModel):
    goal: str
    repo: str
    k: int | None = None
    test_cmd: str | None = None
    topology: str | None = None
    images: list[str] = []


class PlanRequest(BaseModel):
    goal: str
    repo: str
    test_cmd: str | None = None
    images: list[str] = []


class StartRun(PlanRequest):
    plan: dict[str, Any] | None = None
    base: str | None = None
    synthesise: bool = True


ISSUE_REPO = (
    "repo is for a written request (text) only: an issue's repository is its repo:<owner>/<name> label, and "
    "only a person's `task story --repo` builds an issue elsewhere"
)
"""Why a story request with both a card and a repo is refused."""


class StoryRequest(BaseModel):
    """A story: an issue (`card`) or a written request (`text`), and `repo`, a written request's repository
    (a blank one is none: story.repo): an issue's is its repo: label, and a request cannot move it (422,
    ISSUE_REPO). Its test gate is the repository's AGENTS.md, never the request's: a request that names one
    (test_cmd), or anything else, is refused (422) rather than quietly ignored."""

    model_config = ConfigDict(extra="forbid")

    card: str | None = None
    text: str | None = None
    repo: str | None = None


class InitiativeRequest(BaseModel):
    """A new initiative (D1 of the specs phase): the person's paragraph, the repositories it concerns
    (owner/name), and an optional budget in hours. The card is made, approved and its first turn started."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    text: str
    repos: list[str]
    budget_hours: float | None = Field(default=None, gt=0)

    @field_validator("text")
    @classmethod
    def _text(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("the initiative needs some text")
        return v

    @field_validator("repos")
    @classmethod
    def _repos(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("name at least one repository (owner/name)")
        for r in v:
            if not REPOSITORY.fullmatch(r) or ".." in r.split("/"):
                raise ValueError(f"{r!r} is not owner/name")
        return list(dict.fromkeys(v))


class LineRequest(BaseModel):
    """The line's one switch (planning-line P5b, D7): `paused` true holds every lane's next job (a running
    one finishes), false lets them go."""

    model_config = ConfigDict(extra="forbid")

    paused: bool


class PickRequest(BaseModel):
    """A person's pick among an epic's mockup pages (D4): the option's number, 1 to 9."""

    model_config = ConfigDict(extra="forbid")

    option: int = Field(ge=1, le=9)


MOCKUP_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:",
    "X-Content-Type-Options": "nosniff",
}
"""What a mockup page is served with (D13): a model wrote it and the app shows it in a sandboxed frame, so the
browser is told to run and fetch nothing, as the page check already holds the page to."""
_EPIC_ID = re.compile(r"E[0-9]+", re.ASCII)


def _initiative_job_factory() -> Callable[..., Any]:
    from airlie.story.factory import initiative_job

    return initiative_job


def _story_job_factory() -> Callable[..., Any]:
    from airlie.story.factory import story_job

    return story_job


def _run_result(report: RunReport) -> dict[str, Any]:
    return {
        "run_id": report.run_id,
        "ok": report.any_ok,
        "branch": report.winning_branch,
        "tests": report.tests,
        "final_answer": report.final_answer,
        "report": str(report.run_dir.path / "report.md"),
    }


async def _finish_run(work: Awaitable[RunReport]) -> dict[str, Any]:
    return _run_result(await work)


async def _finish_plan(run_id: str, work: Awaitable[PlanOutcome]) -> dict[str, Any]:
    out = await work
    body: dict[str, Any] = {"run_id": run_id, "failed": out.failed}
    if out.plan is not None:
        return {**body, "type": "plan", "plan": plan_to_dict(out.plan)}
    return {**body, "type": "answer", "answer": out.answer}


def _plan_of(job: Job | None) -> dict[str, Any] | None:
    """The plan `job` returned when it is a finished plan job whose chief planned (_finish_plan): the studio's
    own `plan` event names only the subtasks and their roles, so this is where the whole plan is. None for
    any other job, a plan job still running, or a chief that answered instead."""
    if job is None or job.kind != "plan" or job.state != "done":
        return None
    result = job.task.result()
    plan = result.get("plan") if result.get("type") == "plan" else None
    return plan if isinstance(plan, dict) else None


def _ended(job: Job | None) -> bool:
    """Whether this server's job for a run failed or was cancelled: the run is over though its log has no
    run_done."""
    return job is not None and job.state in ("failed", "cancelled")


def _default_factory(cfg: AirlieConfig, k: int | None, topology: str | None = None) -> Any:
    from airlie.cli import _orchestrator, apply_k, select_topology
    from airlie.config import TOPOLOGY_KINDS

    if topology is not None and topology not in TOPOLOGY_KINDS:
        raise HTTPException(400, f"unknown topology '{topology}'; use one of {', '.join(TOPOLOGY_KINDS)}")
    cfg = select_topology(cfg, topology)
    cfg = apply_k(cfg if cfg.source_path is not None else cfg.model_copy(deep=True), k)
    return _orchestrator(cfg)


def create_app(
    cfg: AirlieConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    orchestrator_factory: Callable[..., Any] | None = None,
    api_key: str | None = None,
    story_job: Callable[..., Any] | None = None,
    initiative_job: Callable[..., Any] | None = None,
    auth_store: AuthStore | None = None,
    now: Callable[[], float] = time.time,
    next_dir: Path | None = None,
    tick_s: float | None = None,
    line_beat: LineFor | None = None,
) -> FastAPI:
    """`api_key`: when set, `Authorization: Bearer <key>` is the admin; every /v1 route needs a principal
    (the key, a Tailscale identity or a passkey session: airlie.auth.principal).
    `story_job` builds the job `POST /v1/airlie/stories` starts (default: airlie.story.factory.story_job);
    `initiative_job` the one the initiative routes start (default: airlie.story.factory.initiative_job).
    `auth_store` (default: observability.state_dir/auth.sqlite), `now` and `next_dir` (the Vue app's build,
    default NEXT_DIR), `tick_s` (the line's beat interval, default line.tick_s) and `line_beat` (replaces the
    line's beat, given the function that starts a lane's job) are for tests."""
    app = FastAPI(title="airlie", version="0.1.0")
    started_commit = (
        build_commit()
    )  # the code this process runs: a pull after it changes the checkout, not this
    client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(600.0, connect=10.0))
    factory = orchestrator_factory or _default_factory
    feed = Feed()
    app.state.feed = feed
    jobs = Jobs(feed.publish)
    ticker = LineTicker(cfg, jobs, commit=started_commit, tick_s=tick_s, beat=line_beat, now=now)
    watcher = Watcher(cfg.observability.state_dir.expanduser(), feed)
    app.state.watcher = watcher
    make_story = story_job or _story_job_factory()
    make_initiative = initiative_job or _initiative_job_factory()
    board = BoardStore(cfg.observability.state_dir.expanduser() / "board.sqlite", prefix=cfg.board.prefix)
    app.include_router(board_router(board, feed.publish))

    def repo_path(raw: str) -> Path:
        repo = Path(raw).expanduser().resolve()
        if not (repo / ".git").exists():
            raise HTTPException(400, f"{repo} is not a git repository")
        return repo

    def image_paths(raw: list[str]) -> list[Path]:
        images = [Path(p).expanduser().resolve() for p in raw]
        for p in images:
            if not p.is_file():
                raise HTTPException(400, f"image not found: {p}")
        return images

    store = auth_store or AuthStore(cfg.observability.state_dir.expanduser() / "auth.sqlite")
    resolver = Resolver(store, cfg.auth, api_key)

    @app.middleware("http")
    async def _require_principal(request: Request, call_next: Callable[..., Any]) -> Any:
        if request.url.path.startswith("/v1") and request.url.path not in PUBLIC_PATHS:
            try:
                principal = await resolver.resolve(request)
            except NotInvited:
                return JSONResponse({"error": "not invited"}, status_code=401)
            if principal is None:
                # the metrics tab's own phrase (AC6); every other /v1 route keeps 'not signed in'
                error = (
                    "missing or invalid API key"
                    if request.url.path.startswith("/v1/metrics")
                    else "not signed in"
                )
                return JSONResponse({"error": error}, status_code=401)
            request.state.principal = principal
        return await call_next(request)

    app.include_router(auth_router(store, resolver, cfg.auth, now=now))

    uploads = UploadStore(cfg.observability.state_dir.expanduser() / "uploads", cfg.chat.attachments, now=now)

    def owner_of(request: Request) -> Principal | None:
        return getattr(request.state, "principal", None)

    def may_see(principal: Principal | None, upload: Upload) -> bool:
        return principal is not None and (principal.role == "admin" or principal.user_id == upload.owner)

    @app.post("/v1/airlie/uploads", status_code=201)
    async def upload_file(
        request: Request, name: str = Query(min_length=1, max_length=1024)
    ) -> dict[str, Any]:
        """A Chat attachment: the raw bytes as the body. The kind is read from them; a body past its kind's
        limit is 413 and leaves nothing behind."""
        principal = owner_of(request)
        owner = principal.user_id if principal is not None else "nobody"
        try:
            saved = await uploads.save(name, request.stream(), owner)
        except UploadTooLarge as e:
            raise HTTPException(413, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return saved.public()

    @app.get("/v1/airlie/uploads/{upload_id}")
    async def get_upload(upload_id: str, request: Request) -> Response:
        """An attachment's bytes, to the person who uploaded it (or an admin); anyone else gets the 404 an
        unknown id gets. Only a picture or a video is shown inline: anything else downloads, so an uploaded
        page never runs as this site."""
        found = uploads.get(upload_id)
        if found is None or not may_see(owner_of(request), found):
            raise HTTPException(404, "no such upload")
        inline = found.kind in MEDIA
        return FileResponse(
            uploads.path(found),
            media_type=found.mime if inline else "application/octet-stream",
            filename=found.name,
            content_disposition_type="inline" if inline else "attachment",
            headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"},
        )

    chat_skills: dict[str, Skill] = {}
    if "chat" in cfg.roles and cfg.roles["chat"].skills is not None:  # a bad skill file stops the server here
        chat_skills = {s.name: s for s in load_chat_skills(cfg.roles["chat"].skills)}

    def upstream(name: str) -> tuple[str, str, dict[str, str], str | None]:
        """(engine base url, served model name, headers, prompt-or-None) for a role or a raw catalog model."""
        if name in cfg.roles:
            model, prompt = cfg.roles[name].model, prompt_text(cfg.roles[name].prompt)
        elif name in cfg.models:
            model, prompt = name, None
        else:
            known = ", ".join([*cfg.roles, *cfg.models])
            raise HTTPException(404, f"unknown model '{name}'; roles and catalog models: {known}")
        e = cfg.engines[cfg.engine_for_model(model)]
        headers = {"Authorization": f"Bearer {e.api_key}"} if e.api_key else {}
        return (e.base_url or "").rstrip("/"), e.served_name(model), headers, prompt

    next_root = (next_dir or NEXT_DIR).resolve()

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "roles": list(cfg.roles), "commit": started_commit, "line": ticker.row()}

    @app.get("/v1/events")
    async def events() -> StreamingResponse:
        """What changed, live: server-sent hints (`board` / `AIR-17`, `runs`, `jobs`) that a page answers by
        refetching. No history is kept; a reconnecting page refetches what it shows. Other processes' writes
        are noticed by the watcher, which this starts (and takes its baseline) before `: connected`."""
        return StreamingResponse(
            event_stream(feed, on_subscribed=watcher.ensure_running),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        served = [m for m in cfg.models if m not in cfg.roles]
        return {
            "object": "list",
            "data": [{"id": r, "object": "model", "owned_by": "airlie"} for r in [*cfg.roles, *served]],
        }

    def chat_prompt(prompt: str, skill: Any) -> str:
        """The chat role's prompt with the list of skills it can suggest, and the one run for this request."""
        if chat_skills:
            listing = "\n".join(f"- /{s.name}: {s.summary}" for s in chat_skills.values())
            prompt = f"{prompt.rstrip()}\n\n## Slash commands\n\n{listing}\n"
        if skill is None:
            return prompt
        found = chat_skills.get(str(skill))
        if found is None:
            known = ", ".join(chat_skills) or "none"
            raise HTTPException(404, f"unknown skill '{skill}'; skills: {known}")
        return f"{prompt.rstrip()}\n\n## Skill: /{found.name}\n\n{found.text.strip()}\n"

    @app.get("/v1/airlie/chat/attachments")
    async def attachment_limits() -> dict[str, Any]:
        """What the + may attach, so the composer refuses a too-large file before uploading it."""
        a = cfg.chat.attachments
        return {
            "max_per_message": a.max_per_message,
            "max_image_mb": a.max_image_mb,
            "max_video_mb": a.max_video_mb,
            "max_file_mb": a.max_file_mb,
        }

    @app.get("/v1/airlie/chat/skills")
    async def chat_skill_list() -> dict[str, Any]:
        return {
            "skills": [
                {"name": s.name, "summary": s.summary, "action": s.action} for s in chat_skills.values()
            ]
        }

    async def attach(body: dict[str, Any], request: Request) -> dict[str, Any]:
        """The chat body with every `airlie_upload` part turned into what the chat model can take."""
        msgs = list(body.get("messages") or [])
        limit = cfg.chat.attachments.max_per_message
        for m in msgs:
            parts = m.get("content") if isinstance(m, dict) else None
            if isinstance(parts, list) and sum(p.get("type") == "airlie_upload" for p in parts) > limit:
                raise HTTPException(400, f"a message may carry up to {limit} attachments")
        principal = owner_of(request)
        msgs = await asyncio.to_thread(  # decoding a picture or a video, or reading a PDF, blocks
            resolve,
            msgs,
            uploads,
            can_see=lambda u: may_see(principal, u),
            vision=cfg.roles["chat"].vision,
        )
        return {**body, "messages": msgs}

    @app.post("/v1/chat/completions")
    async def chat(body: dict[str, Any], request: Request) -> Any:
        name = str(body.get("model", ""))
        skill = body.get("skill")  # the Chat screen's slash command: ours, never the engine's
        if skill is not None:
            body = {k: v for k, v in body.items() if k != "skill"}
        base, served, headers, prompt = upstream(name)
        if name == "chat":
            prompt = chat_prompt(prompt or "", skill)
        elif skill is not None:
            raise HTTPException(400, "skills are for the chat role")
        if has_uploads(list(body.get("messages") or [])):
            if name != "chat":
                raise HTTPException(400, "attachments are for the chat role")
            body = await attach(body, request)
        msgs = list(body.get("messages") or [])
        if prompt is not None:
            if msgs and msgs[0].get("role") == "system":  # a client with its own harness (OpenCode, VS Code)
                # the rule is one sentence; the blank line that sets it off from the role prompt is ours
                bridge = "\n\n" + hub_text("client-bridge")
                msgs = [{"role": "system", "content": prompt.rstrip() + bridge}, *msgs]
            else:
                msgs = [{"role": "system", "content": prompt}, *msgs]
        out = {**body, "model": served, "messages": msgs}
        if name in cfg.roles:
            spec = cfg.roles[name]
            out.setdefault("max_tokens", spec.max_tokens)
            out.setdefault("chat_template_kwargs", {"enable_thinking": bool(spec.thinking)})
            # as HTTPEngine sends it, under the name vllm-mlx reads: a thinking role's reasoning is capped
            if spec.thinking and spec.thinking_budget:
                out.setdefault("thinking_token_budget", spec.thinking_budget)
        if out.get("stream"):
            req = client.build_request("POST", f"{base}/chat/completions", json=out, headers=headers)
            resp = await client.send(req, stream=True)

            async def gen() -> AsyncIterator[bytes]:
                repair = ToolCallStreamRepair()
                buf = b""
                try:
                    async for chunk in resp.aiter_bytes():
                        buf += chunk
                        while b"\n\n" in buf:
                            frame, buf = buf.split(b"\n\n", 1)
                            yield repair.frame(frame) + b"\n\n"
                    if buf:
                        yield repair.frame(buf)
                finally:
                    await resp.aclose()

            return StreamingResponse(gen(), status_code=resp.status_code, media_type="text/event-stream")
        r = await client.post(f"{base}/chat/completions", json=out, headers=headers)
        if r.status_code != 200:
            raise HTTPException(r.status_code, r.text[:500])
        return _no_call_stops(r.json())

    @app.post("/v1/airlie/run")
    async def run(req: RunRequest) -> dict[str, Any]:
        repo, images = repo_path(req.repo), image_paths(req.images)
        orch = factory(cfg, req.k, topology=req.topology) if req.topology else factory(cfg, req.k)
        if images:
            report = await orch.execute_task(req.goal, repo=repo, test_cmd=req.test_cmd, images=images)
        else:
            report = await orch.execute_task(req.goal, repo=repo, test_cmd=req.test_cmd)
        return {
            "run_id": report.run_id,
            "winner": report.winner,
            "final_answer": report.final_answer,
            "scores": report.scores,
            "wall_ms": report.wall_ms,
            "run_dir": str(report.run_dir.path),
            "teams": [
                {
                    "team_id": t.team_id,
                    "ok": t.ok,
                    "stop_reason": t.stop_reason,
                    "steps": t.steps,
                    "final_text": t.final_text,
                }
                for t in report.teams
            ],
        }

    @app.post("/v1/airlie/plans", status_code=202)
    async def start_plan(req: PlanRequest) -> dict[str, Any]:
        repo, images = repo_path(req.repo), image_paths(req.images)
        orch: StudioOrchestrator = factory(cfg, None, topology="studio")
        work = orch.plan(req.goal, repo=repo, test_cmd=req.test_cmd, images=images)
        rid = orch.run_dir.run_id
        job = jobs.start("plan", rid, orch.run_dir.events_path, _finish_plan(rid, work))
        return {"job_id": job.id, "kind": job.kind}

    @app.post("/v1/airlie/runs", status_code=202)
    async def start_run(req: StartRun) -> dict[str, Any]:
        repo, images = repo_path(req.repo), image_paths(req.images)
        if req.base is not None and (req.base.startswith("-") or rev_parse(repo, req.base) is None):
            raise HTTPException(400, f"base {req.base!r} is not a branch, tag or commit in {repo}")
        given: Plan | None = None
        if req.plan is not None:
            try:
                given = plan_from_dict(req.plan)
                check_plan(cfg, pin(given))
            except PlanError as e:
                raise HTTPException(400, f"plan rejected: {e}") from e
        # last: the orchestrator makes its run directory when it is built, so a rejected request leaves none
        orch: StudioOrchestrator = factory(cfg, None, topology="studio")
        work = orch.execute_task(
            req.goal,
            repo=repo,
            test_cmd=req.test_cmd,
            images=images,
            plan=given,
            base=req.base,
            synthesise=req.synthesise,
        )
        job = jobs.start("run", orch.run_dir.run_id, orch.run_dir.events_path, _finish_run(work))
        return {"job_id": job.id, "kind": job.kind}

    @app.post("/v1/airlie/stories", status_code=202)
    async def start_story(req: StoryRequest) -> dict[str, Any]:
        if (req.card is None) == (req.text is None):
            raise HTTPException(400, "pass exactly one of card or text")
        repo = (req.repo or "").strip() or None  # a blank repo is none, all the way to the story
        if req.card is not None and repo is not None:
            raise HTTPException(422, ISSUE_REPO)

        def running(job_id: str) -> bool:
            job = jobs.get(job_id)
            return job is not None and job.state == "running"

        try:
            job_id, events, work = await make_story(
                cfg, card=req.card, text=req.text, repo=repo, running=running
            )
        except StoryRunning as e:  # decided before the story is placed: its issue is read, not told or moved
            raise HTTPException(409, str(e)) from e
        except (StorySetupError, BoardError) as e:
            raise HTTPException(400, str(e)) from e
        try:
            job = jobs.start("story", job_id, events, work)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return {"job_id": job.id, "kind": job.kind}

    def job_running(job_id: str) -> bool:
        job = jobs.get(job_id)
        return job is not None and job.state == "running"

    async def start_turn(key: str) -> Response:
        """Start the specs phase's next piece for the initiative `key` (D15, N6): 202 with the job, 409 with
        the busy line while a story holds the lock (the comment stays on the card; the tick runs the turn)."""
        try:
            job_id, events, work = await make_initiative(cfg, key=key, running=job_running)
        except StoryBusy as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        except StoryRunning as e:
            raise HTTPException(409, str(e)) from e
        except BoardError as e:
            raise HTTPException(404, str(e)) from e
        except StorySetupError as e:
            raise HTTPException(400, str(e)) from e
        try:
            job = jobs.start("initiative", job_id, events, work)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return JSONResponse({"job_id": job.id, "kind": job.kind}, status_code=202)

    @app.post("/v1/airlie/initiatives", status_code=202)
    async def start_initiative(req: InitiativeRequest, request: Request) -> Response:
        """An initiative from a person's paragraph: the card is made and approved, and its first turn (the
        first question) starts at once. The card stands whatever the turn does: when a story holds the lock,
        or the turn cannot start, the answer says so beside the key and the tick takes over."""
        principal = owner_of(request)
        labels = [f"repo:{r}" for r in req.repos]
        if req.budget_hours is not None:
            labels.append(f"budget:{req.budget_hours:g}h")
        title = (req.title or req.text.strip().splitlines()[0]).strip()[:200]
        item = board.create(
            level="initiative",
            title=title,
            description=req.text,
            labels=labels,
            created_by=principal.name if principal is not None else "noah",
        )
        board.update(item.key, status="approved", by=principal.name if principal is not None else "person")
        feed.publish("board", item.key)
        answer = await start_turn(item.key)
        if answer.status_code == 202:
            return JSONResponse({"key": item.key, **json.loads(bytes(answer.body))}, status_code=202)
        if answer.status_code == 409 and isinstance(answer, JSONResponse):
            waiting = json.loads(bytes(answer.body))["error"]
            return JSONResponse({"key": item.key, "job_id": None, "waiting": waiting}, status_code=202)
        return answer

    @app.post("/v1/airlie/initiatives/{key}/turn", status_code=202)
    async def turn_initiative(key: str) -> Response:
        return await start_turn(key)

    @app.get("/v1/airlie/jobs")
    async def job_list() -> dict[str, Any]:
        return {"jobs": jobs.list()}

    @app.get("/v1/airlie/running")
    async def running_list() -> dict[str, Any]:
        """Every story and run in progress, whoever started it, newest activity first: the state directory
        says what the tick or a `task` command is doing; this server's running jobs stay listed however
        quiet (airlie.serve.running)."""
        live = frozenset(j["id"] for j in jobs.list() if j["state"] == "running")
        return {"running": in_progress(cfg.observability.state_dir.expanduser(), live=live, now=now())}

    @app.get("/v1/airlie/runs")
    async def run_list(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        return {"runs": recent_runs(cfg.observability.state_dir, limit)}

    @app.get("/v1/airlie/runs/{run_id}/tree")
    async def run_tree_get(run_id: str, since: int = Query(0, ge=0), wait: float = 0.0) -> dict[str, Any]:
        """The topology page's tree of a run, followed by long polls as a job is: with `wait` the reply is
        held until the run's log grows past `since` lines, the run ends, or `wait` seconds pass."""
        models = {name: role.model for name, role in cfg.roles.items()}
        deadline = time.monotonic() + max(0.0, min(wait, 300.0))
        while True:
            try:
                tree = run_tree(cfg.observability.state_dir.expanduser(), run_id, models=models)
            except RunNotFound as e:
                raise HTTPException(404, str(e)) from None
            job = jobs.get(run_id)
            tree["job"] = job.state if job is not None else None
            if job is not None and job.state in ("failed", "cancelled"):
                tree["state"] = job.state  # it ended without its run_done
            ended = tree["state"] != "running" or tree["job"] not in (None, "running")
            if tree["next"] > since or ended or time.monotonic() >= deadline:
                return tree
            await asyncio.sleep(0.25)

    @app.get("/v1/airlie/runs/{run_id}/flow")
    async def run_flow_get(run_id: str) -> dict[str, Any]:
        """A run of any topology as the workflow view draws it (airlie.serve.run_flow), with `job`: this
        server's job state for it, or None; and `plan`: the plan its finished plan job returned, for a person
        to approve before anything is built, or None."""
        models = {name: role.model for name, role in cfg.roles.items()}
        state, job = cfg.observability.state_dir.expanduser(), jobs.get(run_id)
        try:
            flow = run_flow(state, run_id, models=models, ended=_ended(job))
        except RunNotFound as e:
            raise HTTPException(404, str(e)) from None
        flow["job"] = job.state if job is not None else None
        flow["plan"] = _plan_of(job)
        return flow

    @app.get("/v1/airlie/runs/{run_id}/flow/{node_id}/{job_id}")
    async def run_job_get(run_id: str, node_id: str, job_id: str) -> dict[str, Any]:
        state = cfg.observability.state_dir.expanduser()
        try:
            return run_job_detail(state, run_id, node_id, job_id, ended=_ended(jobs.get(run_id)))
        except RunNotFound as e:
            raise HTTPException(404, str(e)) from None

    @app.get("/v1/airlie/stories")
    async def story_list(
        page: int = Query(1, ge=1),
        size: int | None = Query(None, ge=1, le=100),
        stage: str | None = None,
        repo: str | None = None,
        blocked: bool | None = None,
        q: str | None = None,
        sort: Literal["activity", "key", "stage"] = "activity",
        active_from: float | None = None,
        active_to: float | None = None,
    ) -> dict[str, Any]:
        if stage is not None and stage not in STAGES:
            raise HTTPException(422, f"unknown stage {stage!r}; use one of {', '.join(STAGES)}")
        rows = stories(cfg.observability.state_dir)
        return story_page(
            rows,
            page=page,
            size=size,
            stage=stage,
            repo=repo,
            blocked=blocked,
            q=q,
            sort=sort,
            active_from=active_from,
            active_to=active_to,
        )

    @app.get("/v1/airlie/stories/{key}/flow")
    async def story_flow_get(key: str) -> dict[str, Any]:
        try:
            return story_flow(cfg.observability.state_dir.expanduser(), key)
        except StoryNotFound as e:
            raise HTTPException(404, str(e)) from None

    @app.get("/v1/airlie/stories/{key}/flow/{node_id}/{job_id}")
    async def story_job_get(key: str, node_id: str, job_id: str) -> dict[str, Any]:
        try:
            return job_detail(cfg.observability.state_dir.expanduser(), key, node_id, job_id)
        except StoryNotFound as e:
            raise HTTPException(404, str(e)) from None

    @app.post("/v1/airlie/line")
    async def line_switch(req: LineRequest) -> dict[str, Any]:
        """Pause or resume the line (`<state_dir>/line.paused`, read at each beat's top): the line row."""
        set_paused(cfg.observability.state_dir.expanduser(), req.paused)
        return ticker.row()

    @app.get("/v1/airlie/line/stats")
    async def line_stats_get(since: str | None = Query(None)) -> dict[str, Any]:
        """The numbers that judge the line (planning-line P5b, D9): the unattended rate, blocks by class and
        kind, hours against estimates, the amigos' objections, the budgets' overruns. `since` is a date or
        epoch seconds."""
        try:
            floor = parse_since(since) if since else None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return await asyncio.to_thread(
            line_stats,
            cfg.observability.state_dir.expanduser(),
            board,
            since=floor,
            roles=frozenset(cfg.roles),
        )

    @app.get("/v1/airlie/epics")
    async def epic_list() -> dict[str, Any]:
        """The epics the line has planned (or tried to), from their records: key, title, stage, stories."""
        return {"epics": epics(cfg.observability.state_dir.expanduser())}

    @app.get("/v1/airlie/epics/{key}/flow")
    async def epic_flow_get(key: str) -> dict[str, Any]:
        """One epic: its plan, its stories with their board statuses and pipeline stages, its re-plans."""
        state = cfg.observability.state_dir.expanduser()
        statuses = {story.key: story.status for story in board.list(level="story")}
        try:
            return epic_flow(state, key, statuses)
        except StoryNotFound as e:
            raise HTTPException(404, str(e)) from None

    @app.get("/v1/airlie/initiatives")
    async def initiative_list() -> dict[str, Any]:
        """The initiatives the specs phase has a record for: stage, epics, hours, what each waits on."""
        return {"initiatives": initiatives(cfg.observability.state_dir.expanduser(), board)}

    @app.get("/v1/airlie/initiatives/{key}/flow")
    async def initiative_flow_get(key: str) -> dict[str, Any]:
        """One initiative: the dialogue, the consultations, the epics with their prices, verdicts and mockup
        pages, the hours already queued, and what it waits on."""
        try:
            return initiative_flow(cfg.observability.state_dir.expanduser(), board, key)
        except StoryNotFound as e:
            raise HTTPException(404, str(e)) from None

    @app.get("/v1/airlie/initiatives/{key}/mockups/{epic}/{file}")
    async def mockup_page(key: str, epic: str, file: str) -> Response:
        """A mockup page, as a file, locked down: only `option-<1-9>.html` of an epic id under the
        initiative's own directory is ever served (anything else is a 404), with a policy that lets the page
        run and fetch nothing."""
        if not OPTION_FILE.fullmatch(file) or not _EPIC_ID.fullmatch(epic):
            raise HTTPException(404, "no such mockup")
        try:
            folder = mockup_dir(cfg.observability.state_dir.expanduser(), key, epic)
        except ValueError:
            raise HTTPException(404, "no such mockup") from None
        page = folder / file
        if page.is_symlink() or not page.is_file():
            raise HTTPException(404, "no such mockup")
        return FileResponse(page, media_type="text/html", headers=MOCKUP_HEADERS)

    @app.post("/v1/airlie/epics/{key}/mockup")
    async def pick_mockup(key: str, req: PickRequest) -> dict[str, Any]:
        """A person's pick (D4): written into the epic card's block and label and the initiative's log. 404
        for an item that is no epic, 400 for an option the epic has no page for."""
        try:
            item = board.get(key)
        except KeyError:
            raise HTTPException(404, f"no item {key} on the board") from None
        if item.level != "epic":
            raise HTTPException(404, f"{key} is not an epic")
        try:
            spec = record_pick(board, cfg.observability.state_dir.expanduser(), key, req.option)
        except StoryError as e:
            raise HTTPException(400, str(e)) from e
        feed.publish("board", key)
        return spec.to_dict()

    @app.get("/v1/airlie/stories/{key}/diagnosis")
    async def story_diagnosis_get(key: str, n: int | None = Query(None, ge=1)) -> dict[str, Any]:
        """The diagnosis a block wrote (planning-line P5a): the class by rules, Muse's reading, what happens
        next, its Markdown and the paths of the evidence. 404 when the story has no diagnosis. The key names a
        directory of the state directory by name and nothing else."""
        state = cfg.observability.state_dir.expanduser()
        if not _STORY_DIR.fullmatch(key) or ".." in key:
            raise HTTPException(404, f"no diagnosis for {key!r}")
        d = read_diagnosis(state, key, n) if n is not None else latest(state, key)
        if d is None:
            raise HTTPException(404, f"{key} has no diagnosis" + (f" for block {n}" if n else ""))
        return diagnosis_json(d)

    @app.get("/v1/airlie/repos")
    async def repo_list() -> dict[str, Any]:
        return {"repos": repos(cfg)}

    @app.get("/v1/airlie/repos/names")
    async def repo_name_list() -> dict[str, Any]:
        labels = [label for item in board.list() for label in item.labels]
        return {"repos": await asyncio.to_thread(repo_names, cfg, labels)}

    @app.get("/v1/airlie/jobs/{job_id}")
    async def job_status(job_id: str, since: int = Query(0, ge=0), wait: float = 0.0) -> dict[str, Any]:
        out = await jobs.status(job_id, since=since, wait=min(wait, 300.0))
        if out is None:
            raise HTTPException(404, f"no job '{job_id}' in this server (jobs do not survive a restart)")
        return out

    @app.delete("/v1/airlie/jobs/{job_id}")
    async def job_cancel(job_id: str) -> dict[str, Any]:
        if jobs.get(job_id) is None:
            raise HTTPException(404, f"no job '{job_id}'")
        return {"job_id": job_id, "cancelled": jobs.cancel(job_id)}

    METRIC_SLICES = ("role", "model", "topology", "task", "agent")
    METRIC_NAMES = ("generation_tps", "prefill_tps")
    """The slice and metric names the metrics tab's API accepts; the web layer (layer 4) uses the same set."""

    def _metric_time(raw: str | None, name: str) -> float | None:
        """An ISO8601 timestamp or epoch seconds, or None when absent."""
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            from datetime import datetime

            return datetime.fromisoformat(raw).timestamp()

    def _metric_bucket_s(span: float) -> float:
        """The bucket width a range implies: 4h->5min, 24h->1h, 7d->6h, 30d->1d, all else 1d."""
        if span <= 4 * 3600:
            return 300.0
        if span <= 24 * 3600:
            return 3600.0
        if span <= 7 * 86400:
            return 6 * 3600.0
        return 86400.0

    def _metrics_auth(request: Request) -> None:
        """The metrics routes' own 401: the global middleware says 'not signed in', which the other
        serve/auth tests assert, so the metrics tab gets its own phrase here."""
        if getattr(request.state, "principal", None) is None:
            raise HTTPException(401, "missing or invalid API key")

    @app.get("/v1/metrics/summary")
    async def metrics_summary_route(
        request: Request,
        from_: str | None = Query(None, alias="from"),
        to: str | None = None,
        slice_: str = Query("role", alias="slice"),
    ) -> dict[str, Any]:
        _metrics_auth(request)
        if slice_ not in METRIC_SLICES:
            raise HTTPException(422, f"unknown slice '{slice_}'; use one of {', '.join(METRIC_SLICES)}")
        f = _metric_time(from_, "from")
        t = _metric_time(to, "to")
        if f is None or t is None:
            raise HTTPException(422, "from and to are required")
        if t <= f:
            raise HTTPException(422, "to must be after from")
        return metrics_summary(cfg.observability.state_dir.expanduser(), f, t, slice_)

    @app.get("/v1/metrics/series")
    async def metrics_series_route(
        request: Request,
        from_: str | None = Query(None, alias="from"),
        to: str | None = None,
        slice_: str = Query("role", alias="slice"),
        metric: str = "generation_tps",
    ) -> list[dict[str, Any]]:
        _metrics_auth(request)
        if slice_ not in METRIC_SLICES:
            raise HTTPException(422, f"unknown slice '{slice_}'; use one of {', '.join(METRIC_SLICES)}")
        if metric not in METRIC_NAMES:
            raise HTTPException(422, f"unknown metric '{metric}'; use one of {', '.join(METRIC_NAMES)}")
        f = _metric_time(from_, "from")
        t = _metric_time(to, "to")
        if f is None or t is None:
            raise HTTPException(422, "from and to are required")
        if t <= f:
            raise HTTPException(422, "to must be after from")
        return metrics_series(
            cfg.observability.state_dir.expanduser(), metric, slice_, f, t, _metric_bucket_s(t - f)
        )

    @app.on_event("startup")
    async def startup() -> None:
        ticker.start()  # the line's beat, every line.tick_s while serve is up (line.serve_ticks)

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await ticker.stop()
        watcher.stop()
        await client.aclose()
        board.close()
        store.close()

    api_prefixes = ("v1", "health", "docs", "redoc", "openapi.json")

    @app.get("/next", include_in_schema=False)
    @app.get("/next/{path:path}", include_in_schema=False)
    async def next_redirect(request: Request, path: str = "") -> RedirectResponse:
        """The app's old home: the same path without the prefix, the query kept."""
        query = request.url.query
        # one leading slash, whatever came after /next ("//host" and "/\\host" would leave the site)
        tail = path.lstrip("/\\")
        return RedirectResponse("/" + tail + (f"?{query}" if query else ""), status_code=308)

    @app.exception_handler(StarletteHTTPException)
    async def web_app(request: Request, exc: StarletteHTTPException) -> Response:
        """The Vue app: a GET that matched no route is a file inside its build (a hashed one under assets/ is
        cached for good) or else index.html, which the app's router reads. It needs no sign-in: every call the
        app makes goes to /v1, which keeps the guard. An exception handler rather than a last route, so routes
        added after this one still win; an unknown API path stays a JSON 404 and `/app/...` (the retired
        app) is gone."""
        path = request.url.path.lstrip("/")
        first = path.split("/", 1)[0]
        no_page = exc.status_code != 404 or request.method not in ("GET", "HEAD") or exc.detail != "Not Found"
        if no_page or first in api_prefixes or first == "app":
            return await http_exception_handler(request, exc)
        index = next_root / "index.html"
        if not index.is_file():
            return JSONResponse({"error": NEXT_NOT_BUILT}, status_code=503)
        try:
            target = (next_root / path).resolve()
            served = target if target.is_relative_to(next_root) and target.is_file() else None
        except (OSError, ValueError):  # a NUL byte, or a name too long for the filesystem: no such file
            served = None
        if served is not None and served != index.resolve():
            cache = IMMUTABLE if served.is_relative_to(next_root / "assets") else "no-cache"
            return FileResponse(served, headers={"Cache-Control": cache})
        if first == "assets" or "." in path.rsplit("/", 1)[-1]:
            # a file that is not there (an old tab's chunk after a deploy, favicon.ico) is a 404, not the page
            return await http_exception_handler(request, exc)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app
