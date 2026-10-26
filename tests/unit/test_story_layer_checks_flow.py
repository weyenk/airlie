"""A layer's package through the pipeline (P1, 2026-10-24): the architect's tests must fail before any code is
written (the readiness check, one retry for the architect), are then read-only to the coder, and the coder is
done when they pass; the whole suite is the final gate, with one follow-up for the coder when it fails and a
re-plan by the architect when it still fails. On FakeEngine, a NullBoard and a recorded gh, with pytest as
the gate."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.prompts import text as hub_text
from airlie.story.amigos import Agreement, Objection, ProofTest, Turn, Verdict
from airlie.story.board import text_card
from airlie.story.model import Layer, parse_story
from airlie.story.state import StoryState
from airlie.topology.studio_plan import extract_json_block
from tests.unit.git_helpers import git
from tests.unit.test_story_pipeline import (
    MEETING,
    MEETING_ROLES,
    STORY,
    FakeMeeting,
    agents_md,
    fenced,
    kinds,
    package_reply,
    pipeline,
)
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
"""Not FEATURE: a file the size of BROKEN, rewritten in the same second, would be run from BROKEN's .pyc."""
STILL_BROKEN = write("notes.txt", "tried\n")
"""A follow-up that acts without fixing anything (a reply with no tool call is nudged to act, and would take
the next scripted reply)."""
RED = package()
"""Red before the code (mul is not there)."""
VACUOUS = package(TEST_ADD_AGAIN, "test_add_again")
"""Green before the code: add is already there."""
OTHER_RED = package(
    "from calc import mul\n\n\ndef test_mul_zero():\n    assert mul(0, 3) == 0\n", "test_mul_zero"
)
"""A second, different red package, for the architect's second try."""


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
    packages: list[FakeReply] | None = None,
    test_cmd: str = PYTEST,
    gate: tuple[str, ...] = (),
    base_test: str = "assert add(1, 2) == 3",
    story: dict[str, object] | None = None,
) -> tuple[StoryState, FakeEngine]:
    """The one-layer story: the architect's `packages` (default: one red one) after its layer plan, then the
    coder's replies. With `gate`, the repository's AGENTS.md gate runs instead of `test_cmd`; `base_test` as
    in calc_repo; `story` is more story settings."""
    scripts = {
        "product": [STORY],
        "architect": [LAYERS, *(packages if packages is not None else [RED])],
        "coder": coder,
        "reviewer": [APPROVE, APPROVE],
    }
    engine = FakeEngine(scripts)
    repo = calc_repo(tmp_path, gate, base_test)
    cmd = None if gate else test_cmd
    st = await pipeline(tmp_path, repo, engine, test_cmd=cmd, story=story).run(text_card("multiply"))
    return st, engine


def prompts(engine: FakeEngine, role: str) -> list[str]:
    return [r.messages[-1].content for r in engine.requests if r.role == role]


def steers(st: StoryState) -> list[tuple[str, str, str]]:
    """The story's `steer` events: the hub texts the pipeline sent a layer's roles (id, role, layer)."""
    return [(e["id"], e["role"], e["layer"]) for e in kinds(st.path / "events.jsonl", "steer")]


def built_file(tmp_path: Path, st: StoryState, path: str) -> str:
    return git(tmp_path / "repo", "show", f"{st.built['01']['branch']}:{path}")


RUFF = f"{shlex.quote(sys.executable)} -m ruff"
RUFF_GATE = (f"{RUFF} check .", f"{RUFF} format --check .", PYTEST)
UNSORTED = package(
    "from calc import mul\nimport os\n\n\ndef test_mul():\n    assert mul(2,3)==6 and os.sep\n"
)
TOO_LONG = package(
    "from calc import mul\n\n\ndef test_mul():\n    # " + "words " * 12 + "end\n    assert mul(2, 3) == 6\n"
)
"""A comment too long for the line length: ruff format does not wrap comments, so only the architect can."""


# ---- red before the code ----


