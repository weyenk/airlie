"""What a story-mode integration test builds around `airlie story run`: a sandbox checkout with a Test gate, a
story on the local board that names it, an airlie.yaml for it, scripted models behind the real HTTPEngine, and
a recorded gh in place of GitHub.

Everything else is the code a person runs: the CLI, the story factory, the pipeline, the studio, the tools,
the test gate and the layer checks. Only the network ends are replaced: the engine's HTTP transport and gh."""

from __future__ import annotations

import json
import sys
from collections import deque
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from airlie.board import BoardStore
from airlie.config import AirlieConfig, load_config
from airlie.engine import FakeReply
from airlie.engine import http_engine as http_engine_module
from airlie.story import factory
from airlie.story.publish import Publisher
from tests.unit.git_helpers import git
from tests.unit.test_story_pipeline import STORY_ROLES, RecordedGh, agents_md

ROOT = Path(__file__).resolve().parents[2]
GITHUB_REPO = "o/calc"
TRUNK = "main"
BASE_URL = "http://models.test/v1"
CALC = "def add(a, b):\n    return a + b\n"
TEST_CALC = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def gate_command() -> str:
    """The sandbox's Test gate: pytest under this interpreter, which has it. A bare `python` would be
    whichever the agent environment finds first on PATH, and the macOS system one has no pytest."""
    return f"{sys.executable} -m pytest -q -p no:cacheprovider"


def sandbox_checkout(repos_root: Path) -> Path:
    """The clone of github.com/o/calc in `repos_root`, as `story.repos_root` finds it for a repo:o/calc label:
    calc.py with add, a passing test, an AGENTS.md whose Test gate runs pytest, origin naming the repository
    and origin/main where main is (as a fresh clone has it)."""
    repo = repos_root / GITHUB_REPO.split("/")[1]
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("# calc\n\nArithmetic helpers.\n")
    (repo / "calc.py").write_text(CALC)
    (repo / "test_calc.py").write_text(TEST_CALC)
    (repo / "pytest.ini").write_text("[pytest]\n")  # pytest's rootdir is the checkout, not a parent of tmp
    (repo / "AGENTS.md").write_text(agents_md(gate_command()))
    git(repo, "init", "-q", "-b", TRUNK)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    git(repo, "remote", "add", "origin", f"git@github.com:{GITHUB_REPO}.git")
    git(repo, "update-ref", f"refs/remotes/origin/{TRUNK}", TRUNK)
    return repo


def board_story(state_dir: Path, title: str, description: str) -> str:
    """A story on the local board (the default board, in state_dir) labelled for the sandbox; its key."""
    store = BoardStore(state_dir / "board.sqlite")
    try:
        item = store.create(
            level="story", title=title, description=description, labels=[f"repo:{GITHUB_REPO}"]
        )
        store.move(item.key, "approved")
        return item.key
    finally:
        store.close()


def board_item(state_dir: Path, key: str) -> Any:
    store = BoardStore(state_dir / "board.sqlite")
    try:
        return store.get(key)
    finally:
        store.close()


