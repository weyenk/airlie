"""The web app in a real browser: airlie serve's app on a free port of this machine, with a fake engine behind
it and a board of known cards, driven by Playwright's Chromium. Run in the e2e container (`task test:e2e`),
which has the browser; the host gate leaves these out (the `e2e` marker).

Every check a test makes goes through `evidence`: it passes or fails as Playwright's would, and leaves a
screenshot of the page at that moment, the element checked outlined, in test-results/evidence/<test>/ with an
index.md that lists them in order with what each one checked and whether it held."""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
import yaml
from playwright.sync_api import BrowserContext, Locator, Page, expect

from airlie.board.store import BoardStore
from airlie.config import AirlieConfig, load_config
from airlie.serve.app import create_app

CHAT_SKILLS = Path(__file__).resolve().parents[2] / "skills" / "chat"
"""The chat skills airlie ships: the / menu is tested on the real /story."""
KEY = "e2e-admin-key"
"""The admin key the test server is started with: made up here, for this server only."""
CARDS = (
    ("initiative", "Ship the web app", None, "doing"),
    ("epic", "Board polish", "AIR-1", "doing"),
    ("story", "Board: columns fill the page width", "AIR-2", "todo"),
    ("story", "Board: panel width", "AIR-2", "approved"),
    ("story", "Jira import keeps its keys", "AIR-2", "review"),
    ("story", "Sign-in remembers the device", "AIR-2", "blocked"),
    ("story", "Chat streams its answer", "AIR-2", "done"),
)
"""The board every test starts from: (level, title, parent, status), keyed AIR-1.. in this order."""
EVIDENCE = Path("test-results") / "evidence"
"""Where each test's screenshots and index.md go, beside pytest-playwright's own failure artifacts."""
OUTLINE = "3px solid #d9546e"
"""How the element a check is about is marked in its screenshot."""


@dataclass
class Upstream:
    """The engine behind the test server: answers every chat completion with `reply`, as a stream or whole,
    and keeps the bodies it was sent."""

    reply: str = "ok"
    requests: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if body.get("stream"):
            frames = [{"choices": [{"index": 0, "delta": {"content": self.reply}, "finish_reason": None}]}]
            frames.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            sse = "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"
            return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})
        message = {"role": "assistant", "content": self.reply}
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "stop"}]})


@dataclass(frozen=True)
class Server:
    url: str
    state: Path
    upstream: Upstream


def _config(root: Path) -> AirlieConfig:
    """The least airlie.yaml the app starts with: one engine it never reaches (the board needs none), the
    roles the config requires, and the state under `root`. Its own, so the image needs no engine packages."""
    roles = ("coder", "summarizer", "judge", "aggregator")
    for r in roles:
        (root / f"{r}.md").write_text("x")
    (root / "chat.md").write_text("---\nroutable: false\n---\nYou are the chat role.\n")
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://engine.test/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": {
            **{r: {"prompt": f"{r}.md", "model": "base"} for r in roles},
            "chat": {"prompt": "chat.md", "model": "base", "skills": str(CHAT_SKILLS), "vision": True},
        },
        "chat": {"attachments": {"max_file_mb": 1}},  # small, so a too-large file is quick to make
        "observability": {"state_dir": str(root / "state")},
    }
    (root / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(root / "airlie.yaml")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _seed(state: Path) -> None:
    board = BoardStore(state / "board.sqlite")
    try:
        for level, title, parent, status in CARDS:
            item = board.create(level=level, title=title, parent=parent, labels=["repo:weyenk/airlie"])
            if status != "todo":
                board.move(item.key, status)
    finally:
        board.close()


@pytest.fixture(scope="session")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    """The server the read-only checks share: nothing may change its board."""
    yield from _serve(tmp_path_factory.mktemp("airlie"))


@pytest.fixture
def own_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    """A server of the test's own, on the same board, for a test that changes the board."""
    yield from _serve(tmp_path_factory.mktemp("airlie"))


def _serve(root: Path) -> Iterator[Server]:
    config = _config(root)
    state = config.observability.state_dir.expanduser()
    state.mkdir(parents=True, exist_ok=True)
    _seed(state)
    upstream = Upstream()
    app = create_app(config, transport=httpx.MockTransport(upstream), api_key=KEY)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not srv.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("the test server did not start")
        time.sleep(0.05)
    yield Server(f"http://127.0.0.1:{port}", state, upstream)
    srv.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def signed_in(context: BrowserContext, page: Page, server: Server) -> Page:
    """A page whose browser holds the admin key, as Sign in leaves it."""
    context.add_init_script(f"localStorage.setItem('airlie.key', {KEY!r})")
    return page


@pytest.fixture
def errors(page: Page) -> list[str]:
    """What the page logs as an error or throws, collected while the test runs."""
    seen: list[str] = []
    page.on("console", lambda m: seen.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: seen.append(str(e)))
    return seen


def _slug(text: str, limit: int = 60) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit] or "check"


@dataclass
class Evidence:
    """A test's checks, each one a screenshot. `expect(target, what)` is Playwright's expect, and every
    assertion called on it is recorded; `shot(what, target)` records a plain `assert` made just before it."""

    page: Page
    folder: Path
    shots: list[tuple[str, str, bool]] = field(default_factory=list)

    def shot(self, what: str, target: Locator | None = None, *, held: bool = True) -> Path:
        path = self.folder / f"{len(self.shots) + 1:02d}-{_slug(what)}.png"
        self.folder.mkdir(parents=True, exist_ok=True)
        marked = False
        if target is not None and target.count():
            target.evaluate_all(
                f"els => els.forEach(e => {{ e.dataset.e2eOutline = e.style.outline; "
                f"e.style.outline = {OUTLINE!r}; }})"
            )
            marked = True
        try:
            self.page.screenshot(path=str(path), full_page=True)
        finally:
            if marked and target is not None:
                target.evaluate_all(
                    "els => els.forEach(e => { e.style.outline = e.dataset.e2eOutline || ''; "
                    "delete e.dataset.e2eOutline; })"
                )
        self.shots.append((what, path.name, held))
        return path

    def expect(self, target: Page | Locator, what: str) -> Any:
        return _Checked(self, target, what)

    def write_index(self, test: str) -> None:
        if not self.shots:
            return
        lines = [f"# {test}", ""]
        for i, (what, name, held) in enumerate(self.shots, 1):
            lines += [f"{i}. {'held' if held else 'FAILED'}: {what}", "", f"   ![{what}]({name})", ""]
        (self.folder / "index.md").write_text("\n".join(lines))


class _Checked:
    """Playwright's assertions on `target`, each followed by its screenshot (taken on a failure too)."""

    def __init__(self, evidence: Evidence, target: Page | Locator, what: str) -> None:
        self._evidence, self._target, self._what = evidence, target, what

    def __getattr__(self, name: str) -> Callable[..., None]:
        check = getattr(expect(self._target), name)
        outline = self._target if isinstance(self._target, Locator) else None

        def checked(*args: Any, **kwargs: Any) -> None:
            label = f"{self._what} ({name.replace('_', ' ')})"
            try:
                check(*args, **kwargs)
            except AssertionError:
                self._evidence.shot(label, outline, held=False)
                raise
            self._evidence.shot(label, outline)

        return checked


@pytest.fixture
def evidence(page: Page, request: pytest.FixtureRequest) -> Iterator[Evidence]:
    # the whole test id: cut short, the cases of one parametrized test would share a folder
    found = Evidence(page, EVIDENCE / _slug(request.node.nodeid.split("::", 1)[-1], limit=200))
    yield found
    found.write_index(request.node.nodeid)
