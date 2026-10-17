"""A layer whose new tests do not test its change: the coder gets one retry with the findings, then the layer
stops like a failed gate. On FakeEngine, a NullBoard and a recorded gh, with pytest as the gate."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.story.board import text_card
from airlie.story.state import StoryState
from tests.unit.git_helpers import git
from tests.unit.test_story_pipeline import STORY, fenced, kinds, pipeline
from tests.unit.test_studio_orchestrator import APPROVE, DONE

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


def write(path: str, content: str) -> FakeReply:
    return FakeReply(
        tool_calls=[ToolCall(path, "write_file", json.dumps({"path": path, "content": content}))]
    )


FEATURE = write("calc.py", CALC + "\n\ndef mul(a, b):\n    return a * b\n")
VACUOUS = write(
    "tests/test_calc.py", "from calc import add\n\n\ndef test_add_again():\n    assert add(2, 2) == 4\n"
)
RED = write("tests/test_calc.py", "from calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n")
NO_ASSERT = write("tests/test_calc.py", "from calc import mul\n\n\ndef test_mul():\n    mul(2, 3)\n")


def calc_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "pytest.ini").write_text("[pytest]\n")
    (repo / "calc.py").write_text(CALC)
    (repo / "tests" / "test_base.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return repo


async def run(
    tmp_path: Path, coder: list[FakeReply], test_cmd: str = PYTEST
) -> tuple[StoryState, FakeEngine]:
    engine = FakeEngine(
        {"product": [STORY], "architect": [LAYERS], "coder": coder, "reviewer": [APPROVE, APPROVE]}
    )
    st = await pipeline(tmp_path, calc_repo(tmp_path), engine, test_cmd=test_cmd).run(text_card("multiply"))
    return st, engine


def coder_prompts(engine: FakeEngine) -> list[str]:
    return [r.messages[-1].content for r in engine.requests if r.role == "coder"]


async def test_a_vacuous_test_gets_one_retry_with_the_findings_then_blocks_the_layer(tmp_path: Path) -> None:
    st, engine = await run(tmp_path, [FEATURE, VACUOUS, DONE, DONE])
    assert st.blocked is not None and st.stage == "layers" and "01" not in st.built
    assert "layer 01 (Multiply)" in st.blocked and "after one retry" in st.blocked
    assert "tests/test_calc.py::test_add_again" in st.blocked and "passes on the layer's base" in st.blocked
    # the retry: the coder was told which test and why
    retry = [p for p in coder_prompts(engine) if "tests/test_calc.py::test_add_again" in p]
    assert len(retry) == 1 and "passes on the layer's base" in retry[0]
    events = st.path / "events.jsonl"
    found = kinds(events, "vacuous_test")
    assert [(e["layer"], e["attempt"], e["test"]) for e in found] == [
        ("01", 1, "tests/test_calc.py::test_add_again"),
        ("01", 2, "tests/test_calc.py::test_add_again"),
    ]
    assert "def test_add_again" in found[0]["source"] and found[0]["studio_run"]
    assert [e["findings"] for e in kinds(events, "test_checks_retry")] == [1]
    # the note on the issue carries the findings; the gate never ran on a layer that failed its checks
    notes = (st.path / "notes.md").read_text()
    assert "tests/test_calc.py::test_add_again" in notes and "moved to blocked" in notes
    assert kinds(events, "layer_built") == []


async def test_a_retry_that_fixes_the_tests_builds_the_layer(tmp_path: Path) -> None:
    st, engine = await run(tmp_path, [FEATURE, VACUOUS, DONE, RED, DONE])
    assert st.blocked is None and st.stage == "review" and "01" in st.built
    events = st.path / "events.jsonl"
    assert [e["attempt"] for e in kinds(events, "vacuous_test")] == [1]
    assert len(kinds(events, "test_checks_retry")) == 1 and len(kinds(events, "layer_built")) == 1
    built = git(tmp_path / "repo", "show", f"{st.built['01']['branch']}:tests/test_calc.py")
    assert built.endswith("assert mul(2, 3) == 6")


async def test_an_assertion_free_test_is_a_finding_too(tmp_path: Path) -> None:
    st, _ = await run(tmp_path, [FEATURE, NO_ASSERT, DONE, DONE])
    assert st.blocked is not None and "tests/test_calc.py::test_mul" in st.blocked
    found = kinds(st.path / "events.jsonl", "assertion_free_test")
    assert [(e["attempt"], e["test"]) for e in found] == [
        (1, "tests/test_calc.py::test_mul"),
        (2, "tests/test_calc.py::test_mul"),
    ]


async def test_good_tests_pass_the_checks_first_time(tmp_path: Path) -> None:
    st, engine = await run(tmp_path, [FEATURE, RED, DONE])
    assert st.blocked is None and "01" in st.built
    events = st.path / "events.jsonl"
    assert kinds(events, "test_checks_retry") == [] and kinds(events, "vacuous_test") == []
    assert [e["layer"] for e in kinds(events, "test_checks_passed")] == ["01"]


async def test_a_gate_without_pytest_skips_the_checks_and_says_so(tmp_path: Path) -> None:
    # the same vacuous test, but the gate is not pytest: no check runs, nothing fails for it
    st, _ = await run(tmp_path, [FEATURE, VACUOUS, DONE], test_cmd="true")
    assert st.blocked is None and "01" in st.built
    [skipped] = kinds(st.path / "events.jsonl", "test_checks_skipped")
    assert skipped["layer"] == "01" and "pytest" in skipped["reason"]
