"""A golden snapshot of every model-facing text the prompt hub moves out of Python.

CASES renders each text with fixed sample fields. A text that becomes a hub file is keyed by its hub id; the
output of a function that assembles prose has no single id and is keyed `fn:<module>.<function>:<variant>`.
data/prompt_texts.json holds what each rendered to before anything moved. The moves change a case's value
(the old constant becomes `text(id, ...)`), never its key and never the JSON: the models get the same bytes.

Regenerate the JSON only for a deliberate change to what a model is told:
    AIRLIE_WRITE_PROMPT_TEXTS=1 uv run pytest tests/unit/test_prompt_texts.py -q
That run writes the file and then fails on purpose, so a variable left exported cannot disarm the guard."""

import json
import os
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from itertools import product
from pathlib import Path
from typing import Any

import pytest

from airlie.prompts import text
from airlie.story.amigos import ProofTest
from airlie.story.board import text_card
from airlie.story.briefs import (
    built_so_far,
    consult_brief,
    consult_round_text,
    criteria_revision_brief,
    critic_brief,
    directions_brief,
    imagine_brief,
    layers_brief,
    package_blocks,
    package_brief,
    plan_brief,
    proof_revision_brief,
    questions_brief,
    read_back_text,
    read_brief,
    refine_brief,
    render_brief,
    replan_brief,
    revise_brief,
    survey_brief,
)
from airlie.story.initiative import Ask, Attachment, ReadReply
from airlie.story.layer_checks import (
    ASSERTION_FREE,
    EXISTING_FAILS,
    PASSES_BEFORE_CODE,
    Finding,
    findings_text,
)
from airlie.story.lessons import Lesson
from airlie.story.mockups import Direction
from airlie.story.model import Layer, ScopedPath
from airlie.story.package import Interface, Package, PackageFile
from airlie.story.package_checks import package_feedback_said
from airlie.teams.native_team import _goal_message
from airlie.topology.diversify import nudge_text
from airlie.topology.studio_plan import Plan
from tests.unit.test_story_briefs import (
    CARD,
    EPIC,
    EPIC_TEXT,
    INITIATIVE,
    LAYERS,
    REPLAN_PLAN,
    REPLAN_PROGRESS,
    STORY,
)

GOLDEN = Path(__file__).parent / "data" / "prompt_texts.json"

N, TOTAL, K, WHAT, TOOL, P, LABEL, WIDTH = 7, 40, 3, "no test", "read_file", 2, "b", 3
HINT = "/tmp/wt"
TAIL, MAX_PROMPT, MAX_CONTEXT = "…sample tail…", 49200, 65536


DESCRIBED = replace(
    CARD, description="Users want avatars.\nPNG only.", comments=("PNG only", "Max 1 MB per file")
)
BAD_JSON = "no JSON object in the reply"
GUIDE = "- src/store.py: def save(avatar)"
DIFF = "diff --git a/src/store.py b/src/store.py\n+++ b/src/store.py\n+def save(avatar):\n+class Store:\n"
ONE_TEST_FILE = Layer("tools", "Tools", "src/a.py; tests/unit/test_x.py", "Offer every schema.", ("AC1",))
TWO_TEST_FILES = Layer("t", "T", "tests/unit/test_a.py; tests/unit/test_b.py", "Both.", ("AC1",))

FINDINGS = (
    Finding(
        "passes_before_code",
        "tests/unit/test_avatar.py::test_rejects_an_unknown_format",
        PASSES_BEFORE_CODE,
        'def test_rejects_an_unknown_format():\n    with pytest.raises(ValueError, match="format"):\n'
        '        store("x.gif")\n',
    ),
    Finding(
        "assertion_free_test",
        "tests/unit/test_avatar.py::test_stores_a_png",
        ASSERTION_FREE,
        'def test_stores_a_png():\n    store("x.png")\n',
    ),
)
FOUND_AND_UNSOURCED = (
    FINDINGS[0],
    Finding("existing_test_fails", "tests/unit/test_old.py::test_old", EXISTING_FAILS, ""),
)
FAILING = ["tests/unit/test_avatar.py::test_a", "tests/unit/test_avatar.py::test_b"]
ACCOUNT = "I built it; test_a unpacks two values and the card has one."
BLOCKING = ("The empty file is not rejected.",)


