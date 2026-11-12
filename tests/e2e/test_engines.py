"""The model manager's engines on screen: Admin lists each engine with its state, and says why one waits and
how far a download has come; Chat's status strip says the same while the chat's engine is not ready. Each
check leaves its screenshot in test-results/e2e/evidence/ (conftest.Evidence)."""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
from playwright.sync_api import Page

from airlie.models_download import DownloadProgress
from tests.e2e.conftest import KEY, Evidence, GatedEngine, Server

pytestmark = pytest.mark.e2e


@contextmanager
def asked_for(server: Server) -> Iterator[None]:
    """Something asks for the engine (a lease, as another process does) and waits for it, as long as the test
    keeps the gate shut."""
    done = threading.Thread(
        target=lambda: httpx.post(
            f"{server.url}/v1/airlie/engines/main/leases",
            json={"owner": "e2e"},
            headers={"Authorization": f"Bearer {KEY}"},
            timeout=30,
        ),
        daemon=True,
    )
    done.start()
    try:
        yield
    finally:
        done.join(timeout=10)


def test_admin_shows_an_engine_that_is_stopped_until_asked(
    app_page: Page, engine_server: tuple[Server, GatedEngine], evidence: Evidence
) -> None:
    server, _ = engine_server
    app_page.goto(f"{server.url}/admin")
    row = app_page.locator('[data-engine="main"]')
    evidence.expect(row, "the engine is listed").to_be_visible()
    evidence.expect(row, "it is stopped").to_contain_text("stopped")
    evidence.expect(row, "it is on demand").to_contain_text("on_demand")
    evidence.expect(
        row.locator("[data-test=engine-status]"), "nothing is said of an idle engine"
    ).to_have_count(0)


def test_an_engine_that_waits_says_why(
    app_page: Page, engine_server: tuple[Server, GatedEngine], evidence: Evidence
) -> None:
    server, engine = engine_server
    engine.report = ("waiting", "waiting for memory: chat:abc, coder-vm", None)
    app_page.goto(f"{server.url}/admin")
    row = app_page.locator('[data-engine="main"]')
    with asked_for(server):
        evidence.expect(row, "the engine is waiting").to_contain_text("waiting")
        evidence.expect(
            row.get_by_role("status"), "the reason is said with who holds the memory"
        ).to_have_text("Waiting for memory: chat:abc, coder-vm")
        engine.gate.set()
        evidence.expect(row, "it comes up once the memory is free").to_contain_text("ready")
        evidence.expect(row.get_by_role("status"), "a ready engine says nothing").to_have_count(0)


def test_a_download_shows_its_progress_in_admin_and_in_chat(
    app_page: Page, engine_server: tuple[Server, GatedEngine], evidence: Evidence
) -> None:
    server, engine = engine_server
    engine.report = ("downloading", "org/m", DownloadProgress(2_500_000_000, 10_000_000_000, 5e7, 150.0))
    app_page.goto(f"{server.url}/admin")
    row = app_page.locator('[data-engine="main"]')
    with asked_for(server):
        evidence.expect(row, "the engine is downloading").to_contain_text("downloading")
        evidence.expect(
            row.get_by_role("status"), "the figure says how much of the model has come"
        ).to_have_text("Downloading main: 2.5 of 10.0 GB, 3 min left")
        app_page.goto(f"{server.url}/")
        strip = app_page.locator("[data-test=status-strip]")
        evidence.expect(
            strip.get_by_role("status"), "Chat's status strip says the same while the model comes"
        ).to_have_text(re.compile(r"Downloading main: 2\.5 of 10\.0 GB"))
        engine.gate.set()
        evidence.expect(strip.get_by_role("status"), "the line goes once the engine is ready").to_have_count(
            0
        )
