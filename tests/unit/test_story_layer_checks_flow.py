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
from typing import Any

import pytest

from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.engine.types import EngineError
from airlie.story.board import text_card
from airlie.story.state import StoryState
from airlie.teams import native_team
from tests.unit.git_helpers import git
from tests.unit.test_story_pipeline import STORY, TESTER_ROLES, agents_md, fenced, kinds, pipeline
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
FIXED = write("calc.py", CALC + "\n\n# fixed after the gate failed\ndef mul(a, b):\n    return a * b\n")
"""Not FEATURE: a file the size of BROKEN, rewritten in the same second, would be run from BROKEN's .pyc."""
VACUOUS = write(
    "tests/test_calc.py", "from calc import add\n\n\ndef test_add_again():\n    assert add(2, 2) == 4\n"
)
RED = write("tests/test_calc.py", TEST_MUL)
NO_ASSERT = write("tests/test_calc.py", "from calc import mul\n\n\ndef test_mul():\n    mul(2, 3)\n")
LISTED = FakeReply(text="wrote tests/test_calc.py::test_mul: asserts mul(2, 3) == 6")


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
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return repo


async def run(
    tmp_path: Path,
    coder: list[FakeReply],
    tester: list[FakeReply] | None = None,
    test_cmd: str = PYTEST,
    gate: tuple[str, ...] = (),
    base_test: str = "assert add(1, 2) == 3",
) -> tuple[StoryState, FakeEngine]:
    """The one-layer story, with a tester when `tester` is given (its replies), else the coder alone. With
    `gate`, the repository's AGENTS.md gate runs instead of `test_cmd`; `base_test` as in calc_repo."""
    scripts = {"product": [STORY], "architect": [LAYERS], "coder": coder, "reviewer": [APPROVE, APPROVE]}
    if tester is not None:
        scripts["tester"] = tester
    engine = FakeEngine(scripts)
    repo = calc_repo(tmp_path, gate, base_test)
    cmd = None if gate else test_cmd
    if tester is None:
        story = pipeline(tmp_path, repo, engine, test_cmd=cmd)
    else:
        story = pipeline(
            tmp_path, repo, engine, test_cmd=cmd, story={"tester_role": "tester"}, roles=TESTER_ROLES
        )
    st = await story.run(text_card("multiply"))
    return st, engine


def prompts(engine: FakeEngine, role: str) -> list[str]:
    return [r.messages[-1].content for r in engine.requests if r.role == role]


def built_file(tmp_path: Path, st: StoryState, path: str) -> str:
    return git(tmp_path / "repo", "show", f"{st.built['01']['branch']}:{path}")


RUFF = f"{shlex.quote(sys.executable)} -m ruff"
RUFF_GATE = (f"{RUFF} check .", f"{RUFF} format --check .", PYTEST)
UNSORTED = write(
    "tests/test_calc.py",
    "from calc import mul\nimport os\n\n\ndef test_mul():\n    assert mul(2,3)==6 and os.sep\n",
)
TOO_LONG = write(
    "tests/test_calc.py",
    "from calc import mul\n\n\ndef test_mul():\n    # " + "words " * 12 + "end\n    assert mul(2, 3) == 6\n",
)
"""A comment too long for the line length: ruff format does not wrap comments, so only the tester can."""


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


async def test_an_existing_test_already_red_on_the_base_does_not_send_the_tester_back(tmp_path: Path) -> None:
    """AIR-9 layer 02, 2026-10-21: a test red on the base in the confined suite was blamed on the tester's
    tests, and the layer blocked on it. It is the base's: an event names it and the coder starts."""
    st, engine = await run(
        tmp_path, [FEATURE, DONE, DONE, DONE], tester=[RED, LISTED], base_test="assert add(1, 2) == 4"
    )
    events = st.path / "events.jsonl"
    [at_base] = kinds(events, "existing_tests_fail_at_base")
    assert at_base["tests"] == ["tests/test_base.py::test_add"]
    assert kinds(events, "existing_test_fails") == [] and kinds(events, "test_checks_retry") == []
    assert [e["layer"] for e in kinds(events, "test_checks_passed")] == ["01"]
    assert prompts(engine, "coder")  # the coder ran