def _plan_text(plan: Plan) -> str:
    return "\n\n".join(f"[{s.id}]\n{s.brief}" for s in plan.subtasks)


SAMPLE_PACKAGE = Package(
    "profile",
    {"tests/unit/test_profile.py": "def test_shows():\n    assert show() == 1\n"},
    (PackageFile("src/profile.py", "create", "the profile view"),),
    (Interface("src/profile.py", "def show() -> int", "returns 1, the avatar id"),),
    ("create src/profile.py with show() returning 1", "export show from src/__init__.py"),
    {"AC2": ("tests/unit/test_profile.py::test_shows",)},
    "run_tests passes on tests/unit/test_profile.py",
)


def _package_brief(i: int, **kwargs: Any) -> str:
    return package_brief(STORY, LAYERS, i, max_steps=40, context_budget=65_536, **kwargs)


CASES: dict[str, Callable[[], str]] = {
    # --- teams/native_team.py: steering the loop sends
    "malformed-call": lambda: text("malformed-call"),
    "no-tools": lambda: text("no-tools"),
    "last-step": lambda: text("last-step", n=N),
    "write-now": lambda: text("write-now", n=N, total=TOTAL),
    "write-now-continuing": lambda: text("write-now-continuing", n=N, total=TOTAL),
    "write-only": lambda: text("write-only", what=WHAT, n=N, k=K),
    "tests-now": lambda: text("tests-now", n=N, total=TOTAL),
    "code-now": lambda: text("code-now", n=N, total=TOTAL),
    "change-now": lambda: text("change-now", n=N, total=TOTAL),
    "read-refused": lambda: text("read-refused"),
    "read-left": lambda: "\n\n" + text("read-left", k=K),  # the loop puts it after a blank line
    "repeated": lambda: text("repeated", n=N, tool=TOOL),
    "cycled": lambda: text("cycled", p=P, n=N, tool=TOOL),
    "repeat-write-only": lambda: text("repeat-write-only", tool=TOOL),
    "act-first": lambda: text("act-first"),
    "still-red": lambda: text("still-red"),
    "last-turn-retry": lambda: text("last-turn-retry"),
    "one-step-left": lambda: text("one-step-left"),
    "fresh-run": lambda: text("fresh-run", n=N),
    "reasoning-carried": lambda: text("reasoning-carried", tail=TAIL),
    "context-pressure": lambda: text("context-pressure", max_prompt=MAX_PROMPT, max_context=MAX_CONTEXT),
    # --- tools/loop.py, topology/studio.py, serve/app.py
    "no-more-tools": lambda: text("no-more-tools"),
    "fresh-continuation": lambda: text("fresh-continuation"),
    "done-check": lambda: text("done-check"),
    "gate-failed": lambda: text("gate-failed"),
    # serve/app.py puts a blank line before the rule; test_serve.py pins that, this case only restates it
    "client-bridge": lambda: "\n\n" + text("client-bridge"),
    # --- story/briefs.py, story/rounds.py, story/layer_checks.py
    "test-rules": lambda: text("test-rules"),
    "plan-round": lambda: text("plan-round"),
    "plan-fix": lambda: text("plan-fix", error="bad json"),
    "plan-unfinished": lambda: text("plan-unfinished"),
    "package-fix": lambda: text("package-fix"),
    "amigos-findings": lambda: text("amigos-findings"),
    "amigos-opening": lambda: text("amigos-opening"),
    "amigos-turn": lambda: text("amigos-turn"),
    "amigos-product": lambda: text("amigos-product"),
    "amigos-architect": lambda: text("amigos-architect"),
    "amigos-tester": lambda: text("amigos-tester"),
    "amigos-proof-revision": lambda: text("amigos-proof-revision"),
    "package-critic": lambda: text("package-critic"),
    "package-replan": lambda: text("package-replan"),
    "passes-before-code-choice": lambda: "  " + text("passes-before-code-choice"),
    "loose-match": lambda: "  " + text("loose-match"),  # feedback indents it under its finding
}

