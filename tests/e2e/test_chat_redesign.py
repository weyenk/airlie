"""The redesigned chat in Chromium, the one critical journey: open a pinned repository's chat from the panel,
send a message, see a tool block and a permission prompt, approve it, see the reply; once on a desktop and
once at 375 px through the slide-over. The turn route is the real one: its model is a scripted engine (a
read, then a push the person must approve, then a reply), its VM a FakeBox (tests/support) and its GitHub a
recording publisher. Each check leaves its screenshot in test-results/e2e/evidence/ (conftest.Evidence)."""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page

from airlie.boxes.protocol import Box, ExecResult
from airlie.manager.core import Manager
from airlie.serve.bridge.clock import RealClock
from tests.e2e.conftest import KEY, Evidence, Server, _serve
from tests.support.repo_chat import RegisteredBox, RepoFakes, cloning
from tests.support.serve import FakeEngine
from tests.support.serve_chat import Call, ScriptedUpstream, Text
from tests.unit.test_transit import FakeRunner

pytestmark = pytest.mark.e2e

REPO = "o/r"
AUTH = {"Authorization": f"Bearer {KEY}"}


class _Push:
    """The branch the VM last bundled: the host's git (a recording runner) must list it back."""

    branch = ""


class ChatVM(RegisteredBox):
    """A chat VM with two commits to push, whatever its chat's id (and so its branch) is."""

    def _interpret(self, argv: Sequence[str], stdin: bytes | None) -> ExecResult:
        a = tuple(argv)
        if a == ("cat", "/w/README.md"):
            return ExecResult(0, "# o/r\nA small repository.\n")
        if a[:3] == ("git", "rev-list", "--count"):
            return ExecResult(0, "2\n")
        if a[:4] == ("git", "bundle", "create", "-"):
            _Push.branch = a[4].split("..", 1)[1]
            return ExecResult(0, "VM-BUNDLE")
        return super()._interpret(argv, stdin)


def _fakes(root: Path) -> RepoFakes:
    fakes = RepoFakes(root)
    inner = fakes.mirror.run

    def box_for(name: str) -> Box:
        if name not in fakes.boxes:
            box = ChatVM(name, registry=fakes.boxes)
            box.handlers["git"] = cloning
            if "-building-" in name:
                box.handlers["sh"] = lambda _b, _a, _s: ExecResult(0, "")
            fakes.boxes[name] = box
        return fakes.boxes[name]

    async def run(args: list[str], cwd: Path, env: Any) -> str:
        if FakeRunner.verb(args) == "bundle" and "list-heads" in args:
            return f"{fakes.base} refs/heads/{_Push.branch}\n"
        return await inner(args, cwd, env)

    fakes.box_for = box_for  # type: ignore[method-assign]
    fakes.mirror.run = run  # type: ignore[assignment]
    fakes.runner.answers["log"] = "b2b2b2b fix the parser\nb1b1b1b add a test\n"
    fakes.runner.answers["diff"] = "A\0src/parse.py\0"
    return fakes


@pytest.fixture
def chat_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    """A server of the test's own: repository chats in FakeBox VMs, `o/r` pinnable, and a model that reads a
    file, asks to push, and (once the push is approved) says it is done."""
    root = tmp_path_factory.mktemp("airlie")
    fakes = _fakes(root)

    async def pinnable() -> list[str]:
        return [REPO]

    scripted = ScriptedUpstream(
        Call("read_file", {"path": "README.md"}),
        Call("push_branch", {}),
        Text("I need your OK to push."),
        Text("Pushed the branch; it is ready for a pull request."),
    )
    manager = Manager([FakeEngine("main")], budget_gb=100.0, clock=RealClock())
    yield from _serve(root, manager=manager, upstream=scripted, repo_sessions=fakes.build, pinnable=pinnable)


