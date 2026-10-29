"""Kind registry: payload → serve call → hub result."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from airlie.config.schema import BridgeSpec
from airlie.serve.bridge.airlie_client import ServeClient, ServeJobNotFound
from airlie.serve.bridge.clock import Clock, RealClock
from airlie.serve.bridge.errors import BridgeError
from airlie.serve.bridge.payload import assert_data_payload
from airlie.serve.bridge.repos import resolve_repo

log = logging.getLogger(__name__)

HUB_RESULT_KEYS = frozenset({"ok", "final_answer", "branch", "tests"})


def map_run_result(serve_result: dict[str, Any]) -> dict[str, Any]:
    """Drop Mac-only fields (report) for the hub contract."""
    if serve_result.get("type") == "answer":
        return {"ok": not serve_result.get("failed", False), "final_answer": serve_result.get("answer", "")}
    out: dict[str, Any] = {}
    for key in HUB_RESULT_KEYS:
        if key in serve_result:
            out[key] = serve_result[key]
    if "ok" not in out and "failed" in serve_result:
        out["ok"] = not serve_result["failed"]
    return out


def default_test_cmd(spec: BridgeSpec) -> str | None:
    cmds = [c.strip() for c in spec.test_cmds if c.strip()]
    return cmds[0] if cmds else None


@dataclass
class KindOutcome:
    status: str
    result: dict[str, Any] | None = None
    error_message: str | None = None


class KindHandler(Protocol):
    async def run(self, payload: dict[str, Any]) -> KindOutcome: ...


class StudioKind:
    """POST /v1/airlie/runs with plan omitted (None), poll to terminal, map result for the hub."""

    def __init__(
        self,
        bridge: BridgeSpec,
        serve: ServeClient,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._bridge = bridge
        self._serve = serve
        self._clock = clock or RealClock()

    async def run(self, payload: dict[str, Any]) -> KindOutcome:
        data = assert_data_payload(payload)
        goal = data.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            return KindOutcome("failed", error_message="studio payload requires a non-empty goal")
        repo_key = data.get("repo_key")
        if repo_key is not None and not isinstance(repo_key, str):
            return KindOutcome("failed", error_message="repo_key must be a string")
        try:
            repo = resolve_repo(self._bridge, repo_key if isinstance(repo_key, str) else None)
        except BridgeError as e:
            return KindOutcome("failed", error_message=str(e))

        body: dict[str, Any] = {
            "goal": goal.strip(),
            "repo": str(repo),
            "plan": None,
        }
        test_cmd = default_test_cmd(self._bridge)
        if test_cmd is not None:
            body["test_cmd"] = test_cmd
        for key in ("images", "base", "synthesise"):
            if key in data:
                body[key] = data[key]

        try:
            job_id = await self._serve.start_run(body)
        except BridgeError as e:
            return KindOutcome("failed", error_message=str(e))

        log.info("studio serve job %s started", job_id)
        while True:
            try:
                snap = await self._serve.poll_job(job_id, wait=0.0)
            except ServeJobNotFound:
                return KindOutcome(
                    "failed",
                    error_message=f"serve lost job {job_id} (404 while polling)",
                )
            state = str(snap.get("state", ""))
            if state == "running":
                await self._clock.sleep(self._bridge.job_poll_s)
                continue
            if state == "done":
                raw = snap.get("result")
                if not isinstance(raw, dict):
                    return KindOutcome("failed", error_message="serve job finished without a result object")
                return KindOutcome("done", result=map_run_result(raw))
            if state in ("failed", "cancelled"):
                err = snap.get("error") or f"serve job {state}"
                return KindOutcome("failed", error_message=str(err))
            return KindOutcome("failed", error_message=f"unexpected serve state {state!r}")


def handler_for(
    kind: str,
    bridge: BridgeSpec,
    serve: ServeClient,
    *,
    clock: Clock | None = None,
) -> KindHandler:
    if kind not in bridge.kinds:
        raise BridgeError(f"kind {kind!r} is not in bridge.kinds")
    if kind == "studio":
        return StudioKind(bridge, serve, clock=clock)
    raise BridgeError(f"no handler for kind {kind!r}")
