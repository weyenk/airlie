"""busy_reason: the bridge claims only while no story lane is held and nothing else used a model lately."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from airlie.config import load_config
from airlie.serve.bridge.busy import busy_reason, reason_key
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
        self.auth: dict[str, str | None] = {}  # host:port → the Authorization header it was sent
        self.paths: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.timeouts.append(request.extensions.get("timeout"))
        self.auth[f"{request.url.host}:{request.url.port}"] = request.headers.get("authorization")
        self.paths.append(request.url.path)
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


# ---- requests in flight (patches/active_requests.py) ----


def test_a_request_in_flight_is_busy_however_old_its_start(tmp_path: Path) -> None:
    """vllm-mlx stamps last_used_at only when a request starts and ends: a long one looks idle halfway."""
    t = FakeStatus([{"id": "generalist", "loaded": True, "last_used_at": NOW - 500, "active_requests": 1}])
    assert _probe(_cfg(tmp_path), t) == "model generalist has 1 request in flight on engine main"


def test_a_request_in_flight_counts_even_just_after_the_bridges_own_job(tmp_path: Path) -> None:
    """The bridge probes only with no job of its own running: a request in flight is someone else's."""
    t = FakeStatus([{"id": "generalist", "loaded": True, "last_used_at": NOW - 1, "active_requests": 2}])
    assert (
        _probe(_cfg(tmp_path), t, own=NOW - 1) == "model generalist has 2 requests in flight on engine main"
    )


def test_nothing_in_flight_and_an_old_use_is_idle(tmp_path: Path) -> None:
    t = FakeStatus([{"id": "generalist", "loaded": True, "last_used_at": NOW - 500, "active_requests": 0}])
    assert _probe(_cfg(tmp_path), t) is None


def test_an_unpatched_engine_falls_back_to_the_last_use(tmp_path: Path) -> None:
    recent = FakeStatus([{"id": "generalist", "loaded": True, "last_used_at": NOW - 14}])
    assert _probe(_cfg(tmp_path), recent) == "model generalist used 14 s ago on engine main"
    old = FakeStatus([{"id": "generalist", "loaded": True, "last_used_at": NOW - 500}])
    assert _probe(_cfg(tmp_path), old) is None


def test_the_reason_key_drops_only_the_age() -> None:
    assert reason_key("model g used 14 s ago on engine main") == reason_key(
        "model g used 9 s ago on engine main"
    )
    assert reason_key("model g used 14 s ago on engine main") != reason_key(
        "model h used 14 s ago on engine main"
    )
    assert reason_key("story build lane is running") == "story build lane is running"


# ---- engine keys and roots ----


def test_the_probe_sends_each_engines_api_key(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.engines["main"] = cfg.engines["main"].model_copy(update={"api_key": "sekrit"})
    t = FakeStatus([])
    _probe(cfg, t)
    assert t.auth == {"127.0.0.1:8080": "Bearer sekrit", "127.0.0.1:8081": None}


def test_a_trailing_slash_after_v1_is_the_same_root(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.engines["main"] = cfg.engines["main"].model_copy(update={"base_url": "http://127.0.0.1:8080/v1/"})
    t = FakeStatus([])
    _probe(cfg, t)
    assert sorted(t.auth) == ["127.0.0.1:8080", "127.0.0.1:8081"]
    assert t.paths == ["/v1/status", "/v1/status"]
