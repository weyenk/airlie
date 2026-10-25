"""The web app in a real browser: airlie serve's app on a free port of this machine, with a fake engine behind
it and a board of known cards, driven by Playwright's Chromium. Run in the e2e container (`task test:e2e`),
which has the browser; the host gate leaves these out (the `e2e` marker).

Every check a test makes goes through `evidence`: it passes or fails as Playwright's would, and leaves a
screenshot of the page at that moment, the element checked outlined, in test-results/evidence/<test>/ with an
index.md that lists them in order with what each one checked and whether it held."""

from __future__ import annotations

import asyncio
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
import yaml
from playwright.sync_api import BrowserContext, Locator, Page, expect

from airlie.board.store import BoardStore
from airlie.config import AirlieConfig, load_config
from airlie.obs.run_dir import RunDir, new_run_id
from airlie.serve.app import create_app
from airlie.serve.server import Server as AirlieServer
from airlie.topology.studio import PlanOutcome
from airlie.topology.studio_plan import Plan, Subtask

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
    data["roles"]["coder"]["summary"] = "writes the code"  # a role the studio may route a subtask to
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


STORY_KEY = "AIR-4"
"""The board card ("Board: panel width") with a story behind it: layer 01 built, layer 02 stopped."""


def _seed_story(state: Path) -> None:
    """What a story leaves in state/stories/<key>/ and the runs it names: enough for the workflow screen and
    the ticket panel (acceptance criteria, layers, a retried check, the tester's recorded prompt)."""

    def events(path: Path, rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def step(run: str, team: str, n: int) -> dict[str, Any]:
        return {
            "ts": 1.0,
            "run_id": run,
            "kind": "step",
            "team_id": team,
            "step": n,
            "tool": "read_file",
            "ok": True,
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "prefill_ms": 500.0,
            "gen_ms": 2000.0,
            "role": "tester" if team == "tests" else "coder",
            "model": "base",
        }

    story = state / "stories" / STORY_KEY
    layers = [
        {
            "slug": "seam",
            "title": "Resizable panel",
            "scope": "board.js",
            "brief": "drag handle",
            "acceptance": ["AC1"],
        },
        {
            "slug": "full",
            "title": "Full screen toggle",
            "scope": "board.js",
            "brief": "toggle",
            "acceptance": ["AC2"],
        },
    ]
    criteria = [
        {
            "id": "AC1",
            "given": "an open panel",
            "when": "the handle is dragged",
            "then": "the panel width follows",
        },
        {
            "id": "AC2",
            "given": "an open panel",
            "when": "full screen is chosen",
            "then": "the panel fills the window",
        },
    ]
    (story).mkdir(parents=True)
    (story / "state.json").write_text(
        json.dumps(
            {
                "card": {"key": STORY_KEY, "title": "Board: panel width"},
                "repo": "/r",
                "stage": "layers",
                "story": {"title": "Board: panel width", "summary": "s", "acceptance": criteria},
                "layers": layers,
                "built": {"01": {"branch": "b1", "sha": "abc", "run_id": "run-l1"}},
                "blocked": "layer 02 (Full screen toggle): its tests still do not test its change",
                "runs": ["run-l1", "run-l2"],
            }
        )
    )
    events(
        story / "events.jsonl",
        [
            {"ts": 10.0, "kind": "stage", "stage": "intake"},
            {"ts": 12.0, "kind": "run", "stage": "refine", "studio_run": "run-refine"},
            {"ts": 40.0, "kind": "stage", "stage": "refine"},
            {"ts": 41.0, "kind": "run", "stage": "layers", "studio_run": "run-layers"},
            {"ts": 90.0, "kind": "stage", "stage": "layers"},
            {"ts": 91.0, "kind": "layer_start", "layer": "01", "studio_run": "run-l1"},
            {"ts": 150.0, "kind": "test_checks_passed", "layer": "01", "attempt": 1},
            {"ts": 300.0, "kind": "layer_built", "layer": "01", "branch": "b1", "sha": "abc"},
            {"ts": 301.0, "kind": "layer_start", "layer": "02", "studio_run": "run-l2"},
            {"ts": 340.0, "kind": "no_new_test", "layer": "02", "attempt": 1, "why": "no new test"},
            {"ts": 341.0, "kind": "test_checks_retry", "layer": "02", "findings": 1},
            {"ts": 380.0, "kind": "no_new_test", "layer": "02", "attempt": 2, "why": "no new test"},
            {"ts": 381.0, "kind": "blocked", "stage": "layers", "reason": "layer 02 stopped"},
        ],
    )
    runs = state / "runs"
    events(runs / "run-refine" / "events.jsonl", [step("run-refine", "refine", 1)])
    events(runs / "run-layers" / "events.jsonl", [step("run-layers", "layers", 1)])
    events(
        runs / "run-l1" / "events.jsonl",
        [
            {"ts": 91.0, "kind": "subtask_start", "subtask": "tests", "role": "tester"},
            step("run-l1", "tests", 1),
            {
                "ts": 150.0,
                "kind": "subtask_done",
                "subtask": "tests",
                "role": "tester",
                "ok": True,
                "stop_reason": "done",
                "steps": 1,
                "fix_rounds": 0,
            },
            {"ts": 151.0, "kind": "subtask_start", "subtask": "build", "role": "coder"},
            step("run-l1", "build", 1),
            {
                "ts": 290.0,
                "kind": "subtask_done",
                "subtask": "build",
                "role": "coder",
                "ok": True,
                "stop_reason": "done",
                "steps": 1,
                "fix_rounds": 0,
            },
        ],
    )
    events(
        runs / "run-l2" / "events.jsonl",
        [
            {"ts": 301.0, "kind": "subtask_start", "subtask": "tests", "role": "tester"},
            {
                "ts": 340.0,
                "kind": "subtask_done",
                "subtask": "tests",
                "role": "tester",
                "ok": True,
                "stop_reason": "done",
                "steps": 3,
                "fix_rounds": 0,
            },
        ],
    )
    tests = runs / "run-l1" / "trace" / "tests"
    tests.mkdir(parents=True)
    (tests / "prompt.json").write_text(
        json.dumps(
            {
                "role": "tester",
                "attempt": 0,
                "messages": [
                    {"role": "system", "content": "You are the tester."},
                    {"role": "user", "content": "Goal: write the failing tests for the drag handle"},
                ],
            }
        )
    )
    (tests / "step-001.json").write_text(
        json.dumps(
            {
                "u_t": {"name": "read_file", "arguments": "{}"},
                "o_t": "1: x",
                "ok": True,
                "assistant_text": "",
                "reasoning": "start from the existing board tests",
            }
        )
    )


TOPO_DONE = "20261023-100000-done"
"""A finished studio run with its hand-offs: the coder fixed, reviewed twice, the docs subtask, synthesis."""
TOPO_LIVE = "20261023-110000-live"
"""A studio run still going: the coder on its second step, the docs subtask waiting. A test appends to it."""


def _topology_events(run: str, *, done: bool) -> list[dict[str, Any]]:
    def e(kind: str, ts: float, **f: Any) -> dict[str, Any]:
        return {"ts": ts, "run_id": run, "kind": kind, **f}

    def step(team: str, n: int, ts: float, tool: str) -> dict[str, Any]:
        return e(
            "step",
            ts,
            team_id=team,
            step=n,
            tool=tool,
            ok=True,
            prompt_tokens=1200,
            completion_tokens=150,
            model="base",
            role="coder" if team == "fix" else "docs",
        )

    def hand(src: str, dst: str, what: str, text: str, ts: float) -> dict[str, Any]:
        return e("handoff", ts, src=src, dst=dst, what=what, text=text)

    rows = [
        e("run_start", 1792750000.0, goal="Fix the build", k=1, repo="/r/airlie", orchestrator="studio"),
        e("plan", 1792750001.0, subtasks=["fix", "note"], roles=["coder", "docs"]),
        hand("chief", "fix", "brief", "make the check pass", 1792750002.0),
        e("subtask_start", 1792750002.0, subtask="fix", role="coder", skill="python", writes=True),
        step("fix", 1, 1792750003.0, "read_file"),
        step("fix", 2, 1792750004.0, "write_file"),
    ]
    if not done:
        return rows
    return rows + [
        hand("fix", "reviewer", "diff", "1 file changed: PASS", 1792750005.0),
        e("review", 1792750006.0, subtask="fix", verdict="approve", findings=0, blocking=0, round=0),
        hand("reviewer", "fix", "findings", "approve: no findings", 1792750006.0),
        e("subtask_done", 1792750007.0, subtask="fix", role="coder", ok=True, stop_reason="done", steps=2),
        hand("fix", "chief", "result", "wrote PASS; the check passes", 1792750007.0),
        hand("chief", "note", "brief", "describe the repo", 1792750008.0),
        e("subtask_start", 1792750008.0, subtask="note", role="docs", writes=False),
        step("note", 1, 1792750009.0, "read_file"),
        e("subtask_done", 1792750010.0, subtask="note", role="docs", ok=True, stop_reason="done", steps=1),
        hand("note", "chief", "result", "the repo holds a check", 1792750010.0),
        e("synthesis", 1792750011.0, chars=8, open_questions=[]),
        hand("chief", "synthesis", "answer", "all done", 1792750011.0),
        e("run_done", 1792750012.0, ok=True, wall_ms=12000.0, subtasks=2, tests=True),
    ]


RUN_TEAMS = "20261022-100000-teams"
"""A finished teams run: three teams side by side (the first failed, the second won), then the judge."""
RUN_FORK = "20261022-110000-fork"
"""A finished fork run: the agent got stuck and forked twice; the first fork won."""
RUN_SINGLE = "20261022-120000-single"
"""A finished single run: one agent."""


def _write_events(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _seed_topologies(state: Path) -> None:
    """One finished run of each topology the workflow view draws (the studio is TOPO_DONE)."""

    def e(run: str, kind: str, ts: float, **f: Any) -> dict[str, Any]:
        return {"ts": ts, "run_id": run, "kind": kind, **f}

    def step(run: str, team: str, n: int, ts: float) -> dict[str, Any]:
        return e(
            run,
            "step",
            ts,
            team_id=team,
            step=n,
            tool="read_file",
            ok=True,
            prompt_tokens=900,
            completion_tokens=90,
            model="base",
            role="coder",
        )

    t, run = 1792740000.0, RUN_TEAMS
    _write_events(
        state / "runs" / run / "events.jsonl",
        [
            e(
                run,
                "run_start",
                t,
                goal="Speed up repo map rebuilds",
                k=3,
                repo="/r/airlie",
                orchestrator="native",
            ),
            *(e(run, "team_start", t + 1, team_id=f"t{i}", worktree=f"/w/{i}") for i in (1, 2, 3)),
            *(step(run, f"t{i}", 1, t + 2) for i in (1, 2, 3)),
            e(run, "team_done", t + 3, team_id="t1", ok=False, stop_reason="tests failed", steps=1),
            e(run, "team_done", t + 3, team_id="t2", ok=True, stop_reason="done", steps=1),
            e(run, "team_done", t + 3, team_id="t3", ok=True, stop_reason="done", steps=1),
            e(
                run,
                "aggregate",
                t + 4,
                winner="t2",
                reason="tests pass",
                scores={"t1": 0.2, "t2": 1.0, "t3": 0.8},
            ),
            e(run, "run_done", t + 5, ok=True, wall_ms=5000.0),
        ],
    )
    run = RUN_FORK
    _write_events(
        state / "runs" / run / "events.jsonl",
        [
            e(
                run,
                "run_start",
                t,
                goal="Parse the new tool-call format",
                topology="fork",
                repo="/r/airlie",
                width=2,
            ),
            e(run, "branch_start", t + 1, branch="b0", worktree="/w/b0", child=False),
            step(run, "b0", 1, t + 2),
            e(run, "branch_done", t + 3, branch="b0", ok=False, stop_reason="stuck", steps=1),
            e(
                run,
                "fork_point",
                t + 3.1,
                round=1,
                parent="b0",
                reason="stuck",
                children=["b1", "b2"],
                hypotheses=["the parser is wrong", "the test is wrong"],
            ),
            e(run, "branch_start", t + 4, branch="b1", worktree="/w/b1", child=True),
            e(run, "branch_start", t + 4, branch="b2", worktree="/w/b2", child=True),
            step(run, "b1", 1, t + 5),
            e(run, "branch_done", t + 6, branch="b1", ok=True, stop_reason="done", steps=1),
            e(run, "branch_done", t + 6, branch="b2", ok=False, stop_reason="gave up", steps=0),
            e(run, "fork_resolved", t + 7, round=1, winner="b1", how="tests"),
            e(run, "run_done", t + 8, ok=True, wall_ms=8000.0),
        ],
    )
    run = RUN_SINGLE
    _write_events(
        state / "runs" / run / "events.jsonl",
        [
            e(
                run,
                "run_start",
                t,
                goal="Rename the repo map cache key",
                k=1,
                repo="/r/airlie",
                orchestrator="native",
            ),
            e(run, "team_start", t + 1, team_id="t1", worktree="/w/1"),
            step(run, "t1", 1, t + 2),
            e(run, "team_done", t + 3, team_id="t1", ok=True, stop_reason="done", steps=1),
            e(run, "run_done", t + 4, ok=True, wall_ms=4000.0),
        ],
    )


class _FakeStudio:
    """The studio as the Plan and Approve buttons meet it: `plan` returns a plan and writes the events a real
    plan leaves; `execute_task` starts a run that goes on until it is stopped."""

    PLAN = Plan(
        assumptions=(
            "The flake is the upload finishing after the assertion.",
            "No change to the upload API is needed.",
        ),
        subtasks=(
            Subtask(id="fix", brief="Wait for the upload's response before asserting.", role_hint="coder"),
            Subtask(
                id="retry-note",
                brief="Note the wait in the test's docstring.",
                role_hint="coder",
                depends_on=("fix",),
            ),
        ),
    )

    def __init__(self, state: Path) -> None:
        self.run_dir = RunDir.create(state, new_run_id() + "-fake")
        self.path = self.run_dir.events_path

    def _emit(self, kind: str, **f: Any) -> None:
        with self.path.open("a") as out:
            out.write(
                json.dumps({"ts": time.time(), "run_id": self.run_dir.run_id, "kind": kind, **f}) + "\n"
            )

    async def plan(
        self, goal: str, *, repo: Path, test_cmd: str | None = None, images: Any = ()
    ) -> PlanOutcome:
        self._emit("run_start", goal=goal, k=1, repo=str(repo), orchestrator="studio", mode="plan")
        self._emit("plan", subtasks=["fix", "retry-note"], roles=["coder", "coder"])
        self._emit("run_done", ok=True, wall_ms=10.0, subtasks=0, tests=None)
        return PlanOutcome(self.PLAN, "", False)

    async def execute_task(self, goal: str, *, repo: Path, plan: Plan | None = None, **_: Any) -> Any:
        self._emit("run_start", goal=goal, k=1, repo=str(repo), orchestrator="studio")
        self._emit(
            "plan",
            subtasks=[s.id for s in plan.subtasks] if plan else [],
            roles=["coder"] * (len(plan.subtasks) if plan else 0),
        )
        await asyncio.sleep(3600)  # until a person stops it


def _seed_topology(state: Path) -> None:
    for run, done in ((TOPO_DONE, True), (TOPO_LIVE, False)):
        d = state / "runs" / run
        d.mkdir(parents=True, exist_ok=True)
        (d / "events.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in _topology_events(run, done=done))
        )


@pytest.fixture(scope="session")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    """The server the read-only checks share: nothing may change its board."""
    yield from _serve(tmp_path_factory.mktemp("airlie"))


@pytest.fixture(autouse=True)
def _fresh_shared_upstream(request: pytest.FixtureRequest) -> None:
    """The session server's engine stub is shared: a test that scripts a reply or reads the bodies it was sent
    must not hand its leftovers to the next one."""
    if "server" in request.fixturenames:
        upstream = request.getfixturevalue("server").upstream
        upstream.reply = Upstream.reply
        upstream.requests.clear()


@pytest.fixture
def own_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    """A server of the test's own, on the same board, for a test that changes the board."""
    yield from _serve(tmp_path_factory.mktemp("airlie"))


@pytest.fixture
def bootstrap_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    """A fresh install: no admin key and nobody invited, so a request straight from this machine is the
    bootstrap admin (airlie.auth.principal) until the first person is invited."""
    yield from _serve(tmp_path_factory.mktemp("airlie"), api_key=None)


def _serve(root: Path, api_key: str | None = KEY) -> Iterator[Server]:
    config = _config(root)
    state = config.observability.state_dir.expanduser()
    state.mkdir(parents=True, exist_ok=True)
    _seed(state)
    _seed_story(state)
    _seed_topology(state)
    _seed_topologies(state)
    (root / "repo" / ".git").mkdir(parents=True)  # a repository a plan may name
    upstream = Upstream()
    app = create_app(
        config,
        transport=httpx.MockTransport(upstream),
        orchestrator_factory=lambda cfg, k, topology=None: _FakeStudio(state),
        api_key=api_key,
    )
    port = _free_port()
    # airlie's own Server, as `airlie serve` runs: shutdown ends the live feed first, so a page that holds
    # /v1/events open does not make teardown wait for it
    srv = AirlieServer(app, host="127.0.0.1", port=port, log_level="warning")
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
def app_page(signed_in: Page) -> Page:
    """A page signed in for the Vue app served at / (it reads `airlie.key`, which the one init script sets).
    Go to `<server.url>/<path>`: the server is the test's choice (`server`, or `own_server` when it changes
    the board)."""
    return signed_in


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