async def test_a_red_package_is_placed_locked_and_the_coder_builds_against_it(tmp_path: Path) -> None:
    st, engine = await run(tmp_path, [FEATURE, DONE])
    assert st.blocked is None and st.stage == "review", st.blocked
    events = st.path / "events.jsonl"
    [ready] = kinds(events, "package_ready")
    assert (ready["layer"], ready["attempt"], ready["tests"]) == ("01", 1, ["tests/test_calc.py::test_mul"])
    studio_run = st.built["01"]["run_id"]
    studio_events = tmp_path / "state" / "runs" / studio_run / "events.jsonl"
    [locked] = kinds(studio_events, "locked")
    assert locked["owner"] == "architect" and locked["paths"] == ["tests/test_calc.py"]
    assert built_file(tmp_path, st, "tests/test_calc.py") == TEST_MUL.rstrip("\n")
    # the coder ran after the check, against the architect's tests and steps
    goal = prompts(engine, "coder")[0]
    assert "you cannot change them" in goal and "tests/test_calc.py::test_mul" in goal
    assert "1. add mul(a, b) to calc.py returning a * b" in goal and "- calc.py: def mul(a, b) -> int" in goal


async def test_a_test_that_passes_before_the_code_sends_the_package_back_to_the_architect_once(
    tmp_path: Path,
) -> None:
    st, engine = await run(tmp_path, [FEATURE, DONE], [VACUOUS, RED])
    assert st.blocked is None and "01" in st.built
    events = st.path / "events.jsonl"
    [found] = kinds(events, "passes_before_code")
    assert (found["attempt"], found["test"]) == (1, "tests/test_calc.py::test_add_again")
    retry = [p for p in prompts(engine, "architect") if "tests/test_calc.py::test_add_again" in p]
    assert len(retry) == 1 and "passes before the layer's code is written" in retry[0]
    [returned] = kinds(events, "package_returned")
    assert (returned["attempt"], returned["kinds"]) == (1, ["passes_before_code"])
    assert [e["attempt"] for e in kinds(events, "package_start")] == [1, 2]
    assert [e["attempt"] for e in kinds(events, "package_ready")] == [2]


async def test_the_architects_retry_is_a_steer_event_per_hub_text_it_holds(tmp_path: Path) -> None:
    """Spec 2026-10-22-prompt-hub, "Steering events": what the pipeline sends a role is counted by hub id."""
    st, engine = await run(tmp_path, [FEATURE, DONE], [VACUOUS, RED])
    assert st.blocked is None and "01" in st.built
    assert steers(st) == [
        ("checks-found", "architect", "01"),
        ("passes-before-code-choice", "architect", "01"),
        ("package-fix", "architect", "01"),
    ]
    [retry] = [p for p in prompts(engine, "architect") if "tests/test_calc.py::test_add_again" in p]
    assert hub_text("checks-found") in retry and hub_text("package-fix") in retry
    assert hub_text("passes-before-code-choice") in retry


async def test_an_existing_test_already_red_on_the_base_does_not_send_the_package_back(
    tmp_path: Path,
) -> None:
    """AIR-9 layer 02, 2026-10-21: a test red on the base in the confined suite was blamed on the new tests,
    and the layer blocked on it. It is the base's: an event names it and the coder starts."""
    st, engine = await run(tmp_path, [FEATURE, DONE, DONE, DONE], base_test="assert add(1, 2) == 4")
    events = st.path / "events.jsonl"
    [at_base] = kinds(events, "existing_tests_fail_at_base")
    assert at_base["tests"] == ["tests/test_base.py::test_add"]
    assert kinds(events, "existing_test_fails") == [] and kinds(events, "package_returned") == []
    assert [e["layer"] for e in kinds(events, "package_ready")] == ["01"]
    assert prompts(engine, "coder")  # the coder ran


async def test_a_package_still_green_before_the_code_after_the_retry_stops_the_layer_before_the_coder(
    tmp_path: Path,
) -> None:
    st, engine = await run(tmp_path, [FEATURE, DONE], [VACUOUS, VACUOUS])
    assert st.blocked is not None and "01" not in st.built
    assert "layer 01 (Multiply): its package still fails the readiness check after 2 tries" in st.blocked
    assert "(passes_before_code)" in st.blocked
    assert prompts(engine, "coder") == []  # the coder never started
    assert kinds(st.path / "events.jsonl", "layer_built") == []
    assert git(tmp_path / "repo", "worktree", "list").count("\n") == 0  # the scratch checks left nothing
    assert git(tmp_path / "repo", "branch", "--list", "add-*") == ""  # and no branch


