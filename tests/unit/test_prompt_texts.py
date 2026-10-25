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

import pytest

from airlie.prompts import text
from airlie.story.board import text_card
from airlie.story.briefs import (
    built_so_far,
    layer_plan,
    layers_brief,
    refine_brief,
    survey_brief,
)
from airlie.story.layer_checks import (
    ASSERTION_FREE,
    EXISTING_FAILS,
    PASSES_BEFORE_CODE,
    Finding,
    amend_feedback,
    feedback,
    findings_text,
)
from airlie.story.lessons import Lesson
from airlie.story.model import Layer
from airlie.teams.native_team import _goal_message
from airlie.topology.diversify import nudge_text
from airlie.topology.studio_plan import Plan
from tests.unit.test_story_briefs import CARD, LAYERS, STORY

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


def _layer_plan(layers: tuple[Layer, ...], i: int, *, tester: bool, **kwargs: str) -> str:
    plan = layer_plan(
        STORY, layers, i, build_role="coder", tester_role="tester" if tester else None, **kwargs
    )
    return _plan_text(plan)


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
    "pre-code-check": lambda: text("pre-code-check"),
    "static-check": lambda: text("static-check"),
    "tests-only": lambda: text("tests-only"),
    "tests-contract": lambda: text("tests-contract"),
    "plan-round": lambda: text("plan-round"),
    "plan-fix": lambda: text("plan-fix", error="bad json"),
    "plan-unfinished": lambda: text("plan-unfinished"),
    "amend": lambda: text("amend"),
    "package-fix": lambda: text("package-fix"),
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
    "fn:layer_checks.feedback:tester": partial(feedback, FINDINGS),
    "fn:layer_checks.feedback:tester-advice": partial(feedback, FINDINGS, advice="Read the fixtures first."),
    "fn:layer_checks.feedback:coder": partial(feedback, FINDINGS, before_code=False),
    "fn:layer_checks.feedback:coder-advice": partial(
        feedback, FINDINGS, before_code=False, advice="Read the fixtures first."
    ),
    "fn:layer_checks.amend_feedback:bare": partial(amend_feedback, FAILING, "  "),
    "fn:layer_checks.amend_feedback:account": partial(amend_feedback, FAILING, ACCOUNT),
    "fn:layer_checks.amend_feedback:review": partial(amend_feedback, FAILING, "", BLOCKING),
    "fn:layer_checks.amend_feedback:full": partial(amend_feedback, FAILING, ACCOUNT, BLOCKING),
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
# layer_plan: every layer of the fixtures, solo (the coder writes tests and code) and with a tester first
CASES |= {
    f"fn:briefs.layer_plan:{i}:{'tester' if tester else 'solo'}": partial(
        _layer_plan, LAYERS, i, tester=tester
    )
    for i, tester in product(range(len(LAYERS)), (True, False))
}
CASES |= {
    f"fn:briefs.layer_plan:{name}:{'tester' if tester else 'solo'}": partial(
        _layer_plan, layers, 0, tester=tester
    )
    for (name, layers), tester in product(
        (("named-test-file", (ONE_TEST_FILE, *LAYERS)), ("named-test-files", (TWO_TEST_FILES, *LAYERS))),
        (True, False),
    )
}
CASES |= {
    f"fn:briefs.layer_plan:1:{'tester' if tester else 'solo'}-guide-built": partial(
        _layer_plan, LAYERS, 1, tester=tester, guide=GUIDE, built=built_so_far(DIFF)
    )
    for tester in (True, False)
}


def test_every_text_is_byte_identical_to_the_golden_snapshot() -> None:
    rendered = {k: f() for k, f in sorted(CASES.items())}
    if os.environ.get("AIRLIE_WRITE_PROMPT_TEXTS") == "1":
        GOLDEN.write_text(json.dumps(rendered, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        # the file now holds what was just rendered, so the comparison below could not fail: a variable left
        # exported would turn the guard off without a word
        pytest.fail("prompt_texts.json regenerated; rerun without AIRLIE_WRITE_PROMPT_TEXTS")
    assert rendered == json.loads(GOLDEN.read_text(encoding="utf-8"))