# --- text assembled by a function: every branch of the prose it picks
CASES |= {
    "fn:native_team._goal_message:"
    + "-".join(
        [
            "tools" if tools else "no-tools",
            "team" if team else "plain",
            *(["preamble"] if preamble else []),
        ]
    ): partial(_goal_message, "Add the avatar upload.", HINT, team, preamble, tools=tools)
    for tools, team, preamble in product((True, False), ("", "team-a"), (None, "Project rules apply.\n"))
}
CASES |= {
    "fn:native_team._goal_message:read-tools-team": partial(
        _goal_message, "Add the avatar upload.", HINT, "team-a", None, tools=True, writes=False
    )
}
CASES |= {
    "fn:diversify.nudge_text:bare": partial(nudge_text, LABEL, WIDTH, None, ""),
    "fn:diversify.nudge_text:full": partial(
        nudge_text,
        LABEL,
        WIDTH,
        "the parser drops the last row",
        "FAILED tests/unit/test_x.py::test_y - assert 1 == 2",
        (TOOL, '{"path": ""}', "error: path is required"),
    ),
    "fn:diversify.nudge_text:repeated-tail": partial(
        nudge_text,
        LABEL,
        WIDTH,
        None,
        "FAILED tests/unit/test_x.py::test_y - assert 1 == 2",
        (TOOL, '{"path": ""}', "error: path is required"),
    ),
    "fn:diversify.nudge_text:hypothesis": partial(
        nudge_text, LABEL, WIDTH, "the parser drops the last row", ""
    ),
}
CASES |= {
    "fn:package_checks.package_feedback_said": lambda: package_feedback_said(FINDINGS)[0],
    "fn:package_checks.package_feedback_said:unsourced": partial(
        lambda found: package_feedback_said(found)[0], FOUND_AND_UNSOURCED
    ),
    "fn:layer_checks.findings_text:two": partial(findings_text, FOUND_AND_UNSOURCED),
}
CASES |= {
    "fn:briefs.refine_brief:bare": partial(refine_brief, CARD),
    "fn:briefs.refine_brief:full": partial(refine_brief, DESCRIBED, BAD_JSON),
    "fn:briefs.refine_brief:text-request": partial(
        refine_brief, text_card("Add an avatar upload.\nUsers upload a PNG.")
    ),
    "fn:briefs.survey_brief:bare": partial(survey_brief, CARD),
    "fn:briefs.survey_brief:described": partial(survey_brief, DESCRIBED),
    "fn:briefs.layers_brief:plain": partial(layers_brief, STORY, 4),
    "fn:briefs.layers_brief:lessons": partial(
        layers_brief,
        STORY,
        4,
        lessons=(
            Lesson(
                title="Layer 01 storage",
                acceptance=["AC1: the avatar is stored"],
                source_files=5,
                test_files=2,
                steps=30,
                context_tokens=88_000,
                ending="out_of_steps",
            ),
        ),
    ),
    "fn:briefs.layers_brief:rounds": partial(layers_brief, STORY, 4, round_steps=6),
    "fn:briefs.layers_brief:guide-survey": partial(
        layers_brief, STORY, 4, guide=GUIDE, survey="- store.py holds the writes."
    ),
    "fn:briefs.layers_brief:full": partial(
        layers_brief,
        replace(STORY, assumptions=("PNG only",)),
        3,
        BAD_JSON,
        guide=GUIDE,
        survey="- store.py holds the writes.",
        round_steps=6,
    ),
    "fn:briefs.built_so_far:diff": partial(built_so_far, DIFF),
}
# the package brief and the build brief: every layer of the fixtures, plain and with everything optional
CASES |= {f"fn:briefs.package_brief:{i}": partial(_package_brief, i) for i in range(len(LAYERS))}
CASES["fn:briefs.package_brief:full"] = partial(
    _package_brief,
    1,
    guide=GUIDE,
    built=built_so_far(DIFF),
    feedback=BAD_JSON,
    lessons=[
        Lesson(
            title="Layer 01 storage",
            acceptance=["AC1: the avatar is stored"],
            source_files=5,
            test_files=2,
            steps=30,
            context_tokens=88_000,
            ending="package_rejected",
            detail="passes_before_code",
        )
    ],
)
AGREED = (
    ProofTest(
        "AC2",
        "profile",
        "tests/unit/test_profile.py",
        "test_shows",
        "unit",
        "unit",
        "the url shows",
        "an avatar",
    ),
    ProofTest(None, "profile", "tests/unit/test_profile.py", "test_title", "unit", None, "the title", ""),
)
PROFILE = replace(LAYERS[1], files=(ScopedPath("src/profile.py", True),), interfaces=("def show() -> int",))
CASES["fn:briefs.package_brief:agreed"] = partial(
    package_brief,
    STORY,
    (LAYERS[0], PROFILE, LAYERS[2]),
    1,
    max_steps=40,
    context_budget=65_536,
    proof=AGREED,
)
CASES["fn:briefs.critic_brief:plain"] = partial(
    critic_brief, PROFILE, SAMPLE_PACKAGE.tests, AGREED, "## The repository's test gate\n- uv run pytest"
)
CASES["fn:briefs.proof_revision_brief:plain"] = partial(
    proof_revision_brief,
    STORY,
    PROFILE,
    AGREED,
    (("tests/unit/test_profile.py::test_shows", "the page is not rendered in this layer"),),
    "## The repository's test gate\n- uv run pytest",
)
CASES["fn:briefs.criteria_revision_brief:plain"] = partial(
    criteria_revision_brief, STORY, (("AC1", "a PNG cannot be stored at zero bytes"),)
)
CASES["fn:briefs.package_blocks:plain"] = partial(package_blocks, STORY, LAYERS, 1, SAMPLE_PACKAGE)
CASES["fn:briefs.package_blocks:foundation"] = partial(
    package_blocks, STORY, LAYERS, 2, replace(SAMPLE_PACKAGE, covers={})
)
CASES |= {
    "brief-package-task": lambda: text("brief-package-task"),
    "plan-brief": lambda: text("plan-brief", max_stories=8, max_scope_files=8),
    "plan-replan": lambda: text("plan-replan", max_stories=8, max_scope_files=8),
    "fn:briefs.replan_brief:plain": partial(
        replan_brief,
        EPIC_TEXT,
        EPIC,
        REPLAN_PLAN,
        REPLAN_PROGRESS,
        reason="requested with task epic:replan",
        max_stories=8,
        max_scope_files=8,
    ),
    "fn:briefs.replan_brief:full": partial(
        replan_brief,
        EPIC_TEXT,
        EPIC,
        REPLAN_PLAN,
        REPLAN_PROGRESS,
        comments=["Please make the first step smaller."],
        reason="AIR-5 stopped again after its resume",
        guide=GUIDE,
        lessons=[Lesson(title="Layer 01 storage", acceptance=["AC1"], ending="out_of_steps")],
        max_stories=6,
        max_scope_files=4,
        feedback=BAD_JSON,
        round_steps=6,
    ),
    "fn:briefs.plan_brief:plain": partial(plan_brief, EPIC_TEXT, EPIC, max_stories=8, max_scope_files=8),
    "fn:briefs.plan_brief:full": partial(
        plan_brief,
        EPIC_TEXT,
        EPIC,
        guide=GUIDE,
        conventions="tests live in tests/unit",
        lessons=[Lesson(title="Layer 01 storage", acceptance=["AC1"], ending="out_of_steps")],
        max_stories=6,
        max_scope_files=4,
        feedback=BAD_JSON,
        round_steps=6,
    ),
    "package-tests-rules": lambda: text("package-tests-rules"),
    "package-contract": lambda: text("package-contract"),
}
CASES |= {
    "specs-questions": lambda: text("specs-questions", max_questions=5),
    "fn:briefs.questions_brief:plain": partial(
        questions_brief, INITIATIVE, {}, [], max_questions=5, feedback=None
    ),
    "fn:briefs.questions_brief:full": partial(
        questions_brief,
        INITIATIVE,
        {"o/r": "# R\nreadme text"},
        [{"q": "What is it for?", "a": "Speed."}, {"q": "Who uses it?", "a": None}],
        max_questions=5,
        feedback=BAD_JSON,
    ),
    "specs-imagine": lambda: text("specs-imagine", seats="architect, ux, design", max_asks=4, rounds_left=3),
    "specs-consult": lambda: text("specs-consult", chars=4000),
    "specs-round-more": lambda: text("specs-round-more", rounds_left=2),
    "specs-round-due": lambda: text("specs-round-due"),
    "specs-round-refused": lambda: text("specs-round-refused", seats="docs, ux", limit=8),
    "fn:briefs.imagine_brief:plain": partial(
        imagine_brief,
        INITIATIVE,
        [],
        [],
        surveys={},
        lessons=[],
        consultable=["architect", "ux"],
        max_asks=4,
        rounds_left=3,
        budget_hours=None,
        feedback=None,
    ),
    "fn:briefs.imagine_brief:full": partial(
        imagine_brief,
        INITIATIVE,
        [{"q": "What is it for?", "a": "Speed."}],
        ["one user"],
        surveys={"o/r": "The board lives in web/src/Board.vue"},
        lessons=[Lesson(title="Layer 01 storage", acceptance=["AC1"], ending="out_of_steps")],
        consultable=["architect", "ux"],
        max_asks=4,
        rounds_left=1,
        budget_hours=20.0,
        feedback=BAD_JSON,
        attachments=[
            Attachment("notes.txt", "text", "Make it like Linear.", None),
            Attachment("screenshot.png", "image", None, Path("/x/shot.png")),
        ],
    ),
    "fn:briefs.consult_brief": partial(
        consult_brief, INITIATIVE, [{"q": "q", "a": "a"}], Ask("ux", "How would a person move?"), chars=4000
    ),
    "fn:briefs.consult_round_text:more": partial(
        consult_round_text,
        [(Ask("ux", "How would a person move?"), "- one keystroke"), (Ask("design", "dense?"), "- tight")],
        chars=4000,
        rounds_left=2,
        surveys={"o/r": "late notes"},
    ),
    "specs-mockup-directions": lambda: text("specs-mockup-directions", count=3),
    "specs-mockup-render": lambda: text("specs-mockup-render", max_chars=12000),
    "specs-mockup-look": lambda: text("specs-mockup-look"),
    "fn:briefs.directions_brief:plain": partial(
        directions_brief, INITIATIVE, [], Ask("design", "the board screen", "mockups", "E1"), count=3
    ),
    "fn:briefs.directions_brief:full": partial(
        directions_brief,
        INITIATIVE,
        [{"q": "What is it for?", "a": "Speed."}],
        Ask("design", "the board screen", "mockups", "E1"),
        count=3,
        attachments=[Attachment("linear.png", "image", None, Path("/x/linear.png"))],
        feedback=BAD_JSON,
    ),
    "fn:briefs.render_brief": partial(
        render_brief,
        Direction("Columns", "A column per status", "Header, then columns of cards", "Columns, not rows"),
        "Light, serif, one blue accent.",
        max_chars=12000,
    ),
    "specs-read": lambda: text("specs-read"),
    "specs-read-back": lambda: text("specs-read-back"),
    "fn:briefs.read_brief": partial(read_brief, "# Fast board\n\n## Why\nThe board feels slow.\n"),
    "fn:briefs.read_back_text": partial(
        read_back_text, [ReadReply("E1", False, ("Which store holds the cards?",)), ReadReply("E2", True, ())]
    ),
    "specs-revise": lambda: text("specs-revise"),
    "fn:briefs.revise_brief:plain": partial(
        revise_brief, INITIATIVE, "# Fast board\n\n## Why\nSlow.\n", [], "Make it smaller.", feedback=None
    ),
    "fn:briefs.revise_brief:full": partial(
        revise_brief,
        INITIATIVE,
        "# Fast board\n\n## Why\nSlow.\n",
        [{"seat": "ux", "question": "How?", "answer": "- keys"}],
        "Make it smaller.",
        feedback=BAD_JSON,
    ),
    "fn:briefs.consult_round_text:due": partial(
        consult_round_text,
        [(Ask("ux", "How?"), "- a")],
        chars=4000,
        rounds_left=0,
        refused=[Ask("docs", "d?")],
        limit=8,
    ),
}


def test_every_text_is_byte_identical_to_the_golden_snapshot() -> None:
    rendered = {k: f() for k, f in sorted(CASES.items())}
    if os.environ.get("AIRLIE_WRITE_PROMPT_TEXTS") == "1":
        GOLDEN.write_text(json.dumps(rendered, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        # the file now holds what was just rendered, so the comparison below could not fail: a variable left
        # exported would turn the guard off without a word
        pytest.fail("prompt_texts.json regenerated; rerun without AIRLIE_WRITE_PROMPT_TEXTS")
    assert rendered == json.loads(GOLDEN.read_text(encoding="utf-8"))
