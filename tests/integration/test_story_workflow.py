"""Story mode end to end through `airlie story run AIR-n`: a story on the local board → product's story →
architect's one layer → the studio builds it test-first on its own branch → the sandbox's Test gate (pytest,
from its AGENTS.md) and the layer checks → a gh stack of draft PRs → the story in review.

The models are scripted behind the real HTTPEngine, and gh is recorded; the rest is the code a person runs.
This is the baseline: when it fails, story mode is broken for everyone, whatever the models do."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from airlie import cli
from airlie.engine import FakeReply, ToolCall
from airlie.story import factory
from tests.integration.story_harness import (
    TRUNK,
    ScriptedModels,
    board_item,
    board_story,
    engines_down,
    events,
    gate_command,
    live_story_config,
    sandbox_checkout,
    story_yaml,
    wire,
)
from tests.unit.git_helpers import git
from tests.unit.test_story_pipeline import RecordedGh, fenced

runner = CliRunner()

TITLE = "Add subtract"
REQUEST = "calc.py has add(a, b). Add subtract(a, b), which returns a minus b, with a test."
LAYER = "add-subtract-01-subtract"
"""The layer's branch, after the issue key: AIR-1-add-subtract-01-subtract."""
MILESTONES = [
    "story_start",
    "layer_start",
    "test_checks_passed",
    "layer_built",
    "published",
    "story_done",
]
"""The events a story that builds first time logs, in this order (others come between them). Mutation testing
is CI's, on the PR: the story has no stage for it."""

SUBTRACT = "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n"
TEST_SUBTRACT = (
    "from calc import add, subtract\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\n"
    "def test_subtract():\n    assert subtract(5, 3) == 2\n"
)


def write(call_id: str, path: str, content: str) -> FakeReply:
    return FakeReply(
        tool_calls=[ToolCall(call_id, "write_file", json.dumps({"path": path, "content": content}))]
    )


def scripts() -> dict[str, list[FakeReply]]:
    """What each role answers, in order, for a one-layer story that builds first time."""
    story = {
        "type": "story",
        "title": TITLE,
        "summary": "calc gains subtract(a, b).",
        "acceptance": [{"id": "AC1", "given": "two numbers", "when": "subtract is called", "then": "a - b"}],
    }
    layer = {
        "slug": "subtract",
        "title": "Subtract",
        "scope": "calc.py, test_calc.py",
        "acceptance": ["AC1"],
        "brief": "Add subtract(a, b) to calc.py and a test for it in test_calc.py.",
    }
    return {
        "product": [fenced(story)],
        "architect": [fenced({"layers": [layer]})],
        "coder": [
            write("t", "test_calc.py", TEST_SUBTRACT),
            write("c", "calc.py", SUBTRACT),
            FakeReply(tool_calls=[ToolCall("r", "run_tests", "{}")]),
            FakeReply(text="done: subtract added, tests pass"),
        ],
        "reviewer": [FakeReply(text='{"verdict": "approve", "findings": []}')],
    }


def test_a_board_story_is_built_gated_and_published_as_a_draft_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repos_root = tmp_path / "git"
    repo = sandbox_checkout(repos_root)
    config = story_yaml(tmp_path, repos_root)
    state_dir = tmp_path / "state"
    key = board_story(state_dir, TITLE, REQUEST)
    models, gh = ScriptedModels(scripts()), RecordedGh()
    wire(monkeypatch, gh, models)

    result = runner.invoke(cli.app, ["story", "run", key, "--config", str(config)])

    assert result.exit_code == 0, result.output
    assert models.left() == {}, f"replies never asked for: {models.left()}"
    branch = f"{key}-{LAYER}"
    # the board: the story is in review, with airlie's notes on it
    item = board_item(state_dir, key)
    assert item.status == "review"
    assert any(c.author == "airlie" for c in item.comments)
    # the layer: its own branch off the trunk, holding the change and its test
    assert git(repo, "merge-base", branch, TRUNK) == git(repo, "rev-parse", TRUNK)
    assert "def subtract" in git(repo, "show", f"{branch}:calc.py")
    assert "test_subtract" in git(repo, "show", f"{branch}:test_calc.py")
    # the story's state: built behind the AGENTS.md gate, reviewed, one PR
    story_dir = state_dir / "stories" / key
    state = json.loads((story_dir / "state.json").read_text())
    assert state["stage"] == "review" and state["blocked"] is None
    built = state["built"]["01"]
    assert built["branch"] == branch and built["gate"] == gate_command()
    assert [(s["role"], s["stop"], s["review"]) for s in built["subtasks"]] == [("coder", "done", "approve")]
    assert [p["branch"] for p in state["prs"]] == [branch]
    kinds = [e["kind"] for e in events(story_dir)]
    assert [k for k in kinds if k in MILESTONES] == MILESTONES, kinds
    # publishing: a stack of the one branch on the trunk, submitted, its PR described; nothing merges
    assert ["gh", "stack", "init", "--base", TRUNK, branch] in gh.calls
    assert ["gh", "stack", "submit", "--auto", "--remote", "origin"] in gh.calls
    assert any(c[:4] == ["gh", "pr", "edit", branch] for c in gh.calls)
    assert not any("merge" in c for c in gh.calls), "only a human merges"


@pytest.mark.live
async def test_live_models_build_the_same_story_to_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same story on the models airlie.yaml (and airlie.local.yaml) configure, with the story settings as
    they are: survey, planning rounds, tester, done-check. Only the state, the board and the checkouts are
    this test's own, and gh is recorded, so nothing reaches GitHub and the person's board and stories are
    untouched. Needs `task up` (and `task restart -- --topology studio`); skipped while an engine does not
    answer. The models decide the code, so this checks the outcome: the story in review, and its branch passes
    the gate."""
    cfg = live_story_config(tmp_path)
    if down := engines_down(cfg):
        pytest.skip(f"engine not answering: {', '.join(down)}; run `task up`")
    repo = sandbox_checkout(cfg.story.repos_root)
    key = board_story(cfg.observability.state_dir, TITLE, REQUEST)
    gh = RecordedGh()
    wire(monkeypatch, gh)
    board = factory.make_board(cfg)

    st = await factory.run_one(cfg, board=board, card=await board.card(key))

    assert st.blocked is None and st.stage == "review", st.blocked
    assert board_item(cfg.observability.state_dir, key).status == "review"
    branches = [p["branch"] for p in st.prs]
    assert branches, "no PR was opened"
    top = branches[-1]
    assert "def subtract" in git(repo, "show", f"{top}:calc.py")
    checkout = tmp_path / "top"
    git(repo, "worktree", "add", "-q", str(checkout), top)
    gate = subprocess.run(shlex.split(gate_command()), cwd=checkout, capture_output=True, text=True)
    assert gate.returncode == 0, gate.stdout[-2000:]
    assert ["gh", "stack", "init", "--base", TRUNK, *branches] in gh.calls
    assert not any("merge" in c for c in gh.calls), "only a human merges"
