"""`task serve`: the client-agnostic front door. Any OpenAI-compatible client (Claude Code, Qwen Code, curl)
can talk to a role by name; `/v1/airlie/run` launches a parallel team run."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from airlie.config import AirlieConfig
from airlie.skills import prompt_text


class RunRequest(BaseModel):
    goal: str
    repo: str
    k: int | None = None
    test_cmd: str | None = None
    topology: str | None = None
    images: list[str] = []


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
) -> FastAPI:
    """`api_key`: when set, every /v1 route needs `Authorization: Bearer <key>` (use it before a tunnel)."""
    app = FastAPI(title="airlie", version="0.1.0")
    client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(600.0, connect=10.0))
    factory = orchestrator_factory or _default_factory

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
        if prompt is not None and (not msgs or msgs[0].get("role") != "system"):
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
                try:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                finally:
                    await resp.aclose()

            return StreamingResponse(gen(), status_code=resp.status_code, media_type="text/event-stream")
        r = await client.post(f"{base}/chat/completions", json=out, headers=headers)
        if r.status_code != 200:
            raise HTTPException(r.status_code, r.text[:500])
        return r.json()

    @app.post("/v1/airlie/run")
    async def run(req: RunRequest) -> dict[str, Any]:
        repo = Path(req.repo).expanduser().resolve()
        if not (repo / ".git").exists():
            raise HTTPException(400, f"{repo} is not a git repository")
        orch = factory(cfg, req.k, topology=req.topology) if req.topology else factory(cfg, req.k)
        images = [Path(p).expanduser().resolve() for p in req.images]
        for p in images:
            if not p.is_file():
                raise HTTPException(400, f"image not found: {p}")
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

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await client.aclose()

    return app
