"""A layer's tests through the pipeline. With a tester: its tests must fail before any code is written (the
pre-code check, one retry for the tester), are then read-only to the coder, and the coder is done when they
pass; the whole suite is the final gate, with one follow-up for the coder when it fails. Without one: the
coder's tests are read for assertions. On FakeEngine, a NullBoard and a recorded gh, with pytest as the
gate."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.story.board import text_card
from airlie.story.state import StoryState
from tests.unit.git_helpers import git
from tests.unit.test_story_pipeline import STORY, TESTER_ROLES, fenced, kinds, pipeline
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
TEST_MUL = "from calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"


def write(path: str, content: str) -> FakeReply:
    return FakeReply(
        tool_calls=[ToolCall(path, "write_file", json.dumps({"path": path, "content": content}))]
    )


FEATURE = write("calc.py", CALC + "\n\ndef mul(a, b):\n    return a * b\n")
BROKEN = write("calc.py", "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n")
VACUOUS = write(
    "tests/test_calc.py", "from calc import add\n\n\ndef test_add_again():\n    assert add(2, 2) == 4\n"
)
RED = write("tests/test_calc.py", TEST_MUL)
NO_ASSERT = write("tests/test_calc.py", "from calc import mul\n\n\ndef test_mul():\n    mul(2, 3)\n")
LISTED = FakeReply(text="wrote tests/test_calc.py::test_mul: asserts mul(2, 3) == 6")


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
    tmp_path: Path,
    coder: list[FakeReply],
    tester: list[FakeReply] | None = None,
    test_cmd: str = PYTEST,
) -> tuple[StoryState, FakeEngine]:
    """The one-layer story, with a tester when `tester` is given (its replies), else the coder alone."""
    scripts = {"product": [STORY], "architect": [LAYERS], "coder": coder, "reviewer": [APPROVE, APPROVE]}
    if tester is not None:
        scripts["tester"] = tester
    engine = FakeEngine(scripts)
    repo = calc_repo(tmp_path)
    if tester is None:
        story = pipeline(tmp_path, repo, engine, test_cmd=test_cmd)
    else:
        story = pipeline(
            tmp_path, repo, engine, test_cmd=test_cmd, story={"tester_role": "tester"}, roles=TESTER_ROLES
        )
    st = await story.run(text_card("multiply"))
    return st, engine


def prompts(engine: FakeEngine, role: str) -> list[str]:
    return [r.messages[-1].content for r in engine.requests if r.role == role]


def built_file(tmp_path: Path, st: StoryState, path: str) -> str:
    return git(tmp_path / "repo", "show", f"{st.built['01']['branch']}:{path}")


# ---- with a tester: red before the code ----


async def test_the_testers_red_tests_are_checked_locked_and_the_coder_builds_against_them(
    tmp_path: Path,
) -> None:
    st, engine = await run(tmp_path, [FEATURE, DONE], tester=[RED, LISTED])
    assert st.blocked is None and st.stage == "review", st.blocked
    events = st.path / "events.jsonl"
    assert [e["layer"] for e in kinds(events, "test_checks_passed")] == ["01"]
    studio_run = st.built["01"]["run_id"]
    studio_events = tmp_path / "state" / "runs" / studio_run / "events.jsonl"
    [locked] = kinds(studio_events, "locked")
    assert locked["owner"] == "tester" and locked["paths"] == ["tests/test_calc.py"]
    assert built_file(tmp_path, st, "tests/test_calc.py") == TEST_MUL.rstrip("\n")
    # the coder ran after the check, against the tester's tests
    assert "They are the contract" in prompts(engine, "coder")[0]


async def test_a_test_that_passes_before_the_code_goes_back_to_the_tester_once(tmp_path: Path) -> None:
    st, engine = await run(tmp_path, [FEATURE, DONE], tester=[VACUOUS, LISTED, RED, LISTED])
    assert st.blocked is None and "01" in st.built
    events = st.path / "events.jsonl"
    [found] = kinds(events, "passes_before_code")
    assert (found["attempt"], found["test"]) == (1, "tests/test_calc.py::test_add_again")
    retry = [p for p in prompts(engine, "tester") if "tests/test_calc.py::test_add_again" in p]
    assert len(retry) == 1 and "passes before the layer's code is written" in retry[0]
    assert len(kinds(events, "test_checks_retry")) == 1


async def test_tests_still_green_before_the_code_after_the_retry_stop_the_layer_before_the_coder(
    tmp_path: Path,
) -> None:
    st, engine = await run(tmp_path, [FEATURE, DONE], tester=[VACUOUS, LISTED, VACUOUS, LISTED])
    assert st.blocked is not None and "01" not in st.built
    assert "the tester's tests do not hold before the code after one retry" in st.blocked
    assert "test_add_again passes before the code is written" in st.blocked
    assert prompts(engine, "coder") == []  # the coder never started
    assert kinds(st.path / "events.jsonl", "layer_built") == []


async def test_the_coder_cannot_change_the_testers_tests_with_the_file_tools(tmp_path: Path) -> None:
    weaker = write("tests/test_calc.py", "def test_mul():\n    assert True\n")
    st, engine = await run(tmp_path, [weaker, FEATURE, DONE], tester=[RED, LISTED])
    assert st.blocked is None and "01" in st.built
    refused = [
        m.content for r in engine.requests if r.role == "coder" for m in r.messages if m.role == "tool"
    ]
    assert any("you cannot change them" in m for m in refused)
    assert built_file(tmp_path, st, "tests/test_calc.py") == TEST_MUL.rstrip("\n")


# ---- the final gate: the whole suite, with one follow-up ----


async def test_a_failing_final_gate_sends_the_coder_its_output_once(tmp_path: Path) -> None:
    st, engine = await run(tmp_path, [BROKEN, DONE, FEATURE, DONE], tester=[RED, LISTED])
    assert st.blocked is None and "01" in st.built
    follow_up = [p for p in prompts(engine, "coder") if "the whole suite fails with your change" in p]
    assert len(follow_up) == 1 and "test_add" in follow_up[0]
    studio_events = tmp_path / "state" / "runs" / st.built["01"]["run_id"] / "events.jsonl"
    assert [e["follow_up"] for e in kinds(studio_events, "gate_failed")] == ["build"]
    assert "return a + b" in built_file(tmp_path, st, "calc.py")


async def test_a_final_gate_still_failing_after_the_follow_up_stops_the_layer(tmp_path: Path) -> None:
    st, engine = await run(tmp_path, [BROKEN, DONE, DONE], tester=[RED, LISTED])
    assert st.blocked is not None and "01" not in st.built
    assert "fails on" in st.blocked
    assert len([p for p in prompts(engine, "coder") if "the whole suite fails" in p]) == 1


# ---- without a tester: the coder's tests are read, not run before the code ----


async def test_without_a_tester_an_assertion_free_test_gets_the_coder_one_retry(tmp_path: Path) -> None:
    st, engine = await run(tmp_path, [FEATURE, NO_ASSERT, DONE, RED, DONE])
    assert st.blocked is None and "01" in st.built
    found = kinds(st.path / "events.jsonl", "assertion_free_test")
    assert [(e["attempt"], e["test"]) for e in found] == [(1, "tests/test_calc.py::test_mul")]
    retry = [p for p in prompts(engine, "coder") if "tests/test_calc.py::test_mul" in p]
    assert len(retry) == 1 and "Run run_tests until your tests pass" in retry[0]
    assert built_file(tmp_path, st, "tests/test_calc.py").endswith("assert mul(2, 3) == 6")


async def test_without_a_tester_an_assertion_free_test_twice_stops_the_layer(tmp_path: Path) -> None:
    st, _ = await run(tmp_path, [FEATURE, NO_ASSERT, DONE, DONE])
    assert st.blocked is not None and "its tests need fixing after one retry" in st.blocked
    assert "tests/test_calc.py::test_mul has no assertion" in st.blocked


async def test_a_gate_without_pytest_skips_the_checks_and_still_locks_the_testers_files(
    tmp_path: Path,
) -> None:
    st, _ = await run(tmp_path, [FEATURE, DONE], tester=[VACUOUS, LISTED], test_cmd="true")
    assert st.blocked is None and "01" in st.built
    [skipped] = kinds(st.path / "events.jsonl", "test_checks_skipped")
    assert skipped["layer"] == "01" and "pytest" in skipped["reason"]
    studio_events = tmp_path / "state" / "runs" / st.built["01"]["run_id"] / "events.jsonl"
    assert [e["paths"] for e in kinds(studio_events, "locked")] == [["tests/test_calc.py"]]