async def test_the_coder_cannot_change_the_packages_tests_with_the_file_tools(tmp_path: Path) -> None:
    weaker = write("tests/test_calc.py", "def test_mul():\n    assert True\n")
    st, engine = await run(tmp_path, [weaker, FEATURE, DONE])
    assert st.blocked is None and "01" in st.built
    refused = [
        m.content for r in engine.requests if r.role == "coder" for m in r.messages if m.role == "tool"
    ]
    assert any("you cannot change them" in m for m in refused)
    assert built_file(tmp_path, st, "tests/test_calc.py") == TEST_MUL.rstrip("\n")


TEST_BASE_AND_MUL = (
    "from calc import add, mul\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
    "def test_mul():\n    assert mul(2, 3) == 6\n"
)
"""tests/test_base.py as it was (test_add, verbatim), with test_mul added."""


async def test_a_package_that_adds_to_an_existing_test_module_locks_only_its_own_definitions(
    tmp_path: Path,
) -> None:
    """AIR-9 layer 05, 2026-10-22: a whole-file lock kept the coder from fixing a fake in a file whose tests
    it had not written. The package reproduces test_add verbatim and adds test_mul: only test_mul is locked,
    so the coder can change test_add (a fake of its file) and cannot change test_mul."""
    pkg = package(TEST_BASE_AND_MUL, path="tests/test_base.py")
    edit_old = write(
        "tests/test_base.py",
        TEST_BASE_AND_MUL.replace("assert add(1, 2) == 3", "assert add(1, 2) == 3 and 1"),
    )
    st, _ = await run(tmp_path, [edit_old, FEATURE, DONE], [pkg])
    assert st.blocked is None and "01" in st.built, st.blocked
    studio_events = tmp_path / "state" / "runs" / st.built["01"]["run_id"] / "events.jsonl"
    [locked] = kinds(studio_events, "locked")
    assert locked["tests"] == {"tests/test_base.py": ["test_mul"]}
    built = built_file(tmp_path, st, "tests/test_base.py")
    assert "assert add(1, 2) == 3 and 1" in built  # the coder changed the old test
    assert "def test_mul():\n    assert mul(2, 3) == 6" in built  # and the architect's stayed


# ---- the final gate: the whole suite, with one follow-up ----


async def test_a_failing_final_gate_sends_the_coder_its_output_once(tmp_path: Path) -> None:
    st, engine = await run(tmp_path, [BROKEN, DONE, FIXED, DONE])
    assert st.blocked is None and "01" in st.built
    follow_up = [p for p in prompts(engine, "coder") if "the whole suite fails with your change" in p]
    assert len(follow_up) == 1 and "test_add" in follow_up[0]
    studio_events = tmp_path / "state" / "runs" / st.built["01"]["run_id"] / "events.jsonl"
    assert [e["follow_up"] for e in kinds(studio_events, "gate_failed")] == ["build"]
    assert "return a + b" in built_file(tmp_path, st, "calc.py")


async def test_a_final_gate_still_failing_after_the_follow_up_stops_the_layer_with_one_build_allowed(
    tmp_path: Path,
) -> None:
    st, engine = await run(tmp_path, [BROKEN, DONE, DONE])
    assert st.blocked is not None and "01" not in st.built
    assert "fails on" in st.blocked and "after 1 build" not in st.blocked
    assert len([p for p in prompts(engine, "coder") if "the whole suite fails" in p]) == 1


# ---- a layer still red after the coder's follow-up goes back to the architect (D2a) ----