def _prepare(server: Server) -> None:
    """The person's pinned repository with a chat of its own, then a plain chat (so `/` opens that one)."""
    with httpx.Client(base_url=server.url, headers=AUTH, timeout=10) as c:
        c.put(f"/v1/airlie/chat/pins/{REPO}").raise_for_status()
        c.post("/v1/airlie/chat/threads", json={"kind": "repository", "repo": REPO}).raise_for_status()
        time.sleep(1.1)  # the plain chat is the newest by a second (times are whole seconds)
        c.post("/v1/airlie/chat/threads", json={"kind": "plain"}).raise_for_status()


def _journey(page: Page, server: Server, evidence: Evidence, *, phone: bool) -> None:
    _prepare(server)
    page.goto(f"{server.url}/")
    page.get_by_label("Message", exact=True).wait_for()
    evidence.expect(
        page.locator("[data-test=chat-header] h1"), "the chat opens on the newest chat"
    ).to_have_text("New chat")
    if phone:
        evidence.expect(
            page.locator("aside[aria-label=Chats]"), "the panel is out of the way on a phone"
        ).to_have_count(0)
        page.get_by_role("button", name="Show chats").click()
        panel = page.locator("[data-test=panel-slide-over]")
        evidence.expect(panel, "the slide-over opens").to_be_visible()
    else:
        panel = page.locator("aside[aria-label=Chats]")
        evidence.expect(panel, "the panel is open beside the thread").to_be_visible()
    group = panel.locator(f"section[aria-label='{REPO}']")
    evidence.expect(group, "the pinned repository is listed with its chat").to_be_visible()
    group.locator("[data-test=chat-row]").first.get_by_role("button", name=f"{REPO}, airlie/chat-").click()
    if phone:
        evidence.expect(panel, "picking a chat closes the slide-over").to_have_count(0)
    else:
        evidence.expect(
            panel.locator("[aria-current=page]"), "the open chat is marked in the panel"
        ).to_contain_text(REPO)
    if not phone:
        evidence.expect(
            page.locator("[data-test=chat-header]"), "the header names the repository"
        ).to_contain_text(REPO)
    evidence.expect(
        page.locator("[data-test=composer-footer] [data-ask-first]"),
        "the composer says it asks before pushing",
    ).to_have_text("Ask before pushing")

    page.get_by_label("Message", exact=True).fill("read the readme, then push what you changed")
    page.get_by_role("button", name="Send").click()
    evidence.expect(page.locator("[data-msg=user]"), "my message is in the thread").to_contain_text(
        "read the readme"
    )
    tool = page.locator("[data-tool]").first
    evidence.expect(tool, "the model's read shows as a tool block").to_contain_text("read_file")
    evidence.expect(tool, "its state is said").to_have_attribute("data-phase", "done")
    permission = page.locator("[data-permission]")
    evidence.expect(permission, "the push waits for my yes").to_contain_text("Push session branch to origin?")
    evidence.expect(permission, "it says what it would push").to_contain_text("b2b2b2b fix the parser")
    evidence.expect(
        page.get_by_text("I need your OK to push."), "and the model says it waits"
    ).to_be_visible()

    permission.get_by_role("button", name="Approve").click()
    evidence.expect(permission.get_by_role("status"), "the prompt reads Approved").to_have_text("Approved")
    evidence.expect(
        page.get_by_text("Pushed the branch; it is ready for a pull request."),
        "the reply after the push is shown",
    ).to_be_visible()
    evidence.expect(
        page.locator("[data-tool][data-phase=done]"), "the push ran as a tool block too"
    ).to_have_count(2)
    evidence.expect(page.get_by_role("button", name="Send"), "the composer is ready again").to_be_visible()


def test_a_pinned_repository_chat_runs_a_turn_with_a_tool_block_and_an_approved_push(
    app_page: Page, chat_server: Server, evidence: Evidence
) -> None:
    app_page.set_viewport_size({"width": 1280, "height": 800})
    _journey(app_page, chat_server, evidence, phone=False)


def test_the_same_journey_at_375_px_through_the_slide_over(
    app_page: Page, chat_server: Server, evidence: Evidence
) -> None:
    app_page.set_viewport_size({"width": 375, "height": 700})
    _journey(app_page, chat_server, evidence, phone=True)
