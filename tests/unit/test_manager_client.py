"""Every role call holds its engine: ManagedEngine holds around generate and yes_no, and holder_for picks the
manager, serve's lease routes or a local start. Fakes only: a recording holder, httpx.MockTransport for serve,
FakeRunner and FakeProber for the local start."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import yaml

from airlie.config import AirlieConfig, Sampling, load_config
from airlie.engine.fake import FakeEngine, FakeReply
from airlie.engine.process import EngineProcess
from airlie.engine.types import EngineError, GenRequest, Message
from airlie.manager.client import (
    InProcessHolder,
    LazyHolder,
    LeaseHolder,
    LocalHolder,
    ManagedEngine,
    holder_for,
)
from airlie.manager.core import Manager
from airlie.serve.bridge.clock import FakeClock
from tests.support.process import FakeProber, FakeRunner
from tests.support.serve import FakeEngine as FakeResource


def make_cfg(tmp_path: Path) -> AirlieConfig:
    data = {
        "version": 1,
        "engines": {"main": {"kind": "mlx_vlm", "port": 8080, "model": "base", "lifecycle": "on_demand"}},
        "models": {"base": {"repo": "org/base", "revision": "r1", "path": "/models/base"}},
        "roles": {
            r: {"prompt": "p.md", "model": "base"} for r in ("coder", "summarizer", "judge", "aggregator")
        },
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    (tmp_path / "p.md").write_text("x")
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


class RecordingHolder:
    """Notes each hold's enter and exit in `events` (shared with the engine it wraps)."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    @asynccontextmanager
    async def hold(self, engine: str, owner: str) -> AsyncIterator[None]:
        self.events.append(f"enter {engine} {owner}")
        try:
            yield
        finally:
            self.events.append(f"exit {engine}")


class NotingEngine(FakeEngine):
    def __init__(self, events: list[str], scripts: dict[str, list[FakeReply]]) -> None:
        super().__init__(scripts)
        self.events = events

    async def generate(self, req, on_token=None):  # type: ignore[no-untyped-def]
        self.events.append("generate")
        return await super().generate(req, on_token)

    async def yes_no(self, role, messages):  # type: ignore[no-untyped-def]
        self.events.append("yes_no")
        return await super().yes_no(role, messages)


def request(role: str = "coder") -> GenRequest:
    return GenRequest(role, [Message("user", "hi")], Sampling(), 16)


def managed(events: list[str], reply: FakeReply | None = None) -> ManagedEngine:
    inner = NotingEngine(events, {"coder": [reply or FakeReply(text="ok")]})
    return ManagedEngine(inner, "main", RecordingHolder(events))


async def test_generate_runs_inside_a_hold() -> None:
    events: list[str] = []
    res = await managed(events).generate(request())
    assert res.text == "ok"
    assert events == ["enter main role:coder", "generate", "exit main"]


async def test_yes_no_runs_inside_a_hold() -> None:
    events: list[str] = []
    p = await managed(events, FakeReply(p_yes=0.75)).yes_no("coder", [Message("user", "ok?")])
    assert p == (0.75, 0.25)
    assert events == ["enter main role:coder", "yes_no", "exit main"]


async def test_other_methods_do_not_hold() -> None:
    events: list[str] = []
    e = managed(events)
    await e.start()
    await e.health()
    await e.cancel("r")
    e.stats()
    await e.stop()
    assert events == []
    assert e.capabilities == e._inner.capabilities


async def test_the_hold_is_released_when_the_call_raises() -> None:
    events: list[str] = []
    e = ManagedEngine(NotingEngine(events, {}), "main", RecordingHolder(events))  # no script: generate raises
    with pytest.raises(EngineError, match="no script"):
        await e.generate(request())
    assert events == ["enter main role:coder", "generate", "exit main"]


# ---- holder_for ----