async def test_a_gate_still_red_after_the_coders_follow_up_replans_the_layer_once_on_the_same_branch(
    tmp_path: Path,
) -> None:
    """The first build breaks add and the follow-up does not fix it: the architect is asked for the package
    again with the coder's account and the gate's tail, and the layer builds on the second try. The failed
    build's worktree and branch are removed first, or `git worktree add -b` refuses the same name."""
    st, engine = await run(
        tmp_path,
        [BROKEN, DONE, STILL_BROKEN, DONE, FEATURE, DONE],
        [RED, OTHER_RED],
        story={"layer_attempts": 2},
    )
    assert st.blocked is None and "01" in st.built, st.blocked
    events = st.path / "events.jsonl"
    [again] = [p for p in prompts(engine, "architect") if "the whole gate is still red" in p]
    assert DONE.text in again and "test_add" in again  # the coder's account and the gate's tail
    assert hub_text("package-replan") in again and "could not be used" in again
    assert "tests/test_calc.py::test_mul_zero" not in again  # the old package is not repeated
    [replanned] = kinds(events, "layer_replanned")
    assert (replanned["layer"], replanned["attempt"], replanned["why"]) == ("01", 1, "gate_red")
    assert [e["attempt"] for e in kinds(events, "layer_start")] == [1, 2]
    branches = {e["branch"] for e in kinds(events, "layer_start")}
    assert len(branches) == 1 and st.built["01"]["branch"] in branches  # the same name, built twice
    assert built_file(tmp_path, st, "tests/test_calc.py") == OTHER_RED_TEXT.rstrip("\n")
    assert ("package-replan", "architect", "01") in steers(st)
    [lesson] = [e for e in kinds(events, "lesson_recorded")]
    assert (lesson["layer"], lesson["ending"]) == ("01", "gate_red")
    assert git(tmp_path / "repo", "worktree", "list").count("\n") == 0


OTHER_RED_TEXT = "from calc import mul\n\n\ndef test_mul_zero():\n    assert mul(0, 3) == 0\n"


async def test_a_layer_red_after_every_build_stops_the_story_naming_the_builds(tmp_path: Path) -> None:
    st, engine = await run(
        tmp_path,
        [BROKEN, DONE, STILL_BROKEN, DONE, BROKEN, DONE, STILL_BROKEN, DONE],
        [RED, OTHER_RED],
        story={"layer_attempts": 2},
    )
    assert st.blocked is not None and "01" not in st.built
    assert "fails on" in st.blocked and "after 2 builds" in st.blocked
    events = st.path / "events.jsonl"
    assert [e["ending"] for e in kinds(events, "lesson_recorded")] == ["gate_red", "gate_red"]
    assert len(kinds(events, "layer_replanned")) == 1  # the last build is not re-planned, it stops
    assert "its worktree is kept at" in st.blocked  # the last build's is left for a person to look at


# ---- a gate without pytest ----


async def test_a_gate_without_pytest_skips_the_red_check_and_still_locks_the_packages_files(
    tmp_path: Path,
) -> None:
    st, _ = await run(tmp_path, [FEATURE, DONE], [VACUOUS], test_cmd="true")
    assert st.blocked is None and "01" in st.built
    [skipped] = kinds(st.path / "events.jsonl", "test_checks_skipped")
    assert skipped["layer"] == "01" and "pytest" in skipped["reason"]
    studio_events = tmp_path / "state" / "runs" / st.built["01"]["run_id"] / "events.jsonl"
    assert [e["paths"] for e in kinds(studio_events, "locked")] == [["tests/test_calc.py"]]


# ---- ruff in the gate: lint before the package is placed ----


async def test_what_ruff_can_fix_in_the_package_is_fixed_before_it_is_placed(tmp_path: Path) -> None:
    st, engine = await run(tmp_path, [FEATURE, DONE], [UNSORTED], gate=RUFF_GATE)
    assert st.blocked is None and "01" in st.built, st.blocked
    built = built_file(tmp_path, st, "tests/test_calc.py")
    assert built.startswith("import os\n\nfrom calc import mul\n") and "mul(2, 3) == 6" in built
    assert kinds(st.path / "events.jsonl", "package_returned") == []  # nothing was left to fix by hand
    assert st.built["01"]["package"]["tests"]["tests/test_calc.py"] == built + "\n"  # the stored text too


async def test_what_ruff_cannot_fix_goes_back_to_the_architect_with_the_rule(tmp_path: Path) -> None:
    st, engine = await run(tmp_path, [FEATURE, DONE], [TOO_LONG, RED], gate=RUFF_GATE)
    assert st.blocked is None and "01" in st.built, st.blocked
    [found] = kinds(st.path / "events.jsonl", "lint_fails")
    assert (found["attempt"], found["test"]) == (1, RUFF_GATE[0])
    [retry] = [p for p in prompts(engine, "architect") if "fix these by hand" in p]
    assert "E501" in retry and "tests/test_calc.py" in retry