def story_yaml(tmp_path: Path, repos_root: Path) -> Path:
    """An airlie.yaml for the scripted models: the story roles, each on a model of its own (`m-<role>`) so the
    scripted server knows who is asking, the local board, the studio with one review round, and the story
    settings the unit tests use (no survey, no planning rounds), so each role's script is short."""
    d = tmp_path / "cfg"
    d.mkdir()
    roles: dict[str, dict[str, object]] = {}
    for name, (text, tools) in STORY_ROLES.items():
        (d / f"{name}.md").write_text(text)
        roles[name] = {"prompt": f"{name}.md", "model": f"m-{name}", "thinking": False}
        if tools is not None:
            roles[name]["tools"] = tools
    data = {
        "version": 1,
        "engines": {
            "main": {"kind": "openai_http", "base_url": BASE_URL, "models": [f"m-{r}" for r in roles]}
        },
        "models": {f"m-{r}": {"repo": f"org/{r}", "revision": "r"} for r in roles},
        "roles": roles,
        "teams": {"orchestrator": "native", "max_steps": 6},
        "tools": {"builtin": ["read_file", "write_file", "list_dir", "run_tests"]},
        "topology": {"kind": "studio"},
        "router": {"kind": "chief"},
        "studio": {
            "parallel": 2,
            "review": {"enabled": True, "roles": ["coder"], "rounds": 1},
            "done_check": False,
        },
        "repo_map": {"enabled": False},
        "observability": {"state_dir": str(tmp_path / "state")},
        "story": {
            "trunk": TRUNK,
            "repos_root": str(repos_root),
            "board": "local",
            "github_app": {"app_id": None},
            "plan_round_steps": 0,
            "survey": False,
            "amigos": {"enabled": False},  # the story meeting has tests of its own
        },
    }
    path = d / "airlie.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def _sse(reply: FakeReply) -> bytes:
    """`reply` as vllm-mlx streams it: content, then each tool call, then the finish reason and the usage."""
    frames: list[dict[str, Any]] = []
    if reply.text:
        frames.append({"choices": [{"index": 0, "delta": {"content": reply.text}, "finish_reason": None}]})
    for i, call in enumerate(reply.tool_calls):
        fn = {"name": call.name, "arguments": call.arguments}
        delta = {"tool_calls": [{"index": i, "id": call.id, "type": "function", "function": fn}]}
        frames.append({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
    finish = reply.finish_reason or ("tool_calls" if reply.tool_calls else "stop")
    frames.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    frames.append({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    return b"".join(b"data: " + json.dumps(f).encode() + b"\n\n" for f in frames) + b"data: [DONE]\n\n"


class ScriptedModels:
    """An OpenAI-compatible engine as an httpx transport handler: each role's replies in order, chosen by the
    model the request names (`m-<role>`), streamed as server-sent events. A role with no reply left gets a
    500 that names it, so a pipeline that asks for more than the script says so in the stop reason."""

    def __init__(self, scripts: Mapping[str, list[FakeReply]]) -> None:
        self.scripts = {role: deque(replies) for role, replies in scripts.items()}
        self.asked: list[str] = []

    def left(self) -> dict[str, int]:
        """The replies each role was scripted with and never asked for."""
        return {role: len(q) for role, q in self.scripts.items() if q}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "healthy", "engine_type": "scripted"})
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": f"m-{r}"} for r in self.scripts]})
        if path != "/v1/chat/completions":
            return httpx.Response(404, json={"error": {"message": f"no route {path}"}})
        role = str(json.loads(request.content)["model"]).removeprefix("m-")
        self.asked.append(role)
        queue = self.scripts.get(role)
        if not queue:
            return httpx.Response(500, json={"error": {"message": f"no scripted reply left for role {role}"}})
        return httpx.Response(
            200, content=_sse(queue.popleft()), headers={"content-type": "text/event-stream"}
        )


def wire(monkeypatch: pytest.MonkeyPatch, gh: RecordedGh, models: ScriptedModels | None = None) -> None:
    """gh is `gh`; with `models`, every HTTPEngine the CLI builds talks to them instead of the network."""
    monkeypatch.setattr(factory, "Publisher", partial(Publisher, run=gh))
    if models is not None:
        scripted = partial(http_engine_module.HTTPEngine, transport=httpx.MockTransport(models))
        monkeypatch.setattr(http_engine_module, "HTTPEngine", scripted)


def events(story_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (story_dir / "events.jsonl").read_text().splitlines()]


def live_story_config(tmp_path: Path) -> AirlieConfig:
    """This checkout's airlie.yaml (with airlie.local.yaml) as a story run loads it, with this test's own
    state directory (so its own board, stories and story lock) and repos_root, and no GitHub App: gh is
    recorded."""
    cfg = factory.story_config(load_config(ROOT / "airlie.yaml"))
    story = cfg.story.model_copy(
        update={
            "board": "local",
            "repos_root": tmp_path / "git",
            "github_app": cfg.story.github_app.model_copy(update={"app_id": None}),
        }
    )
    observability = cfg.observability.model_copy(update={"state_dir": tmp_path / "state"})
    return cfg.model_copy(update={"story": story, "observability": observability})


def engines_down(cfg: AirlieConfig) -> list[str]:
    """The engines of `cfg` whose /health does not answer 200."""
    down = []
    for name, e in cfg.engines.items():
        try:
            ok = httpx.get(f"{(e.base_url or '').removesuffix('/v1')}/health", timeout=3).status_code == 200
        except httpx.HTTPError:
            ok = False
        if not ok:
            down.append(name)
    return down
