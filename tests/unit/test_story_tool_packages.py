"""A layer's package written with the file tools (the tool-written packages design, 2026-11-06, build step 3):
with story.package_tools on, a package ask may write test files only, its reply is the package JSON, a try
that fails keeps its files for the next, and a turn that wrote its tests but gave no JSON gets one short turn
for it. The drafting conversation writes with the tools too (build step 4), so a retry is a turn of it. On
FakeEngine, the calc layer of test_story_layer_checks_flow."""

from __future__ import annotations

from pathlib import Path

from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.prompts import text as hub_text
from airlie.story.package import PACKAGE_CONTRACT, PACKAGE_TOOLS_CONTRACT
from airlie.story.state import StoryState
from tests.unit.test_story_layer_checks_flow import (
    DONE,
    FEATURE,
    MUL_INTERFACE,
    RUFF_GATE,
    STEPS,
    TEST_MUL,
    VACUOUS,
    built_file,
    run,
    steers,
    write,
)
from tests.unit.test_story_pipeline import kinds, package_reply

TOOLS = {"package_tools": True}
PATH = "tests/test_calc.py"
JSON_ONLY = package_reply(
    {},
    {"AC1": [f"{PATH}::test_mul"], "AC2": [f"{PATH}::test_mul"]},
    files=[{"path": "calc.py", "action": "modify"}],
    interfaces=[MUL_INTERFACE],
    steps=STEPS,
    prose="The tests are written.\n",
)
"""A package reply with no test block: its test is in the checkout."""
NO_JSON = FakeReply(text="I read the code; the layer adds mul.")
"""A draft that cannot be used (no JSON block): the layer's first try, so its retry is a package ask."""
WRITE_MUL = write(PATH, TEST_MUL)


def events(st: StoryState, kind: str) -> list[dict[str, object]]:
    return kinds(st.path / "events.jsonl", kind)


def architect_asks(engine: FakeEngine) -> list[list[str]]:
    """Each architect request's messages, as text."""
    return [[m.content for m in r.messages] for r in engine.requests if r.role == "architect"]


def tool_names(engine: FakeEngine) -> list[list[str]]:
    return [[t.name for t in r.tools or []] for r in engine.requests if r.role == "architect"]


async def test_a_retry_writes_its_tests_with_the_file_tools_and_replies_with_the_json_alone(
    tmp_path: Path,
) -> None:
    """The drafted package, a reply in the old form (its blocks are written into the drafting checkout),
    passes before the code; its retry is a turn of the drafting conversation, which writes with write_file
    and edit_file, starts on the draft's test file as checked and is told so, not shown it."""
    st, engine = await run(tmp_path, [FEATURE, DONE], [VACUOUS, WRITE_MUL, JSON_ONLY], story=TOOLS)
    assert st.blocked is None and "01" in st.built, st.blocked
    [ready] = events(st, "package_ready")
    assert (ready["attempt"], ready["tests"]) == (2, [f"{PATH}::test_mul"])
    assert built_file(tmp_path, st, PATH) == TEST_MUL.rstrip("\n")
    [placed] = events(st, "package_files_placed")
    assert placed["paths"] == [PATH]
    asks = architect_asks(engine)
    drafted, retry = asks[1][-1], asks[2][-1]  # after the layer plan: the drafting turn, then the retry
    assert PACKAGE_TOOLS_CONTRACT in drafted and PACKAGE_CONTRACT not in drafted
    assert hub_text("brief-package-next-tools") in retry and hub_text("package-files-placed") in retry
    assert [e["layer"] for e in events(st, "package_resumed")] == ["01"]
    assert f"- {PATH}" in retry and "def test_add_again" not in retry  # named, not pasted
    assert hub_text("package-fix-tools") in retry and hub_text("package-as-checked") not in retry
    tools = tool_names(engine)
    assert {"write_file", "edit_file"} <= set(tools[1]) and {"write_file", "edit_file"} <= set(tools[2])
    assert [s[0] for s in steers(st)] == [
        "checks-found",
        "passes-before-code-choice",
        "package-files-placed",
        "package-fix-tools",
    ]
    written = events(st, "package_written")
    assert [w["paths"] for w in written] == [[PATH], [PATH]]  # the draft's blocks, then the retry's write
    assert events(st, "package_answer_turn") == []