# ---- the agreed proof (P2, 2026-10-24) ----

TWO_TESTS = (
    "from calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n\n\n"
    "def test_mul_zero():\n    assert mul(0, 3) == 0\n"
)
TWO_REVISED = TWO_TESTS.replace("mul(0, 3) == 0", "mul(0, 3) == 0 and mul(3, 0) == 0")
NO_ASSERT = TWO_TESTS.replace("    assert mul(0, 3) == 0\n", "    mul(0, 3)\n")
ID_MUL, ID_ZERO = "tests/test_calc.py::test_mul", "tests/test_calc.py::test_mul_zero"
ID_ONE = "tests/test_calc.py::test_mul_by_one"


def two(text: str = TWO_TESTS, second: str = "test_mul_zero") -> FakeReply:
    """A package covering AC1 with test_mul and AC2 with `second`, as the agreed proof says."""
    return package_reply(
        {"tests/test_calc.py": text},
        {"AC1": [ID_MUL], "AC2": [f"tests/test_calc.py::{second}"]},
        files=[{"path": "calc.py", "action": "modify"}],
        interfaces=[MUL_INTERFACE],
        steps=STEPS,
    )


def agreement() -> Agreement:
    """The calc story's agreement: one layer, two criteria, a test each."""
    story = parse_story(extract_json_block(STORY.text))
    layer = Layer("mul", "Multiply", "calc.py", "Add mul.", ("AC1", "AC2"))
    proof = (
        ProofTest("AC1", "mul", "tests/test_calc.py", "test_mul", "tests", None, "mul(2, 3) is 6", ""),
        ProofTest("AC2", "mul", "tests/test_calc.py", "test_mul_zero", "tests", None, "mul(0, 3) is 0", ""),
    )
    return Agreement(story, (layer,), proof, 6)


AGREE_ALL = FakeReply(text='Fine.\n```json\n{"type": "verdict", "proof": "agree", "objections": []}\n```')
OBJECT = FakeReply(
    text="Thin.\n```json\n"
    + json.dumps(
        {
            "type": "verdict",
            "proof": "object",
            "objections": [
                {"about": ID_ZERO, "claim": "it checks only one operand order", "suggest": "swap them too"}
            ],
        }
    )
    + "\n```"
)
MEETING_STORY = {**MEETING, "package_attempts": 2}


async def agreed_run(
    tmp_path: Path,
    packages: list[FakeReply],
    tester: list[FakeReply],
    coder: list[FakeReply],
    *,
    meeting: object | None = None,
    story: dict[str, object] | None = None,
) -> tuple[StoryState, FakeEngine]:
    """The one-layer calc story with an agreement in hand (the meeting is a double): the architect's
    `packages` after its layer plan, the tester critic's replies, then the coder's."""
    engine = FakeEngine(
        {
            "product": [STORY],
            "architect": [LAYERS, *packages],
            "tester": tester,
            "coder": coder,
            "reviewer": [APPROVE, APPROVE],
        }
    )
    repo = calc_repo(tmp_path)
    pipe = pipeline(
        tmp_path,
        repo,
        engine,
        test_cmd=PYTEST,
        story={**MEETING_STORY, **(story or {})},
        roles=MEETING_ROLES,
        meeting=meeting or (lambda cfg, tools, on_turn: FakeMeeting(agreement())),
    )
    return await pipe.run(text_card("multiply")), engine


async def test_the_critics_objection_gives_the_architect_one_revision_before_the_checks(
    tmp_path: Path,
) -> None:
    st, engine = await agreed_run(tmp_path, [two(), two(TWO_REVISED)], [OBJECT], [FEATURE, DONE])
    assert st.blocked is None and st.stage == "review", st.blocked
    asked = prompts(engine, "architect")
    assert len(asked) == 3  # the layers, the package, its one revision
    assert "The tester read your tests against the agreed proof and objects" in asked[2]
    assert f"- {ID_ZERO}: it checks only one operand order" in asked[2] and "swap them too" in asked[2]
    assert (
        asked[2].rstrip().endswith(hub_text("package-fix").splitlines()[-1])
        or hub_text("package-fix") in asked[2]
    )
    critic = prompts(engine, "tester")[0]
    assert "Read these tests against the agreed proof" in critic and "def test_mul_zero" in critic
    assert f"{ID_ZERO} (proves AC2" in critic
    events = st.path / "events.jsonl"
    [objection] = kinds(events, "amigos_objection")
    assert (objection["role"], objection["part"], objection["about"], objection["layer"]) == (
        "tester",
        "proof",
        ID_ZERO,
        "01",
    )
    assert [e["verdict"] for e in kinds(events, "amigos_critic")] == ["object"]
    assert ("package-fix", "architect", "01") in steers(st)
    assert (
        built_file(tmp_path, st, "tests/test_calc.py").strip() == TWO_REVISED.strip()
    )  # the revision stands


