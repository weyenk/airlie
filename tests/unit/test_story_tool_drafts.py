"""Every layer's package drafted with the file tools in one conversation (the tool-written packages design,
2026-11-06, build step 4): each layer's tests are the files changed since the layer before's snapshot, a
later layer edits an earlier layer's test file in place, and a draft is merged onto its layer's real base
three ways, so what the layers below changed in the same file since it was drafted is kept, and a change
that conflicts goes back to the architect. On FakeEngine, two calc layers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.story.board import text_card
from airlie.story.package import PACKAGE_TOOLS_CONTRACT
from airlie.story.state import StoryState
from tests.unit.git_helpers import git
from tests.unit.test_story_layer_checks_flow import PYTEST, calc_repo, write
from tests.unit.test_story_pipeline import STORY, fenced, kinds, package_reply, pipeline
from tests.unit.test_studio_orchestrator import APPROVE, DONE

PATH = "tests/test_calc.py"
TWO_LAYERS = fenced(
    {
        "layers": [
            {
                "slug": "mul",
                "title": "Multiply",
                "scope": "calc.py",
                "acceptance": ["AC1"],
                "brief": "Add mul.",
            },
            {
                "slug": "div",
                "title": "Divide",
                "scope": "calc.py",
                "acceptance": ["AC2"],
                "brief": "Add div.",
            },
        ]
    }
)
CALC = "def add(a, b):\n    return a + b\n"
MUL = "\n\ndef mul(a, b):\n    return a * b\n"
DIV = "\n\ndef div(a, b):\n    return a // b\n"
T01 = "from calc import mul\n\nFACTOR = 2\n\n\ndef test_mul():\n    assert mul(FACTOR, 3) == 6\n"
TEST_DIV = "\n\ndef test_div():\n    from calc import div\n\n    assert div(6, 3) == 2\n"


def edit(path: str, old: str, new: str) -> FakeReply:
    args = json.dumps({"path": path, "old": old, "new": new})
    return FakeReply(tool_calls=[ToolCall(f"e-{len(new)}", "edit_file", args)])


def json_only(ac: str, name: str) -> FakeReply:
    return package_reply(
        {},
        {ac: [f"{PATH}::{name}"]},
        files=[{"path": "calc.py", "action": "modify"}],
        interfaces=[{"file": "calc.py", "signature": f"def {name.removeprefix('test_')}(a, b) -> int"}],
        steps=[f"add {name.removeprefix('test_')} to calc.py"],
        prose="The tests are written.\n",
    )


ADD_DIV = edit(PATH, "    assert mul(FACTOR, 3) == 6\n", "    assert mul(FACTOR, 3) == 6\n" + TEST_DIV)
"""Layer 02's draft: its test added to layer 01's file, in place."""
CODER_FAKE_TEXT = T01.replace("FACTOR = 2\n", "FACTOR = 2  # the coder's\n")
CODER_FAKE = write(PATH, CODER_FAKE_TEXT)
"""Layer 01's coder changes the constant beside the locked test (allowed: the lock is the test's own
definition)."""


async def two_layers(
    tmp_path: Path, architect: list[FakeReply], coder: list[FakeReply]
) -> tuple[StoryState, FakeEngine, Path]:
    repo = calc_repo(tmp_path)
    engine = FakeEngine(
        {"product": [STORY], "architect": [TWO_LAYERS, *architect], "coder": coder, "reviewer": [APPROVE] * 4}
    )
    st = await pipeline(tmp_path, repo, engine, test_cmd=PYTEST, story={"package_tools": True}).run(
        text_card("divide")
    )
    return st, engine, repo


def built(repo: Path, st: StoryState, nn: str, path: str) -> str:
    return git(repo, "show", f"{st.built[nn]['branch']}:{path}")


def refs(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "refs/airlie/"], capture_output=True, text=True
    )
    return out.stdout.splitlines()


async def test_a_later_layers_draft_edits_the_earlier_ones_file_and_keeps_what_its_coder_changed(
    tmp_path: Path,
) -> None:
    st, engine, repo = await two_layers(
        tmp_path,
        [write(PATH, T01), json_only("AC1", "test_mul"), ADD_DIV, json_only("AC2", "test_div")],
        [write("calc.py", CALC + MUL), CODER_FAKE, DONE, write("calc.py", CALC + MUL + DIV), DONE],
    )
    assert st.blocked is None and set(st.built) == {"01", "02"}, st.blocked
    events = st.path / "events.jsonl"
    assert [(e["layer"], e["tests"]) for e in kinds(events, "package_drafted")] == [
        ("01", [f"{PATH}::test_mul"]),
        ("02", [f"{PATH}::test_div"]),
    ]
    assert [(e["layer"], e["paths"]) for e in kinds(events, "package_written")] == [
        ("01", [PATH]),
        ("02", [PATH]),
    ]
    # layer 02's file: the coder's change from layer 01 and the draft's new test, both
    assert built(repo, st, "01", PATH) == CODER_FAKE_TEXT.rstrip("\n")
    assert built(repo, st, "02", PATH) == (CODER_FAKE_TEXT + TEST_DIV).rstrip("\n")
    assert kinds(events, "draft_conflict") == []
    # the drafting conversation wrote with the tools, and its later turn asked for the tools' form
    drafting = [r for r in engine.requests if r.role == "architect"][1]
    assert PACKAGE_TOOLS_CONTRACT in drafting.messages[-1].content
    assert {"write_file", "edit_file"} <= {t.name for t in drafting.tools or []}
    assert refs(repo) == []  # the story published: its draft refs are gone


async def test_a_draft_that_conflicts_with_its_real_base_goes_back_in_the_conversation(
    tmp_path: Path,
) -> None:
    clash = edit(PATH, "FACTOR = 2\n", "FACTOR = 2\nDIVISOR = 3\n")  # beside the coder's change: a conflict
    st, engine, repo = await two_layers(
        tmp_path,
        [
            write(PATH, T01),
            json_only("AC1", "test_mul"),
            clash,
            json_only("AC2", "test_mul"),
            ADD_DIV,
            json_only("AC2", "test_div"),
        ],
        [write("calc.py", CALC + MUL), CODER_FAKE, DONE, write("calc.py", CALC + MUL + DIV), DONE],
    )
    assert st.blocked is None and set(st.built) == {"01", "02"}, st.blocked
    events = st.path / "events.jsonl"
    [conflict] = kinds(events, "draft_conflict")
    assert (conflict["layer"], conflict["paths"]) == ("02", [PATH])
    returned = [e for e in kinds(events, "package_returned") if e["layer"] == "02"]
    assert [(e["kinds"], e["charged"]) for e in returned] == [(["draft_conflict"], False)]
    [resumed] = kinds(events, "package_resumed")
    assert resumed["layer"] == "02"
    retry = [r for r in engine.requests if r.role == "architect"][-2].messages[-1].content
    assert "changed tests/test_calc.py where the layers below it changed the same lines" in retry
    assert "FACTOR = 2  # the coder's" in built(repo, st, "02", PATH)
    assert "def test_div" in built(repo, st, "02", PATH)
    assert [e["attempt"] for e in kinds(events, "package_ready") if e["layer"] == "02"] == [1]


async def test_each_layers_draft_snapshot_is_kept_under_a_ref_until_the_story_publishes(
    tmp_path: Path,
) -> None:
    """A draft's `at` is a commit of the drafting checkout, which is removed with the conversation: a ref per
    layer keeps it from git's garbage collection while a later layer may still read it. Here layer 02's build
    fails, so the story stops before it publishes and the refs are still there."""
    from airlie.story.cleanup import draft_ref, drop_draft_refs

    st, _, repo = await two_layers(
        tmp_path,
        [write(PATH, T01), json_only("AC1", "test_mul"), ADD_DIV, json_only("AC2", "test_div")],
        [write("calc.py", CALC + MUL), DONE, FakeReply(text="Nothing to change."), DONE, DONE],
    )
    assert st.blocked is not None and set(st.built) == {"01"}
    key = st.path.name
    assert [line.split()[-1] for line in refs(repo)] == [draft_ref(key, "01"), draft_ref(key, "02")]
    snap = git(repo, "rev-parse", draft_ref(key, "01"))
    assert git(repo, "show", f"{snap}:{PATH}") == T01.rstrip("\n")  # layer 01's files as drafted
    assert drop_draft_refs(repo, key) == 2 and refs(repo) == []