async def test_a_try_cut_off_keeps_its_files_and_the_next_try_starts_on_them(tmp_path: Path) -> None:
    """A try that fails loses nothing it wrote: the next one finds the file placed and committed as the
    previous try, and its harvest, since the layer's base, holds it though this try wrote nothing."""
    cut = FakeReply(text="### test file: tests/test_calc.py\n```python\nfrom calc", finish_reason="length")
    st, engine = await run(
        tmp_path,
        [FEATURE, DONE],
        [NO_JSON, WRITE_MUL, cut, JSON_ONLY],
        story={**TOOLS, "package_attempts": 3},
    )
    assert st.blocked is None and "01" in st.built, st.blocked
    assert [e["attempt"] for e in events(st, "package_ready")] == [3]
    assert [e["paths"] for e in events(st, "package_files_placed")] == [[PATH]]
    third = architect_asks(engine)[-1][-1]
    assert "ran out of room" in third and hub_text("package-files-placed") in third and f"- {PATH}" in third
    assert built_file(tmp_path, st, PATH) == TEST_MUL.rstrip("\n")


async def test_a_retrys_checkout_holds_the_file_as_ruff_fixed_it(tmp_path: Path) -> None:
    vacuous_unsorted = (
        "from calc import add\nimport os\n\n\ndef test_add_again():\n    assert add(2,2)==4 and os.sep\n"
    )
    read = FakeReply(tool_calls=[ToolCall("r", "read_file", '{"path": "tests/test_calc.py"}')])
    vacuous_json = package_reply(
        {},
        {"AC1": [f"{PATH}::test_add_again"], "AC2": [f"{PATH}::test_add_again"]},
        files=[{"path": "calc.py", "action": "modify"}],
        interfaces=[MUL_INTERFACE],
        steps=STEPS,
    )
    st, engine = await run(
        tmp_path,
        [FEATURE, DONE],
        [NO_JSON, write(PATH, vacuous_unsorted), vacuous_json, read, WRITE_MUL, JSON_ONLY],
        gate=RUFF_GATE,
        story={**TOOLS, "package_attempts": 3},
    )
    assert st.blocked is None and "01" in st.built, st.blocked
    seen = [r.messages[-1].content for r in engine.requests if r.role == "architect"]
    shown = next(s for s in seen if s.startswith("1: "))  # the read_file result in the third try
    assert shown.startswith("1: import os\n2: \n3: from calc import add") and "add(2, 2) == 4" in shown


async def test_a_turn_that_wrote_its_tests_and_gave_no_json_gets_one_turn_for_the_json(
    tmp_path: Path,
) -> None:
    prose = FakeReply(text="The tests are written; mul is next.")
    st, engine = await run(tmp_path, [FEATURE, DONE], [NO_JSON, WRITE_MUL, prose, JSON_ONLY], story=TOOLS)
    assert st.blocked is None and "01" in st.built, st.blocked
    [turn] = events(st, "package_answer_turn")
    assert turn["paths"] == [PATH] and turn["stop_reason"] == "done"
    assert architect_asks(engine)[-1][-1] == hub_text("package-json-only")
    assert ("package-json-only", "architect", "01") in steers(st)
    assert [e["attempt"] for e in events(st, "package_ready")] == [2]  # the same try


async def test_a_try_that_deletes_a_test_on_the_base_is_refused_by_the_tests_id(tmp_path: Path) -> None:
    gone = write("tests/test_base.py", "from calc import add\n")
    st, _ = await run(tmp_path, [FEATURE, DONE], [NO_JSON, gone, WRITE_MUL, JSON_ONLY], story=TOOLS)
    assert st.blocked is not None and "after 2 tries" in st.blocked
    [_, retry] = events(st, "retry")
    assert "deleted tests/test_base.py::test_add" in str(retry["error"])


def test_a_retrys_feedback_names_the_placed_files_and_carries_the_json_not_the_tests() -> None:
    from airlie.story.layer_checks import Finding
    from airlie.story.package import Package
    from airlie.story.package_checks import package_feedback_said, package_json

    package = Package("mul", {PATH: TEST_MUL}, (), (), ("add mul",), {"AC1": (f"{PATH}::test_mul",)}, "done")
    found = [Finding("passes_before_code", f"{PATH}::test_mul", "it passes", "")]
    told, ids = package_feedback_said(found, placed=package)
    assert ids == ("checks-found", "passes-before-code-choice", "package-files-placed", "package-fix-tools")
    assert f"{hub_text('package-files-placed')}\n- {PATH}\n{package_json(package)}\n" in told
    assert "def test_mul" not in told and told.endswith(hub_text("package-fix-tools"))


def test_a_satisfied_reply_that_does_not_hold_asks_for_files_with_the_tools() -> None:
    from airlie.story.layer_checks import Finding
    from airlie.story.satisfied_check import satisfied_feedback

    found = [Finding("satisfied_unproven", "01", "layer 01 is not built", "")]
    assert satisfied_feedback(found)[1] == ("checks-found", "satisfied-unproven")
    told, ids = satisfied_feedback(found, tools=True)
    assert ids == ("checks-found", "satisfied-unproven-tools") and "write_file" in told
