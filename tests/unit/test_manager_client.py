"""A call holds its engine: ManagedEngine holds around generate and yes_no, and a LeaseHolder takes serve's
lease routes. Fakes only: a recording holder and httpx.MockTransport for serve."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from airlie.config import Sampling
from airlie.engine.fake import FakeEngine, FakeReply
from airlie.engine.types import EngineError, GenRequest, Message
from airlie.manager.client import (
    InProcessHolder,
    LeaseHolder,
    ManagedEngine,
)
from airlie.manager.core import Manager
from airlie.serve.bridge.clock import FakeClock


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


def request(role: str = "architect") -> GenRequest:
    return GenRequest(role, [Message("user", "hi")], Sampling(), 16)


def managed(events: list[str], reply: FakeReply | None = None) -> ManagedEngine:
    inner = NotingEngine(events, {"architect": [reply or FakeReply(text="ok")]})
    return ManagedEngine(inner, "main", RecordingHolder(events))


async def test_generate_runs_inside_a_hold() -> None:
    events: list[str] = []
    res = await managed(events).generate(request())
    assert res.text == "ok"
    assert events == ["enter main role:architect", "generate", "exit main"]


async def test_yes_no_runs_inside_a_hold() -> None:
    events: list[str] = []
    p = await managed(events, FakeReply(p_yes=0.75)).yes_no("architect", [Message("user", "ok?")])
    assert p == (0.75, 0.25)
    assert events == ["enter main role:architect", "yes_no", "exit main"]


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
    assert events == ["enter main role:architect", "generate", "exit main"]


# ---- holder_for ----


async def test_the_in_process_holder_leaves_an_engine_the_manager_does_not_run() -> None:
    manager = Manager([], budget_gb=100.0, clock=FakeClock())
    async with InProcessHolder(manager).hold("elsewhere", "role:architect"):
        pass


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
    async with holder.hold("main", "role:architect"):
        while len([c for c in serve.calls if c[0] == "PUT"]) < 2:
            await asyncio.sleep(0)
    assert clock.sleeps[0] == 30.0
    base = "/v1/airlie/engines/main/leases"
    assert serve.calls[0] == ("POST", base)
    assert ("PUT", f"{base}/lease-1") in serve.calls
    assert serve.calls[-1] == ("DELETE", f"{base}/lease-1")
    assert serve.owners == ['{"owner":"role:architect"}']
    assert set(serve.keys) == {"Bearer k"}


async def test_no_key_sends_no_authorization() -> None:
    serve = FakeServe()
    async with lease_holder(serve, FakeClock(), key=None).hold("main", "role:architect"):
        pass
    assert set(serve.keys) == {None}


async def test_a_failed_lease_start_raises_what_serve_said() -> None:
    serve = FakeServe(grant=httpx.Response(503, json={"error": "out of memory", "engine": "main"}))
    with pytest.raises(EngineError, match="main: out of memory"):
        async with lease_holder(serve, FakeClock()).hold("main", "role:architect"):
            raise AssertionError("not held")
    assert [m for m, _ in serve.calls] == ["POST"]


async def test_a_serve_that_stops_answering_the_lease_request_is_an_engine_error() -> None:
    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    holder = LeaseHolder(
        "http://serve.test", None, httpx.AsyncClient(transport=httpx.MockTransport(refuse)), FakeClock()
    )
    with pytest.raises(EngineError, match="did not answer"):
        async with holder.hold("main", "role:architect"):
            raise AssertionError("not held")


async def test_the_lease_is_deleted_when_the_body_raises() -> None:
    serve = FakeServe()
    with pytest.raises(ValueError):
        async with lease_holder(serve, FakeClock()).hold("main", "role:architect"):
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
    async with holder.hold("main", "role:architect"):
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
    async with restarted_holder(serve).hold("main", "role:architect"):
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
    assert serve.owners == ['{"owner":"role:architect"}'] * 2


async def test_a_new_lease_serve_does_not_answer_is_asked_for_again_each_interval() -> None:
    serve = RestartedServe(refuse_posts=[None, 503])
    async with restarted_holder(serve).hold("main", "role:architect"):
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
    async with holder.hold("main", "role:architect"):
        while len(puts) < 2:
            await asyncio.sleep(0)


async def test_a_503_without_a_json_body_is_still_an_engine_error() -> None:
    serve = FakeServe(grant=httpx.Response(502, text="bad gateway"))
    with pytest.raises(EngineError, match="main: serve answered 502"):
        async with lease_holder(serve, FakeClock()).hold("main", "role:architect"):
            raise AssertionError("not held")


# ---- LocalHolder ----


# ---- LazyHolder ----


# ---- _http_engine ----


# ---- the serving manager ----


# ---- LocalHolder: concurrent holds ----
