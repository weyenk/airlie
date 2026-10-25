"""A block that is airlie's own fault becomes a fix card on airlie's board, one per signature (planning-line
P5a, D5), and the line's bookkeeping after it (D6, Task 5)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from airlie.board import BoardStore
from airlie.engine import FakeEngine
from airlie.story import fixes
from airlie.story import pipeline as pipeline_module
from airlie.story.board import Card
from airlie.story.local_board import LocalBoard
from airlie.story.pipeline import StoryPipeline
from airlie.story.state import StoryState
from tests.unit.test_fork_orchestrator import repo_with_marker_tests
from tests.unit.test_story_pipeline import RecordingBoard, kinds, pipeline
from tests.unit.test_studio_orchestrator import cfg_for

TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/r/src/airlie/story/pipeline.py", line 812, in _build_once\n    x = 1\n'
    '  File "/r/src/airlie/story/state.py", line 9, in save\n    y = 2\n'
    "KeyError: 'x'"
)
HOME = "weyenk/airlie"


class Setup:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = repo_with_marker_tests(tmp_path)
        self.store = BoardStore(tmp_path / "board.sqlite")
        self.board = LocalBoard(self.store)
        self.pipe: StoryPipeline = pipeline(tmp_path, repo, FakeEngine(), board=self.board)
        monkeypatch.setattr(pipeline_module, "home_repo", lambda cfg, checkout: HOME)

    async def block(self, reason: str, *, traceback: str | None = TRACEBACK) -> tuple[Card, StoryState]:
        """A new story on the board, stopped with `reason` (after an unhandled error when `traceback`)."""
        item = self.store.create(level="story", title="A story", description="d", created_by="noah")
        card = await self.board.card(item.key)
        st = StoryState.open(self.pipe.stories_dir / card.key, card, self.pipe.repo)
        if traceback is not None:
            st.emit("error", traceback=traceback)
        await self.pipe._block(st, card, reason)
        return card, st


async def test_a_process_block_files_a_fix_card_with_its_sections_and_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = Setup(tmp_path, monkeypatch)
    card, st = await s.block("KeyError: 'x'")
    [fix] = [i for i in s.store.list(level="story") if fixes.FIX in i.labels]
    assert fix.status == "todo" and fix.created_by == "line" and fix.level == "story"
    sig = "process:KeyError:airlie.story.state:save"
    assert set(fix.labels) == {fixes.FIX, f"repo:{HOME}", f"signature:{sig}", f"blocks:{card.key}"}
    assert fix.title == f"Line: {card.key} blocked at new: KeyError"
    for section in (
        "## Why",
        "## Evidence",
        "## Hypothesis",
        "## Reproduce",
        "## Acceptance criteria",
        "## Out of scope",
    ):
        assert section in fix.description
    assert "KeyError: 'x'" in fix.description and f"task line:why -- {card.key}" in fix.description
    assert "does not raise KeyError in airlie.story.state:save" in fix.description
    assert f"blocked_by:{fix.key}" in s.store.get(card.key).labels
    [filed] = kinds(st.path / "events.jsonl", "fix_filed")
    assert (filed["fix"], filed["signature"]) == (fix.key, sig)
    note = s.store.get(card.key).comments[-1].text
    assert f"Next: fix card {fix.key}" in note
    assert f"fix card {fix.key}" in (st.path / "diagnosis" / "1.md").read_text()


async def test_a_second_block_with_the_same_signature_links_the_open_card_instead_of_a_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = Setup(tmp_path, monkeypatch)
    first, _ = await s.block("KeyError: 'x'")
    # the same bug after an edit moved its lines: the traceback differs, the signature does not
    moved = TRACEBACK.replace("line 812", "line 903").replace("line 9,", "line 41,")
    second, st2 = await s.block("KeyError: 'x'", traceback=moved)
    fixes_open = [i for i in s.store.list(level="story") if fixes.FIX in i.labels]
    assert len(fixes_open) == 1
    [fix] = fixes_open
    assert {f"blocks:{first.key}", f"blocks:{second.key}"} <= set(fix.labels)
    assert any(c.text == f"also blocks {second.key}: block 1, KeyError: 'x'" for c in fix.comments)
    assert f"blocked_by:{fix.key}" in s.store.get(second.key).labels
    [linked] = kinds(st2.path / "events.jsonl", "fix_linked")
    assert linked["fix"] == fix.key
    # a done card is a fixed bug: the next block with its signature is a new card
    s.store.update(fix.key, status="done")
    await s.block("KeyError: 'x'")
    assert len([i for i in s.store.list(level="story") if fixes.FIX in i.labels]) == 2


async def test_a_config_block_files_a_card_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = Setup(tmp_path, monkeypatch)
    # a planning role out of steps: stage intake, the run's last subtask stopped max_steps
    item = s.store.create(level="story", title="A story", description="d")
    card = await s.board.card(item.key)
    st = StoryState.open(s.pipe.stories_dir / card.key, card, s.pipe.repo)
    run = s.pipe.cfg.observability.state_dir / "runs" / "r1"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text(
        '{"ts": 1, "kind": "subtask_done", "subtask": "refine", "role": "product", '
        '"stop_reason": "max_steps"}\n'
    )
    st.emit("run", stage="intake", studio_run="r1")
    await s.pipe._block(st, card, "product did not produce a usable story: the product did not finish")
    [fix] = [i for i in s.store.list(level="story") if fixes.FIX in i.labels]
    assert "signature:config:max_steps:product" in fix.labels
    assert "step cap" in fix.description


async def test_a_content_or_transient_block_files_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = Setup(tmp_path, monkeypatch)
    await s.block("layer 01 (B) changed nothing", traceback=None)  # content
    await s.block("the trunk main could not be fetched from origin", traceback=None)  # transient
    assert [i for i in s.store.list(level="story") if fixes.FIX in i.labels] == []
    assert len(s.store.list(level="story")) == 2  # only the two stories


async def test_a_jira_board_gets_the_text_as_a_note_and_no_labels(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    board = RecordingBoard()
    pipe = pipeline(tmp_path, repo, FakeEngine(), board=board)
    card = Card("AIR-7", "1", "AIR-7", "t", "d")
    st = StoryState.open(pipe.stories_dir / card.key, card, pipe.repo)
    st.emit("error", traceback=TRACEBACK)
    await pipe._block(st, card, "KeyError: 'x'")
    texts = [n for n in board.notes if "A fix card for airlie's own board" in n]
    assert (
        len(texts) == 1
        and "Line: AIR-7 blocked at new: KeyError" in texts[0]
        and "## Acceptance criteria" in texts[0]
    )
    assert "written as a note on this issue" in board.notes[-1]
    assert board.moves[-1] == "blocked"
    assert kinds(st.path / "events.jsonl", "fix_noted")


def test_line_config_has_defaults_and_the_home_repo_falls_back_to_the_checkouts_remote(
    tmp_path: Path,
) -> None:
    cfg = cfg_for(tmp_path)
    assert (cfg.line.home_repo, cfg.line.transient_retries) == (None, 2)
    checkout = tmp_path / "home"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/weyenk/airlie.git"], cwd=checkout, check=True
    )
    assert fixes.home_repo(cfg, checkout) == "weyenk/airlie"
    assert fixes.home_repo(cfg, tmp_path / "not-a-checkout") is None
    set_up = cfg_for(tmp_path, extra={"line": {"home_repo": "o/mine", "transient_retries": 0}})
    assert fixes.home_repo(set_up, checkout) == "o/mine" and set_up.line.transient_retries == 0