def serve_transport(health: int | None, seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def answer(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        if health is None:
            raise httpx.ConnectError("refused")
        return httpx.Response(health, json={"ok": True})

    return httpx.MockTransport(answer)


async def test_holder_for_uses_the_in_process_manager_when_given(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    manager = Manager([FakeResource("main")], budget_gb=100.0, clock=FakeClock())
    h = holder_for(make_cfg(tmp_path), manager=manager, transport=serve_transport(200, seen))
    assert isinstance(h, InProcessHolder)
    assert seen == []  # serve is not even asked
    async with h.hold("main", "role:coder"):
        assert manager.status()[0].holders == ("role:coder",)
    assert manager.status()[0].holders == ()


async def test_the_in_process_holder_leaves_an_engine_the_manager_does_not_run() -> None:
    manager = Manager([], budget_gb=100.0, clock=FakeClock())
    async with InProcessHolder(manager).hold("elsewhere", "role:coder"):
        pass


async def test_holder_for_uses_serve_when_its_health_answers(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    h = holder_for(
        make_cfg(tmp_path),
        manager=None,
        env={"AIRLIE_URL": "http://serve.test:9000"},
        transport=serve_transport(200, seen),
    )
    assert isinstance(h, LeaseHolder)
    assert [str(r.url) for r in seen] == ["http://serve.test:9000/health"]


async def test_holder_for_falls_back_to_local_when_serve_is_down(tmp_path: Path) -> None:
    assert isinstance(
        holder_for(make_cfg(tmp_path), manager=None, transport=serve_transport(None)), LocalHolder
    )


async def test_holder_for_falls_back_to_local_when_health_is_not_a_200(tmp_path: Path) -> None:
    assert isinstance(
        holder_for(make_cfg(tmp_path), manager=None, transport=serve_transport(503)), LocalHolder
    )


async def test_the_default_serve_is_loopback_8090(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    holder_for(make_cfg(tmp_path), manager=None, env={}, transport=serve_transport(200, seen))
    assert str(seen[0].url) == "http://127.0.0.1:8090/health"


# ---- LeaseHolder ----


class FakeServe:
    """serve's lease routes: grants lease-1, counts renewals, notes the key and the delete."""

    def __init__(self, ttl_s: float = 120.0, grant: httpx.Response | None = None) -> None:
        self.ttl_s = ttl_s
        self.grant = grant
        self.calls: list[tuple[str, str]] = []
        self.keys: list[str | None] = []
        self.owners: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path))
        self.keys.append(req.headers.get("authorization"))
        if req.method == "POST":
            self.owners.append(req.content.decode())
            return self.grant or httpx.Response(200, json={"lease": "lease-1", "ttl_s": self.ttl_s})
        return httpx.Response(204)


def lease_holder(serve: FakeServe, clock: FakeClock, key: str | None = "k") -> LeaseHolder:
    client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
    return LeaseHolder("http://serve.test:8090/", key, client, clock)


async def test_a_lease_is_taken_renewed_every_third_of_its_ttl_and_deleted() -> None:
    serve, clock = FakeServe(ttl_s=90.0), FakeClock()
    holder = lease_holder(serve, clock)
    async with holder.hold("main", "role:coder"):
        while len([c for c in serve.calls if c[0] == "PUT"]) < 2:
            await asyncio.sleep(0)
    assert clock.sleeps[0] == 30.0
    base = "/v1/airlie/engines/main/leases"
    assert serve.calls[0] == ("POST", base)
    assert ("PUT", f"{base}/lease-1") in serve.calls
    assert serve.calls[-1] == ("DELETE", f"{base}/lease-1")
    assert serve.owners == ['{"owner":"role:coder"}']
    assert set(serve.keys) == {"Bearer k"}


async def test_no_key_sends_no_authorization() -> None:
    serve = FakeServe()
    async with lease_holder(serve, FakeClock(), key=None).hold("main", "role:coder"):
        pass
    assert set(serve.keys) == {None}


async def test_a_failed_lease_start_raises_what_serve_said() -> None:
    serve = FakeServe(grant=httpx.Response(503, json={"error": "out of memory", "engine": "main"}))
    with pytest.raises(EngineError, match="main: out of memory"):
        async with lease_holder(serve, FakeClock()).hold("main", "role:coder"):
            raise AssertionError("not held")
    assert [m for m, _ in serve.calls] == ["POST"]


async def test_a_serve_that_stops_answering_the_lease_request_is_an_engine_error() -> None:
    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    holder = LeaseHolder(
        "http://serve.test", None, httpx.AsyncClient(transport=httpx.MockTransport(refuse)), FakeClock()
    )
    with pytest.raises(EngineError, match="did not answer"):
        async with holder.hold("main", "role:coder"):
            raise AssertionError("not held")


async def test_the_lease_is_deleted_when_the_body_raises() -> None:
    serve = FakeServe()
    with pytest.raises(ValueError):
        async with lease_holder(serve, FakeClock()).hold("main", "role:coder"):
            raise ValueError("boom")
    assert serve.calls[-1][0] == "DELETE"


async def test_a_failed_delete_does_not_hide_the_result() -> None:
    def answer(req: httpx.Request) -> httpx.Response:
        if req.method == "DELETE":
            raise httpx.ConnectError("gone")
        return httpx.Response(200, json={"lease": "l", "ttl_s": 3.0})

    holder = LeaseHolder(
        "http://serve.test", None, httpx.AsyncClient(transport=httpx.MockTransport(answer)), FakeClock()
    )
    async with holder.hold("main", "role:coder"):
        pass


class RestartedServe:
    """serve restarted after granting l1: renewing l1 is a 404 and the next POST grants l2. `refuse_posts`
    re-POSTs fail first (serve not back yet: a ConnectError, or a status while it boots)."""

    def __init__(self, refuse_posts: list[int | None] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.owners: list[str] = []
        self.refuse_posts = list(refuse_posts or [])
        self.granted = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path.rsplit("/", 1)[-1]))
        if req.method == "POST":
            self.owners.append(req.content.decode())
            if self.granted and self.refuse_posts:
                status = self.refuse_posts.pop(0)
                if status is None:
                    raise httpx.ConnectError("refused")
                return httpx.Response(status, json={"error": "starting"})
            self.granted += 1
            return httpx.Response(200, json={"lease": f"l{self.granted}", "ttl_s": 3.0})
        if req.method == "PUT" and req.url.path.endswith("/l1"):
            return httpx.Response(404, json={"detail": "unknown lease"})
        return httpx.Response(204)

    def puts(self, lease: str) -> int:
        return self.calls.count(("PUT", lease))


def restarted_holder(serve: RestartedServe) -> LeaseHolder:
    return LeaseHolder(
        "http://serve.test", None, httpx.AsyncClient(transport=httpx.MockTransport(serve)), FakeClock()
    )


async def test_a_lease_serve_has_forgotten_is_taken_again_and_renewed() -> None:
    serve = RestartedServe()
    async with restarted_holder(serve).hold("main", "role:coder"):
        while serve.puts("l2") < 2:
            await asyncio.sleep(0)
    assert serve.calls[:5] == [
        ("POST", "leases"),
        ("PUT", "l1"),
        ("POST", "leases"),
        ("PUT", "l2"),
        ("PUT", "l2"),
    ]
    assert serve.calls[-1] == ("DELETE", "l2")
    assert serve.owners == ['{"owner":"role:coder"}'] * 2


async def test_a_new_lease_serve_does_not_answer_is_asked_for_again_each_interval() -> None:
    serve = RestartedServe(refuse_posts=[None, 503])
    async with restarted_holder(serve).hold("main", "role:coder"):
        while serve.puts("l2") < 1:
            await asyncio.sleep(0)
    assert [m for m, _ in serve.calls[:6]] == ["POST", "PUT", "POST", "POST", "POST", "PUT"]
    assert serve.calls[-1] == ("DELETE", "l2")


async def test_a_renewal_that_fails_is_tried_again() -> None:
    puts: list[int] = []

    def answer(req: httpx.Request) -> httpx.Response:
        if req.method == "PUT":
            puts.append(1)
            if len(puts) == 1:
                raise httpx.ConnectError("blip")
        if req.method == "POST":
            return httpx.Response(200, json={"lease": "l", "ttl_s": 3.0})
        return httpx.Response(204)

    clock = FakeClock()
    holder = LeaseHolder(
        "http://serve.test", None, httpx.AsyncClient(transport=httpx.MockTransport(answer)), clock
    )
    async with holder.hold("main", "role:coder"):
        while len(puts) < 2:
            await asyncio.sleep(0)


async def test_a_503_without_a_json_body_is_still_an_engine_error() -> None:
    serve = FakeServe(grant=httpx.Response(502, text="bad gateway"))
    with pytest.raises(EngineError, match="main: serve answered 502"):
        async with lease_holder(serve, FakeClock()).hold("main", "role:coder"):
            raise AssertionError("not held")


# ---- LocalHolder ----


def local(tmp_path: Path, prober: FakeProber, runner: FakeRunner) -> LocalHolder:
    cfg = make_cfg(tmp_path)

    def process_for(name: str) -> EngineProcess:
        return EngineProcess(
            cfg,
            name,
            state_dir=tmp_path / "state",
            runner=runner,
            prober=prober,
            snapshot_dir=lambda m: Path(f"/hf/{m.repo}/{m.revision}"),
            poll_s=0,
        )

    return LocalHolder(cfg, tmp_path / "state", process_for=process_for)


async def test_local_holder_starts_a_down_engine_and_leaves_it(tmp_path: Path) -> None:
    runner = FakeRunner()
    prober = FakeProber(health=[False, True], loaded=[["/models/base"]])
    async with local(tmp_path, prober, runner).hold("main", "role:coder"):
        assert len(runner.spawned) == 1
    assert runner.killed == []  # serve's reaper stops it, not the caller
    assert runner.alive == {4242}


async def test_local_holder_leaves_a_running_engine_alone(tmp_path: Path) -> None:
    runner = FakeRunner()
    async with local(tmp_path, FakeProber(health=[True]), runner).hold("main", "role:coder"):
        pass
    assert runner.spawned == [] and runner.killed == []


async def test_local_holder_raises_when_the_start_fails(tmp_path: Path) -> None:
    runner = FakeRunner(port_holder=999)
    with pytest.raises(EngineError, match="port 8080 is held"):
        async with local(tmp_path, FakeProber(health=[False]), runner).hold("main", "role:coder"):
            raise AssertionError("not held")


# ---- LazyHolder ----


async def test_a_lazy_holder_chooses_once_at_its_first_hold() -> None:
    events: list[str] = []
    chosen: list[int] = []

    def choose() -> RecordingHolder:
        chosen.append(1)
        return RecordingHolder(events)

    h = LazyHolder(choose)
    assert chosen == []
    async with h.hold("main", "a"):
        pass
    async with h.hold("main", "b"):
        pass
    assert chosen == [1]
    assert events == ["enter main a", "exit main", "enter main b", "exit main"]


async def test_a_lazy_holder_that_chose_local_takes_a_lease_once_serve_answers(tmp_path: Path) -> None:
    serve = FakeServe()
    choices: list[LocalHolder | LeaseHolder] = [
        local(tmp_path, FakeProber(health=[True]), FakeRunner()),  # serve down: the engine is up, used here
        lease_holder(serve, FakeClock()),  # serve up at the next hold
    ]
    h = LazyHolder(lambda: choices.pop(0))
    async with h.hold("main", "a"):
        pass
    async with h.hold("main", "b"):
        pass
    assert [m for m, _ in serve.calls] == ["POST", "DELETE"]
    assert serve.owners == ['{"owner":"b"}']


async def test_a_lazy_holder_keeps_asking_while_serve_is_down(tmp_path: Path) -> None:
    asked: list[int] = []
    runner = FakeRunner()

    def choose() -> LocalHolder:
        asked.append(1)
        return local(tmp_path, FakeProber(health=[True]), runner)

    h = LazyHolder(choose)
    async with h.hold("main", "a"):
        pass
    async with h.hold("main", "b"):
        pass
    assert asked == [1, 1] and runner.spawned == []


async def test_a_lazy_holder_that_stays_local_keeps_its_first_local_holder(tmp_path: Path) -> None:
    runner = FakeRunner()
    first = local(tmp_path, FakeProber(health=[True]), runner)
    choices = [first, local(tmp_path, FakeProber(health=[True]), runner)]
    h = LazyHolder(lambda: choices.pop(0))
    async with h.hold("main", "a"):
        pass
    async with h.hold("main", "b"):
        pass
    assert h._chosen is first  # its per-engine start locks stay the ones every hold shares


# ---- _http_engine ----


async def test_a_scripted_transport_goes_without_a_hold(tmp_path: Path) -> None:
    from airlie import cli
    from airlie.engine.http_engine import HTTPEngine

    eng = cli._http_engine(make_cfg(tmp_path), "main", transport=serve_transport(200))
    assert isinstance(eng, HTTPEngine)


async def test_a_scripted_transport_with_a_manager_holds_through_it(tmp_path: Path) -> None:
    from airlie import cli
    from airlie.engine.http_engine import HTTPEngine

    manager = Manager([FakeResource("main")], budget_gb=100.0, clock=FakeClock())
    eng = cli._http_engine(make_cfg(tmp_path), "main", transport=serve_transport(200), manager=manager)
    assert isinstance(eng, ManagedEngine) and isinstance(eng._inner, HTTPEngine)


# ---- the serving manager ----


async def test_holder_for_uses_the_serving_manager_without_asking_serve(tmp_path: Path) -> None:
    from airlie.manager.client import serving

    seen: list[httpx.Request] = []
    manager = Manager([FakeResource("main")], budget_gb=100.0, clock=FakeClock())
    with serving(manager):
        h = holder_for(make_cfg(tmp_path), manager=None, transport=serve_transport(200, seen))
    assert isinstance(h, InProcessHolder)
    assert seen == []


async def test_without_a_serving_manager_holder_for_asks_serve(tmp_path: Path) -> None:
    from airlie.manager.client import serving

    manager = Manager([], budget_gb=100.0, clock=FakeClock())
    with serving(manager):
        pass
    h = holder_for(make_cfg(tmp_path), manager=None, transport=serve_transport(None))
    assert isinstance(h, LocalHolder)


# ---- LocalHolder: concurrent holds ----


async def test_concurrent_local_holds_start_the_engine_once(tmp_path: Path) -> None:
    runner = FakeRunner()

    class Loading(FakeProber):
        probes_since_spawn = 0

        async def health_ok(self) -> bool:
            await asyncio.sleep(0)  # a real probe waits on the network: the other hold runs meanwhile
            if runner.spawned:
                self.probes_since_spawn += 1
            return self.probes_since_spawn >= 3  # a spawned engine takes a few probes to answer

    holder = local(tmp_path, Loading(health=[], loaded=[["/models/base"]]), runner)

    async def one() -> None:
        async with holder.hold("main", "role:coder"):
            await asyncio.sleep(0)

    await asyncio.gather(one(), one())
    assert len(runner.spawned) == 1


async def test_a_start_that_loses_the_race_is_not_an_error(tmp_path: Path) -> None:
    """Another process started the engine between the health probe and the start: it is up, so go on."""
    runner = FakeRunner()
    runner.alive.add(77)
    holder = local(tmp_path, FakeProber(health=[False, True]), runner)
    (tmp_path / "state" / "engines" / "main").mkdir(parents=True)
    (tmp_path / "state" / "engines" / "main" / "pid").write_text("77\n")
    async with holder.hold("main", "role:coder"):
        pass
    assert runner.spawned == []