class DownForTester(FakeEngine):
    """The engine refusing every request of the tester's, as one that is not running does."""

    async def generate(self, req: Any, on_token: Any = None) -> Any:
        if req.role == "tester":
            raise EngineError("ConnectError: All connection attempts failed")
        return await super().generate(req, on_token)


async def test_an_engine_the_tester_cannot_reach_stops_the_layer_as_the_engines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AIR-9 layer 02, 2026-10-21: the engine was down, the tester's run ended on it, and the layer stopped on
    "no new test" after a retry that met the same engine. Now: no check, no retry, a stop that says why."""
    monkeypatch.setattr(native_team, "ENGINE_RETRY_S", 0.0)
    scripts = {"product": [STORY], "architect": [LAYERS], "coder": [FEATURE, DONE], "reviewer": [APPROVE]}
    engine = DownForTester(scripts)
    repo = calc_repo(tmp_path)
    story = pipeline(
        tmp_path, repo, engine, test_cmd=PYTEST, story={"tester_role": "tester"}, roles=TESTER_ROLES
    )
    st = await story.run(text_card("multiply"))
    assert st.blocked is not None and "the tester could not reach the engine" in st.blocked
    assert "ConnectError" in st.blocked and "task up" in st.blocked
    events = st.path / "events.jsonl"
    assert [e["attempt"] for e in kinds(events, "engine_error")] == [1]
    assert kinds(events, "no_new_test") == [] and kinds(events, "test_checks_retry") == []
    assert prompts(engine, "coder") == []  # the coder never started


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
    st, engine = await run(tmp_path, [BROKEN, DONE, FIXED, DONE], tester=[RED, LISTED])
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


# ---- with a tester and ruff in the gate: lint before the tester is done ----


async def test_what_ruff_can_fix_in_the_testers_tests_is_fixed_before_the_coder_starts(
    tmp_path: Path,
) -> None:
    st, engine = await run(tmp_path, [FEATURE, DONE], tester=[UNSORTED, LISTED], gate=RUFF_GATE)
    assert st.blocked is None and "01" in st.built, st.blocked
    [fixed] = kinds(st.path / "events.jsonl", "tester_lint_fixed")
    assert (fixed["attempt"], fixed["files"]) == (1, ["tests/test_calc.py"])
    built = built_file(tmp_path, st, "tests/test_calc.py")
    assert built.startswith("import os\n\nfrom calc import mul\n") and "mul(2, 3) == 6" in built
    assert kinds(st.path / "events.jsonl", "test_checks_retry") == []  # nothing was left to fix by hand


async def test_what_ruff_cannot_fix_goes_back_to_the_tester_with_the_rule(tmp_path: Path) -> None:
    st, engine = await run(tmp_path, [FEATURE, DONE], tester=[TOO_LONG, LISTED, RED, LISTED], gate=RUFF_GATE)
    assert st.blocked is None and "01" in st.built, st.blocked
    [found] = kinds(st.path / "events.jsonl", "lint_fails")
    assert (found["attempt"], found["test"]) == (1, RUFF_GATE[0])
    [retry] = [p for p in prompts(engine, "tester") if "fix these by hand" in p]
    assert "E501" in retry and "tests/test_calc.py" in retry


async def test_a_lint_error_still_there_after_the_retry_stops_the_layer_before_the_coder(
    tmp_path: Path,
) -> None:
    st, engine = await run(
        tmp_path, [FEATURE, DONE], tester=[TOO_LONG, LISTED, TOO_LONG, LISTED], gate=RUFF_GATE
    )
    assert st.blocked is not None and "fails on the tester's files" in st.blocked
    assert prompts(engine, "coder") == []


# ---- a wrong acceptance test goes back to the tester (AIR-9 layer 02, 2026-10-21) ----

WRONG = write(
    "tests/test_calc.py",
    "from calc import mul\n\n\ndef test_mul():\n    [r] = [mul(2, 3), mul(1, 1)]\n    assert r == 6\n",
)
"""Red before the code (mul is not there) and red after it, for a mistake of its own: it unpacks one value
from two, as AIR-9's unpacked one comment from a card with two."""
MENDED = FakeReply(text="test_mul unpacked two values into one: it now asserts mul(2, 3) == 6 directly")
STILL_RIGHT = FakeReply(text="the test is right; the code is wrong")