async def test_a_critic_that_agrees_or_cannot_be_read_costs_the_architect_no_revision(tmp_path: Path) -> None:
    st, engine = await agreed_run(tmp_path, [two()], [AGREE_ALL], [FEATURE, DONE])
    assert st.blocked is None and len(prompts(engine, "architect")) == 2
    assert [e["verdict"] for e in kinds(st.path / "events.jsonl", "amigos_critic")] == ["agree"]
    unreadable = FakeReply(text="I think it is fine.")
    (tmp_path / "b").mkdir()
    st, engine = await agreed_run(tmp_path / "b", [two()], [unreadable], [FEATURE, DONE])
    assert st.blocked is None and st.stage == "review", st.blocked  # a critic is advisory: the checks follow
    assert len(prompts(engine, "architect")) == 2
    [skipped] = kinds(st.path / "events.jsonl", "amigos_critic_unreadable")
    assert skipped["layer"] == "01" and "verdict" in skipped["reason"]


async def test_the_critic_reads_the_packages_test_files_on_disk_as_in_its_brief(tmp_path: Path) -> None:
    """On the first full real run (airlie-sandbox, 2026-10-26) Muse read the test file from the base, where it
    was absent or an earlier layer's, and objected that every agreed test was missing; the critic's tree is
    the base with the package's test files placed, so its tools see what its brief shows."""
    look = FakeReply(tool_calls=[ToolCall("r", "read_file", json.dumps({"path": "tests/test_calc.py"}))])
    st, engine = await agreed_run(tmp_path, [two()], [look, AGREE_ALL], [FEATURE, DONE])
    assert st.blocked is None and st.stage == "review", st.blocked
    second = engine.requests[[r.role for r in engine.requests].index("tester") + 1]
    [result] = [m.content for m in second.messages if m.role == "tool"]
    assert "def test_mul_zero" in str(result), result


async def test_a_package_covering_a_test_the_proof_lacks_is_a_proof_mismatch(tmp_path: Path) -> None:
    extra = TWO_TESTS + "\n\ndef test_extra():\n    assert mul(1, 1) == 1\n"
    wide = package_reply(
        {"tests/test_calc.py": extra},
        {"AC1": [ID_MUL], "AC2": [ID_ZERO, "tests/test_calc.py::test_extra"]},
        files=[{"path": "calc.py", "action": "modify"}],
        interfaces=[MUL_INTERFACE],
        steps=STEPS,
    )
    st, engine = await agreed_run(tmp_path, [wide, two()], [AGREE_ALL, AGREE_ALL], [FEATURE, DONE])
    assert st.blocked is None and st.stage == "review", st.blocked
    [returned] = kinds(st.path / "events.jsonl", "package_returned")
    assert returned["kinds"] == ["proof_mismatch"]
    retry = prompts(engine, "architect")[2]
    assert (
        "tests/test_calc.py::test_extra: your package covers it, but the agreed proof does not name it"
        in retry
    )


