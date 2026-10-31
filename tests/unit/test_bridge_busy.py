"""busy_reason: the bridge claims only while no story lane is held and nothing else used a model lately."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from airlie.config import load_config
from airlie.serve.bridge.busy import busy_reason
from airlie.story.state import lane_lock

ROOT = Path(__file__).resolve().parents[2]
NOW = 1_800_000_000.0


def _cfg(tmp_path: Path) -> Any:
    cfg = load_config(ROOT / "airlie.yaml", local_path=ROOT / "no-local-overrides.yaml")
    cfg.observability.state_dir = tmp_path
    return cfg


class FakeStatus(httpx.AsyncBaseTransport):
    def __init__(
        self, models: list[dict[str, Any]] | None = None, *, error: bool = False, status: int = 200
    ) -> None:
        self.models = models or []
        self.error = error
        self.status = status
        self.calls = 0
        self.timeouts: list[Any] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.timeouts.append(request.extensions.get("timeout"))
        if self.error:
            raise httpx.ConnectError("refused", request=request)
        assert request.url.path == "/v1/status"
        return httpx.Response(self.status, json={"model_manager": {"models": self.models}})


def _probe(cfg: Any, t: FakeStatus, *, own: float = 0.0) -> str | None:
    async def go() -> str | None:
        async with httpx.AsyncClient(transport=t) as http:
            return await busy_reason(cfg, http=http, now=NOW, own_last_used=own)

    return asyncio.run(go())


def test_build_lock_held_is_busy(tmp_path: Path) -> None:
    t = FakeStatus()
    with lane_lock(tmp_path / "stories", "build") as held:
        assert held
        assert _probe(_cfg(tmp_path), t) == "story build lane is running"
    assert t.calls == 0


def test_plan_lock_held_is_busy(tmp_path: Path) -> None:
    t = FakeStatus()
    with lane_lock(tmp_path / "stories", "plan") as held:
        assert held
        assert _probe(_cfg(tmp_path), t) == "story plan lane is running"
    assert t.calls == 0


def test_recent_model_use_is_busy(tmp_path: Path) -> None:
    t = FakeStatus([{"id": "generalist", "loaded": True, "last_used_at": NOW - 14}])
    reason = _probe(_cfg(tmp_path), t)
    assert reason is not None
    assert "generalist" in reason and "14 s" in reason


def test_old_model_use_is_idle(tmp_path: Path) -> None:
    t = FakeStatus([{"id": "generalist", "loaded": True, "last_used_at": NOW - 121}])
    assert _probe(_cfg(tmp_path), t) is None


def test_own_job_does_not_count(tmp_path: Path) -> None:
    t = FakeStatus([{"id": "generalist", "loaded": True, "last_used_at": NOW - 5}])
    assert _probe(_cfg(tmp_path), t, own=NOW - 4) is None


def test_unreachable_engine_is_idle(tmp_path: Path) -> None:
    assert _probe(_cfg(tmp_path), FakeStatus(error=True)) is None


def test_non_200_is_idle(tmp_path: Path) -> None:
    t = FakeStatus([{"id": "g", "loaded": True, "last_used_at": NOW - 1}], status=500)
    assert _probe(_cfg(tmp_path), t) is None


def test_status_probe_times_out_in_3s(tmp_path: Path) -> None:
    t = FakeStatus([])
    _probe(_cfg(tmp_path), t)
    assert t.timeouts
    assert all(x["read"] == 3.0 for x in t.timeouts)


def test_unloaded_or_null_last_used_is_ignored(tmp_path: Path) -> None:
    t = FakeStatus(
        [
            {"id": "a", "loaded": True, "last_used_at": None},
            {"id": "b", "loaded": False, "last_used_at": NOW - 1},
        ]
    )
    assert _probe(_cfg(tmp_path), t) is None


def test_missing_stories_dir_is_idle(tmp_path: Path) -> None:
    state = tmp_path / "fresh"
    assert _probe(_cfg(state), FakeStatus()) is None
    assert not (state / "stories").exists()
