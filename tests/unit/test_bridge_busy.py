"""busy_reason: the bridge claims only while no story lane is held and nothing else used a model lately."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from airlie.config import load_config
from airlie.config.schema import EngineSpec
from airlie.serve.bridge.busy import busy_reason, reason_key
from airlie.story.state import lane_lock

ROOT = Path(__file__).resolve().parents[2]
NOW = 1_800_000_000.0


def _cfg(tmp_path: Path) -> Any:
    """The committed config's two probed shapes: the `main` mlx-vlm engine serving `generalist` and the
    `decision` one."""
    cfg = load_config(ROOT / "airlie.yaml", local_path=ROOT / "no-local-overrides.yaml")
    cfg.observability.state_dir = tmp_path
    main = EngineSpec(kind="mlx_vlm", port=8080, models=["generalist"], base_url="http://127.0.0.1:8080/v1")
    decision = EngineSpec(
        kind="mlx_vlm",
        port=8081,
        models=["decider_2b"],
        decision=True,
        base_url="http://127.0.0.1:8081/v1",
    )
    cfg.engines = {"main": main, "decision": decision}
    return cfg


def _metrics(in_flight: int, last: float | None) -> dict[str, Any]:
    return {"latest": None, "recent": [], "summary": {"in_flight": in_flight, "last_request_at": last}}


class FakeEngines(httpx.AsyncBaseTransport):
    """Serve's /health and every engine's /v1/metrics."""

    def __init__(
        self,
        metrics: Any = None,
        *,
        error: bool = False,
        status: int = 200,
        health: int | str = 200,
    ) -> None:
        self.metrics = _metrics(0, None) if metrics is None else metrics  # the /v1/metrics body
        self.health = health  # an HTTP status, or "down" for a connection error
        self.health_calls = 0
        self.health_auth: str | None = None
        self.health_timeouts: list[Any] = []
        self.error = error
        self.status = status
        self.calls = 0
        self.timeouts: list[Any] = []
        self.auth: dict[str, str | None] = {}  # host:port → the Authorization header it was sent
        self.paths: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            self.health_calls += 1
            self.health_auth = request.headers.get("authorization")
            self.health_timeouts.append(request.extensions.get("timeout"))
            if self.health == "down":
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(int(self.health), json={"ok": True})
        self.calls += 1
        self.timeouts.append(request.extensions.get("timeout"))
        self.auth[f"{request.url.host}:{request.url.port}"] = request.headers.get("authorization")
        self.paths.append(request.url.path)
        if self.error:
            raise httpx.ConnectError("refused", request=request)
        assert request.url.path == "/v1/metrics"
        return httpx.Response(self.status, json=self.metrics)


def _probe(cfg: Any, t: FakeEngines, *, own: float = 0.0, serve_key: str | None = None) -> str | None:
    async def go() -> str | None:
        async with httpx.AsyncClient(transport=t) as http:
            return await busy_reason(cfg, http=http, now=NOW, own_last_used=own, serve_api_key=serve_key)

    return asyncio.run(go())


def test_build_lock_held_is_busy(tmp_path: Path) -> None:
    t = FakeEngines()
    with lane_lock(tmp_path / "stories", "build") as held:
        assert held
        assert _probe(_cfg(tmp_path), t) == "story build lane is running"
    assert t.calls == 0


def test_plan_lock_held_is_busy(tmp_path: Path) -> None:
    t = FakeEngines()
    with lane_lock(tmp_path / "stories", "plan") as held:
        assert held
        assert _probe(_cfg(tmp_path), t) == "story plan lane is running"
    assert t.calls == 0


def test_recent_engine_use_is_busy(tmp_path: Path) -> None:
    reason = _probe(_cfg(tmp_path), FakeEngines(_metrics(0, NOW - 14)))
    assert reason == "model generalist used 14 s ago on engine main"


def test_old_or_never_used_engine_is_idle(tmp_path: Path) -> None:
    assert _probe(_cfg(tmp_path), FakeEngines(_metrics(0, NOW - 121))) is None
    assert _probe(_cfg(tmp_path), FakeEngines(_metrics(0, None))) is None


def test_own_job_does_not_count(tmp_path: Path) -> None:
    assert _probe(_cfg(tmp_path), FakeEngines(_metrics(0, NOW - 5)), own=NOW - 4) is None
    assert _probe(_cfg(tmp_path), FakeEngines(_metrics(0, NOW - 14)), own=NOW - 14) is None


def test_the_newest_use_across_engines_names_the_busy_model(tmp_path: Path) -> None:
    class Staggered(FakeEngines):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.port == 8081:
                self.metrics = _metrics(0, NOW - 3)
            else:
                self.metrics = _metrics(0, NOW - 14)
            return await super().handle_async_request(request)

    assert _probe(_cfg(tmp_path), Staggered()) == "model decider_2b used 3 s ago on engine decision"


def test_engine_connection_refused_is_busy(tmp_path: Path) -> None:
    """An engine that does not answer is down or wedged: claiming into it would fail every job."""
    assert _probe(_cfg(tmp_path), FakeEngines(error=True)) == "engine main not answering"


def test_engine_timeout_is_busy(tmp_path: Path) -> None:
    class Slow(FakeEngines):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return await super().handle_async_request(request)
            raise httpx.ReadTimeout("slow", request=request)

    assert _probe(_cfg(tmp_path), Slow()) == "engine main not answering"


def test_a_server_with_no_metrics_answers_404_and_stays_idle(tmp_path: Path) -> None:
    assert _probe(_cfg(tmp_path), FakeEngines({}, status=404)) is None


@pytest.mark.parametrize("code", [401, 403])
def test_a_denied_probe_is_not_idle(tmp_path: Path, code: int) -> None:
    """A wrong or rotated key must not open the gate."""
    denied = FakeEngines({"detail": "no"}, status=code)
    assert _probe(_cfg(tmp_path), denied) == f"engine main not answering (HTTP {code})"


@pytest.mark.parametrize("health", ["down", 500, 503, 404])
def test_serve_not_answering_is_busy_and_checked_first(tmp_path: Path, health: int | str) -> None:
    t = FakeEngines(health=health)
    reason = _probe(_cfg(tmp_path), t)
    assert reason == "serve not answering at http://127.0.0.1:8090"
    assert t.calls == 0


def test_serve_down_beats_a_held_lane_lock(tmp_path: Path) -> None:
    with lane_lock(tmp_path / "stories", "build") as held:
        assert held
        reason = _probe(_cfg(tmp_path), FakeEngines(health="down"))
    assert reason == "serve not answering at http://127.0.0.1:8090"


def test_serve_probe_times_out_in_3s_and_sends_the_serve_key(tmp_path: Path) -> None:
    t = FakeEngines()
    _probe(_cfg(tmp_path), t, serve_key="srv-key")
    assert t.health_calls == 1
    assert t.health_auth == "Bearer srv-key"
    assert all(x["read"] == 3.0 for x in t.health_timeouts)
    keyless = FakeEngines()
    _probe(_cfg(tmp_path), keyless)
    assert keyless.health_auth is None


def test_outage_reasons_keep_their_key() -> None:
    for r in ("serve not answering at http://127.0.0.1:8090", "engine main not answering"):
        assert reason_key(r) == r


def test_non_200_is_idle(tmp_path: Path) -> None:
    assert _probe(_cfg(tmp_path), FakeEngines(_metrics(0, NOW - 1), status=500)) is None


def test_metrics_probe_times_out_in_3s(tmp_path: Path) -> None:
    t = FakeEngines()
    _probe(_cfg(tmp_path), t)
    assert t.timeouts
    assert all(x["read"] == 3.0 for x in t.timeouts)


def test_missing_stories_dir_is_idle(tmp_path: Path) -> None:
    state = tmp_path / "fresh"
    assert _probe(_cfg(state), FakeEngines()) is None
    assert not (state / "stories").exists()


# ---- requests in flight ----


def test_a_request_in_flight_is_busy_however_old_the_last_one(tmp_path: Path) -> None:
    t = FakeEngines(_metrics(1, NOW - 500))
    assert _probe(_cfg(tmp_path), t) == "engine main has 1 request(s) in flight"
    assert t.paths == ["/v1/metrics"]


def test_a_request_in_flight_counts_even_just_after_the_bridges_own_job(tmp_path: Path) -> None:
    """The bridge probes only with no job of its own running: a request in flight is someone else's."""
    t = FakeEngines(_metrics(2, NOW - 1))
    assert _probe(_cfg(tmp_path), t, own=NOW - 1) == "engine main has 2 request(s) in flight"


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
    t = FakeEngines()
    _probe(cfg, t)
    assert t.auth == {"127.0.0.1:8080": "Bearer sekrit", "127.0.0.1:8081": None}


def test_a_trailing_slash_after_v1_is_the_same_root(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.engines["main"] = cfg.engines["main"].model_copy(update={"base_url": "http://127.0.0.1:8080/v1/"})
    t = FakeEngines()
    _probe(cfg, t)
    assert sorted(t.auth) == ["127.0.0.1:8080", "127.0.0.1:8081"]
    assert t.paths == ["/v1/metrics", "/v1/metrics"]