async def test_a_test_wrong_in_itself_is_mended_by_the_tester_and_the_layer_is_built(tmp_path: Path) -> None:
    st, engine = await run(tmp_path, [FEATURE, DONE], tester=[WRONG, LISTED, RED, MENDED])
    assert st.blocked is None and "01" in st.built, st.blocked
    assert built_file(tmp_path, st, "tests/test_calc.py") == TEST_MUL.rstrip("\n")
    events = st.path / "events.jsonl"
    assert [e["tests"] for e in kinds(events, "locked_tests_fail")] == [["tests/test_calc.py::test_mul"]]
    assert [e["files"] for e in kinds(events, "tests_mended")] == [["tests/test_calc.py"]]
    [asked] = [p for p in prompts(engine, "tester") if "fails only in your tests" in p]
    assert (
        "tests/test_calc.py::test_mul" in asked and DONE.text in asked
    )  # the failure, and the coder's account
    assert [p for p in prompts(engine, "coder") if "the whole suite fails with your change" in p] == []
    # locked again as mended: the gate's restore keeps the mend
    [_, relocked] = kinds(tmp_path / "state" / "runs" / st.built["01"]["run_id"] / "events.jsonl", "locked")
    assert relocked["paths"] == ["tests/test_calc.py"]


async def test_a_mend_that_passes_before_the_code_is_undone_and_the_coder_is_sent_the_gate(
    tmp_path: Path,
) -> None:
    weakened = write("tests/test_calc.py", "def test_mul():\n    assert True\n")
    st, engine = await run(tmp_path, [FEATURE, DONE, DONE], tester=[WRONG, LISTED, weakened, MENDED])
    assert st.blocked is not None and "01" not in st.built
    events = st.path / "events.jsonl"
    [undone] = kinds(events, "tests_mend_undone")
    assert undone["why"] == "tests/test_calc.py::test_mul"
    assert [e["test"] for e in kinds(events, "passes_before_code")] == ["tests/test_calc.py::test_mul"]
    assert kinds(events, "tests_mended") == []
    assert len([p for p in prompts(engine, "coder") if "the whole suite fails with your change" in p]) == 1


async def test_a_mend_that_changes_the_code_is_undone(tmp_path: Path) -> None:
    st, engine = await run(
        tmp_path, [FEATURE, DONE, DONE], tester=[WRONG, LISTED, RED, write("calc.py", CALC), MENDED]
    )
    assert st.blocked is not None and "01" not in st.built
    [undone] = kinds(st.path / "events.jsonl", "tests_mend_undone")
    assert undone["why"] == "it changed calc.py"


async def test_a_tester_that_stands_by_its_tests_changes_nothing_and_the_coder_is_sent_the_gate(
    tmp_path: Path,
) -> None:
    st, engine = await run(tmp_path, [FEATURE, DONE, DONE], tester=[WRONG, LISTED, STILL_RIGHT])
    assert st.blocked is not None
    events = st.path / "events.jsonl"
    assert len(kinds(events, "tests_not_mended")) == 1 and kinds(events, "tests_mended") == []
    assert len([p for p in prompts(engine, "coder") if "the whole suite fails with your change" in p]) == 1


async def test_a_gate_failing_outside_the_testers_files_does_not_ask_the_tester(tmp_path: Path) -> None:
    st, engine = await run(tmp_path, [BROKEN, DONE, FIXED, DONE], tester=[RED, LISTED])
    assert st.blocked is None and "01" in st.built
    assert kinds(st.path / "events.jsonl", "locked_tests_fail") == []
    assert [p for p in prompts(engine, "tester") if "fails only in your tests" in p] == []