PROOF_CHANGE = FakeReply(
    text="mul(0, 3) is not a case this layer can fail.\n```json\n"
    + json.dumps(
        {
            "type": "proof_change",
            "changes": [{"test": ID_ZERO, "why": "zero is covered by test_mul's operands"}],
        }
    )
    + "\n```"
)
NEW_PROOF = FakeReply(
    text="Revised for the layer.\n```json\n"
    + json.dumps(
        {
            "type": "proof",
            "tests": [
                {
                    "criterion": "AC2",
                    "layer": "mul",
                    "file": "tests/test_calc.py",
                    "name": "test_mul_by_one",
                    "level": "tests",
                    "asserts": "mul(7, 1) is 7",
                    "needs": "",
                },
                {
                    "criterion": "AC1",
                    "layer": "mul",
                    "file": "tests/test_calc.py",
                    "name": "test_mul",
                    "level": "tests",
                    "asserts": "mul(2, 3) is 6",
                    "needs": "",
                },
            ],
        }
    )
    + "\n```"
)
BY_ONE = TWO_TESTS.replace("test_mul_zero", "test_mul_by_one").replace("mul(0, 3) == 0", "mul(7, 1) == 7")


async def test_a_proof_change_reply_revises_the_layers_proof_through_the_tester_and_asks_the_architect_again(
    tmp_path: Path,
) -> None:
    """The architect found the proof wrong: the tester revises that layer's tests in one fresh ask, the
    agreement in the state changes, the architect is asked again, and the attempt it used is not charged
    (one attempt is all this story allows)."""
    st, engine = await agreed_run(
        tmp_path,
        [PROOF_CHANGE, two(BY_ONE, "test_mul_by_one")],
        [NEW_PROOF, AGREE_ALL],
        [FEATURE, DONE],
        story={"package_attempts": 1},
    )
    assert st.blocked is None and st.stage == "review", st.blocked
    asked = prompts(engine, "architect")
    assert len(asked) == 3 and ID_ONE in asked[2] and "asserts: mul(7, 1) is 7" in asked[2]
    revision = prompts(engine, "tester")[0]
    assert "> " + ID_ZERO + ": zero is covered by test_mul's operands" in revision
    assert st.agreement is not None
    assert [t["name"] for t in st.agreement["proof"]] == ["test_mul_by_one", "test_mul"]
    events = st.path / "events.jsonl"
    [revised] = kinds(events, "amigos_proof_revised")
    assert revised["layer"] == "01" and revised["tests"] == [ID_ONE, ID_MUL]
    [ready] = kinds(events, "package_ready")
    assert ready["tests"] == [ID_MUL, ID_ONE]
    assert not kinds(events, "package_returned")


async def test_a_proof_revision_that_names_another_layer_or_fails_stops_the_story(tmp_path: Path) -> None:
    wrong = FakeReply(text=NEW_PROOF.text.replace('"layer": "mul"', '"layer": "elsewhere"'))
    st, _ = await agreed_run(
        tmp_path, [PROOF_CHANGE], [wrong, wrong], [], story={"package_attempts": 1, "stage_attempts": 2}
    )
    assert st.blocked is not None and "tester did not produce a usable proof revision" in st.blocked
    assert not kinds(st.path / "events.jsonl", "amigos_proof_revised")


class Objecting(FakeMeeting):
    """A meeting double that, before it ends, lets product object to a test of the proof."""

    def __init__(self, held: Agreement, on_turn: object) -> None:
        super().__init__(held)
        self.on_turn = on_turn

    async def run(self, *args: object, **kwargs: object) -> Agreement:
        verdict = Verdict(
            "product",
            "agree",
            "agree",
            "object",
            (Objection("proof", ID_ZERO, "it asserts nothing", "assert", "product"),),
        )
        self.on_turn(Turn("product", "The zero test is empty.", verdict), {})  # type: ignore[operator]
        return await super().run(*args, **kwargs)  # type: ignore[arg-type]


async def test_a_check_finding_on_an_objected_test_is_borne_out(tmp_path: Path) -> None:
    """D10: product objected to the zero test in the meeting; the package's first try has no assertion in it,
    and the finding is recorded against the objection."""
    st, _ = await agreed_run(
        tmp_path,
        [two(NO_ASSERT), two()],
        [AGREE_ALL, AGREE_ALL],
        [FEATURE, DONE],
        meeting=lambda cfg, tools, on_turn: Objecting(agreement(), on_turn),
    )
    assert st.blocked is None and st.stage == "review", st.blocked
    events = st.path / "events.jsonl"
    [borne] = kinds(events, "amigos_borne_out")
    assert (borne["layer"], borne["test"], borne["finding"], borne["part"], borne["role"]) == (
        "01",
        ID_ZERO,
        "assertion_free_test",
        "proof",
        "product",
    )
