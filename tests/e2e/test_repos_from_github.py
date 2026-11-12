"""Connect GitHub from the chat panel, see a person's repositories, and open a repository chat whose model
reads README.md. GitHub is FakeGitHubUser; VMs are FakeBox. Screenshots in test-results/e2e/evidence/."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from playwright.sync_api import Page

from airlie.config import load_config
from airlie.manager.core import Manager
from airlie.serve.app import create_app
from airlie.serve.bridge.clock import RealClock
from airlie.serve.server import Server as AirlieServer
from airlie.sources.github import GitHubSource
from tests.e2e.conftest import (
    KEY,
    Evidence,
    Server,
    Upstream,
    _config,
    _free_port,
    _seed,
    _seed_story,
    _seed_topologies,
    _seed_topology,
)
from tests.e2e.test_chat_redesign import _fakes
from tests.support.github_user import FakeGitHubUser
from tests.support.repo_chat import RepoFakes
from tests.support.serve import FakeEngine
from tests.support.serve_chat import Call, ScriptedUpstream, Text
from tests.unit.github_helpers import APP_ID, FakeGitHub, app_spec

pytestmark = pytest.mark.e2e

REPO = "o/r"
CLIENT_ID, SECRET = "Iv1.testclient", "s3cr3t-client-secret"
ANSWER = "A small repository."


async def _no_extras() -> list[str]:
    return []


def _serve_github(
    root: Path,
    *,
    spec: Any,
    source: GitHubSource,
    fakes: RepoFakes,
    scripted: ScriptedUpstream,
) -> Iterator[Server]:
    path = root / "airlie.yaml"
    data = yaml.safe_load(path.read_text())
    (root / "keys").mkdir(parents=True, exist_ok=True)
    token_key = root / "keys" / "tokens.key"
    data["story"] = {
        "github_app": {
            "app_id": APP_ID,
            "client_id": CLIENT_ID,
            "token_key_file": str(token_key),
            "key_file": str(spec.key_file),
            "client_secret_file": str(root / "client-secret"),
            "oauth_url": "http://127.0.0.1:9/oauth",
        }
    }
    data["chat"]["sources"] = ["github_app"]
    path.write_text(yaml.safe_dump(data))
    cfg = load_config(path)
    state = cfg.observability.state_dir.expanduser()
    state.mkdir(parents=True, exist_ok=True)
    _seed(state)
    _seed_story(state)
    _seed_topology(state)
    _seed_topologies(state)
    (root / "repo" / ".git").mkdir(parents=True)
    manager = Manager([FakeEngine("main")], budget_gb=100.0, clock=RealClock())
    app = create_app(
        cfg,
        transport=httpx.MockTransport(scripted),
        manager=manager,
        api_key=KEY,
        pinnable=_no_extras,
        repo_sessions=fakes.build,
        repo_sources={"github_app": source},
        now=time.time,
    )
    port = _free_port()
    srv = AirlieServer(app, host="127.0.0.1", port=port, log_level="warning")
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not srv.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("the test server did not start")
        time.sleep(0.05)
    yield Server(f"http://127.0.0.1:{port}", state, Upstream())
    srv.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def github_chat(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Server, RepoFakes, FakeGitHubUser]]:
    root = tmp_path_factory.mktemp("airlie")
    _config(root)
    secret = root / "client-secret"
    secret.write_text(SECRET + "\n")
    secret.chmod(0o600)
    clock = time.time
    fake = FakeGitHubUser(
        FakeGitHub(installed=(REPO,)), client_id=CLIENT_ID, client_secret=SECRET, clock=clock
    )
    fake.grant("weyenk", {1: [(REPO, True)]})
    spec = app_spec(
        root, client_id=CLIENT_ID, oauth_url="http://127.0.0.1:9/oauth", client_secret_file=secret
    )
    source = GitHubSource(
        spec, f"https://github.com/{REPO}.git", transport=httpx.MockTransport(fake), clock=clock
    )
    fakes = _fakes(root)
    scripted = ScriptedUpstream(
        Call("read_file", {"path": "README.md"}),
        Text(ANSWER),
    )
    for server in _serve_github(root, spec=spec, source=source, fakes=fakes, scripted=scripted):
        yield server, fakes, fake


def test_a_person_connects_github_sees_their_repositories_and_opens_a_repository_chat(
    app_page: Page, github_chat: tuple[Server, RepoFakes, FakeGitHubUser], evidence: Evidence
) -> None:
    server, _fakes, fake = github_chat
    app_page.set_viewport_size({"width": 1280, "height": 800})
    app_page.goto(f"{server.url}/")
    app_page.get_by_label("Message", exact=True).wait_for()
    panel = app_page.locator("aside[aria-label=Chats]")
    connect = panel.get_by_role("button", name="Connect GitHub")
    evidence.expect(connect, "Connect GitHub in the panel").to_be_visible()
    panel.get_by_role("button", name="Connect GitHub").click()
    code = panel.locator("[data-test=link-code]")
    evidence.expect(code, "the user code is shown").to_be_visible()
    user_code = code.inner_text()
    evidence.expect(
        panel.get_by_role("link", name="Open https://github.com/login/device"), "the verification link"
    ).to_be_visible()
    fake.approve(user_code, "weyenk")
    repo_section = panel.locator(f"section[aria-label='{REPO}']")
    evidence.expect(repo_section, f"{REPO} is listed under GitHub").to_be_visible(timeout=30_000)
    repo_section.get_by_role("button", name=f"New chat in {REPO}").click()
    app_page.wait_for_url("**/chat/**")
    app_page.get_by_label("Message", exact=True).fill("what is this repository?")
    app_page.get_by_role("button", name="Send").click()
    tool = app_page.locator("[data-tool]").first
    evidence.expect(tool, "the model's read shows as a tool block").to_contain_text("read_file")
    evidence.expect(tool, "the read finished").to_have_attribute("data-phase", "done")
    evidence.expect(app_page.get_by_text(ANSWER), "the model's answer after the read").to_be_visible(
        timeout=30_000
    )
    evidence.expect(
        panel.locator(f"section[aria-label='{REPO}'] [data-test=chat-row]"),
        "the chat is listed under its repository",
    ).to_have_count(1)
