"""Shared test helpers for tests.support.story_layer_checks_flow."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.story.board import text_card
from airlie.story.state import StoryState
from tests.support.git import git
from tests.support.story import STORY, agents_md, fenced, kinds, package_reply, pipeline
from tests.support.studio import APPROVE, DONE

# Re-exported for test modules that imported DONE from test_story_layer_checks_flow before the move.
__all__ = ["DONE"]

PYTEST = f"{shlex.quote(sys.executable)} -m pytest -q"

LAYERS = fenced(
    {
        "layers": [
            {
                "slug": "mul",
                "title": "Multiply",
                "scope": "calc.py",
                "acceptance": ["AC1", "AC2"],
                "brief": "Add mul.",
            }
        ]
    }
)

CALC = "def add(a, b):\n    return a + b\n"

TEST_MUL = "from calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"

TEST_ADD_AGAIN = "from calc import add\n\n\ndef test_add_again():\n    assert add(2, 2) == 4\n"

MUL_INTERFACE = {"file": "calc.py", "signature": "def mul(a, b) -> int", "behaviour": "the product"}

STEPS = ["add mul(a, b) to calc.py returning a * b"]


def write(path: str, content: str) -> FakeReply:
    return FakeReply(
        tool_calls=[ToolCall(path, "write_file", json.dumps({"path": path, "content": content}))]
    )


def package(
    text: str = TEST_MUL, name: str = "test_mul", path: str = "tests/test_calc.py", **kw: object
) -> FakeReply:
    """The architect's package for the calc layer: one test file that covers both criteria, calc.py to
    change, mul declared."""
    return package_reply(
        {path: text},
        {"AC1": [f"{path}::{name}"], "AC2": [f"{path}::{name}"]},
        files=[{"path": "calc.py", "action": "modify"}],
        interfaces=[MUL_INTERFACE],
        steps=STEPS,
        **kw,  # type: ignore[arg-type]
    )


FEATURE = write("calc.py", CALC + "\n\ndef mul(a, b):\n    return a * b\n")

BROKEN = write("calc.py", "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n")

FIXED = write("calc.py", CALC + "\n\n# fixed after the gate failed\ndef mul(a, b):\n    return a * b\n")

STILL_BROKEN = write("notes.txt", "tried\n")

RED = package()

VACUOUS = package(TEST_ADD_AGAIN, "test_add_again")

OTHER_RED = package(
    "from calc import mul\n\n\ndef test_mul_zero():\n    assert mul(0, 3) == 0\n", "test_mul_zero"
)


def calc_repo(tmp_path: Path, gate: tuple[str, ...] = (), base_test: str = "assert add(1, 2) == 3") -> Path:
    """The calc repository; with `gate`, an AGENTS.md whose Test gate runs it and a strict ruff.toml.
    `base_test` is the existing test's assertion."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "pytest.ini").write_text("[pytest]\n")
    if gate:
        (repo / "AGENTS.md").write_text(agents_md(*gate))
        (repo / "ruff.toml").write_text('line-length = 60\n\n[lint]\nselect = ["E", "F", "I"]\n')
    (repo / "calc.py").write_text(CALC)
    (repo / "tests" / "test_base.py").write_text(
        f"from calc import add\n\n\ndef test_add():\n    {base_test}\n"
    )
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return repo


async def run(
    tmp_path: Path,
    architect_build: list[FakeReply] | None = None,
    coder: list[FakeReply] | None = None,
    packages: list[FakeReply] | None = None,
    test_cmd: str = PYTEST,
    gate: tuple[str, ...] = (),
    base_test: str = "assert add(1, 2) == 3",
    story: dict[str, object] | None = None,
) -> tuple[StoryState, FakeEngine]:
    """The one-layer story: the architect's `packages` (default: one red one) after its layer plan, then the
    coder's replies. With `gate`, the repository's AGENTS.md gate runs instead of `test_cmd`; `base_test` as
    in calc_repo; `story` is more story settings."""
    build = architect_build if architect_build is not None else (coder or [])
    scripts = {
        "product": [STORY],
        "architect": [LAYERS, *(packages if packages is not None else [RED]), *build],
        "reviewer": [APPROVE, APPROVE],
    }
    engine = FakeEngine(scripts)
    repo = calc_repo(tmp_path, gate, base_test)
    cmd = None if gate else test_cmd
    st = await pipeline(tmp_path, repo, engine, test_cmd=cmd, story=story).run(text_card("multiply"))
    return st, engine


def steers(st: StoryState) -> list[tuple[str, str, str]]:
    """The story's `steer` events: the hub texts the pipeline sent a layer's roles (id, role, layer)."""
    return [(e["id"], e["role"], e["layer"]) for e in kinds(st.path / "events.jsonl", "steer")]


def built_file(tmp_path: Path, st: StoryState, path: str) -> str:
    return git(tmp_path / "repo", "show", f"{st.built['01']['branch']}:{path}")


RUFF = f"{shlex.quote(sys.executable)} -m ruff"

RUFF_GATE = (f"{RUFF} check .", f"{RUFF} format --check .", PYTEST)
