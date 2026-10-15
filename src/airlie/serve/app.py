"""`task serve`: the client-agnostic front door. Any OpenAI-compatible client (Claude Code, Qwen Code, curl)
can talk to a role by name; `/v1/airlie/run` launches a parallel team run. The jobs API starts a studio plan
(`/v1/airlie/plans`), a studio run (`/v1/airlie/runs`) or a story (`/v1/airlie/stories`) in the background; a
client follows the job by long polls on `GET /v1/airlie/jobs/{id}?since=N&wait=S` and cancels it with
`DELETE`."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from airlie.config import AirlieConfig
from airlie.report import RunReport
from airlie.serve.jobs import Jobs
from airlie.skills import prompt_text
from airlie.story.board import BoardError
from airlie.story.model import StorySetupError
from airlie.teams.worktree import rev_parse
from airlie.topology.studio import PlanOutcome, StudioOrchestrator, check_plan
from airlie.topology.studio_plan import Plan, PlanError, pin, plan_from_dict, plan_to_dict


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
    """A whole (not streamed) reply, with "stop" for the finish_reason "tool_calls" of a choice that holds no
    call with an id and a name, as ToolCallStreamRepair does for a stream."""
    choices = reply.get("choices") if isinstance(reply, dict) else None
    for choice in choices or []:
        calls = (choice.get("message") or {}).get("tool_calls") or []
        whole = [c for c in calls if c.get("id") and (c.get("function") or {}).get("name")]
        if choice.get("finish_reason") == "tool_calls" and not whole:
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


class StoryRequest(BaseModel):
    card: str | None = None
    text: str | None = None
    repo: str | None = None
    test_cmd: str | None = None


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
) -> FastAPI:
    """`api_key`: when set, every /v1 route needs `Authorization: Bearer <key>` (use it before a tunnel).
    `story_job` builds the job `POST /v1/airlie/stories` starts (default: airlie.story.factory.story_job)."""
    app = FastAPI(title="airlie", version="0.1.0")
    client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(600.0, connect=10.0))
    factory = orchestrator_factory or _default_factory
    jobs = Jobs()
    make_story = story_job or _story_job_factory()

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

    if api_key:

        @app.middleware("http")
        async def _require_key(request: Request, call_next: Callable[..., Any]) -> Any:
            if request.url.path.startswith("/v1"):
                auth = request.headers.get("authorization", "")
                if auth != f"Bearer {api_key}":
                    return JSONResponse({"error": "invalid or missing API key"}, status_code=401)
            return await call_next(request)

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

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "roles": list(cfg.roles)}

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        served = [m for m in cfg.models if m not in cfg.roles]
        return {
            "object": "list",
            "data": [{"id": r, "object": "model", "owned_by": "airlie"} for r in [*cfg.roles, *served]],
        }

    @app.post("/v1/chat/completions")
    async def chat(body: dict[str, Any]) -> Any:
        name = str(body.get("model", ""))
        base, served, headers, prompt = upstream(name)
        msgs = list(body.get("messages") or [])
        if prompt is not None:
            if msgs and msgs[0].get("role") == "system":  # a client with its own harness (OpenCode, VS Code)
                bridge = (
                    "\n\nYou are running inside a client that provides its own instructions and tools below; "
                    "use the tools offered in this conversation, not the tool names mentioned above."
                )
                msgs = [{"role": "system", "content": prompt.rstrip() + bridge}, *msgs]
            else:
                msgs = [{"role": "system", "content": prompt}, *msgs]
        out = {**body, "model": served, "messages": msgs}
        if name in cfg.roles:
            spec = cfg.roles[name]
            out.setdefault("max_tokens", spec.max_tokens)
            out.setdefault("chat_template_kwargs", {"enable_thinking": bool(spec.thinking)})
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
        try:
            job_id, events, work = await make_story(
                cfg, card=req.card, text=req.text, repo=req.repo, test_cmd=req.test_cmd
            )
        except (StorySetupError, BoardError) as e:
            raise HTTPException(400, str(e)) from e
        try:
            job = jobs.start("story", job_id, events, work)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return {"job_id": job.id, "kind": job.kind}

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

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await client.aclose()

    return app
