"""A block that is airlie's own fault becomes a fix card on airlie's board, one per signature (planning-line
P5a, D5), and the line's bookkeeping after it (D6, Task 5)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from airlie.board import BoardStore
from airlie.engine import FakeEngine
from airlie.story import fixes
from airlie.story import pipeline as pipeline_module
from airlie.story.board import Card
from airlie.story.diagnosis import Classified, Diagnosis, Pack, Paths, write_diagnosis
from airlie.story.local_board import LocalBoard
from airlie.story.pipeline import StoryPipeline
from airlie.story.state import StoryState
from tests.unit.git_helpers import git
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


# ---- after the fix: the release (D6) ----


def home_checkout_with_a_fix_ahead(tmp_path: Path) -> tuple[Path, str]:
    """A home checkout at commit A, and the sha of a commit B on a branch it has not pulled: a fix merged on
    GitHub that the checkout does not have yet."""
    home = tmp_path / "home"
    home.mkdir()
    git(home, "init", "-q", "-b", "main")
    git(home, "config", "user.email", "t@example.com")
    git(home, "config", "user.name", "t")
    (home / "a.txt").write_text("a\n")
    git(home, "add", ".")
    git(home, "commit", "-qm", "A")
    git(home, "checkout", "-qb", "ahead")
    (home / "b.txt").write_text("b\n")
    git(home, "add", ".")
    git(home, "commit", "-qm", "B")
    fix_sha = git(home, "rev-parse", "HEAD")
    git(home, "checkout", "-q", "main")
    return home, fix_sha


def merged_gh(sha: str, *, state: str = "MERGED") -> Callable[[list[str]], Awaitable[str]]:
    async def gh(args: list[str]) -> str:
        assert args[:3] == ["gh", "pr", "view"]
        return json.dumps({"state": state, "mergeCommit": {"oid": sha}, "mergedAt": "2026-10-25T10:00:00Z"})

    return gh


def fix_with_blocked_story(tmp_path: Path, store: BoardStore) -> tuple[str, str, Path]:
    """A fix card whose story published a PR, and a story blocked by it: (fix key, story key, state dir)."""
    state = tmp_path / "state"
    fix = store.create(level="story", title="Line: AIR-9 blocked", labels=[fixes.FIX], created_by="line")
    story = store.create(level="story", title="A story")
    store.update(story.key, status="blocked", labels=[fixes.blocked_by_label(fix.key)])
    store.update(fix.key, status="doing")
    (state / "stories" / fix.key).mkdir(parents=True)
    (state / "stories" / fix.key / "state.json").write_text(
        json.dumps({"prs": [{"url": "https://github.com/o/r/pull/5"}]})
    )
    return fix.key, story.key, state


async def test_a_merged_fix_not_yet_in_the_checkout_gets_one_awaiting_deploy_note(tmp_path: Path) -> None:
    store = BoardStore(tmp_path / "board.sqlite")
    home, sha = home_checkout_with_a_fix_ahead(tmp_path)
    fix_key, story_key, state = fix_with_blocked_story(tmp_path, store)
    for _ in range(2):  # every beat looks; the note is written once
        got = await fixes.release_after_fix(
            store, state, store.get(fix_key), home=home, serve_commit=None, gh=merged_gh(sha)
        )
        assert got == []
    fix = store.get(fix_key)
    notes = [c.text for c in fix.comments if "task line:deploy" in c.text]
    assert len(notes) == 1 and story_key in notes[0] and fixes.AWAITING_DEPLOY in fix.labels
    assert fix.status == "doing" and store.get(story_key).status == "blocked"  # nothing resumed
    events = (state / "line" / "events.jsonl").read_text().splitlines()
    assert [json.loads(e)["kind"] for e in events] == ["fix_awaiting_deploy"]
    # an open PR is not a merge at all
    open_pr = await fixes.fix_prs_merged(store, state, fix, gh=merged_gh(sha, state="OPEN"))
    assert open_pr is None


async def test_a_fix_in_the_running_code_releases_the_stories_it_blocked_once_and_closes_the_card(
    tmp_path: Path,
) -> None:
    store = BoardStore(tmp_path / "board.sqlite")
    home, sha = home_checkout_with_a_fix_ahead(tmp_path)
    git(home, "merge", "-q", "--ff-only", sha)  # task line:deploy pulled it
    fix_key, story_key, state = fix_with_blocked_story(tmp_path, store)
    # the serve process started before the pull: it still runs the old code, so nothing resumes yet
    old = git(home, "rev-parse", "HEAD~1")
    held = await fixes.release_after_fix(
        store, state, store.get(fix_key), home=home, serve_commit=old, gh=merged_gh(sha)
    )
    assert held == [] and store.get(story_key).status == "blocked"
    got = await fixes.release_after_fix(
        store, state, store.get(fix_key), home=home, serve_commit=sha, gh=merged_gh(sha)
    )
    assert got == [story_key]
    story = store.get(story_key)
    assert story.status == "approved" and f"resumed_after:{fix_key}" in story.labels
    assert any(fix_key in c.text and "resumed" in c.text for c in story.comments)
    assert store.get(fix_key).status == "done"
    again = await fixes.release_after_fix(
        store, state, store.get(fix_key), home=home, serve_commit=sha, gh=merged_gh(sha)
    )
    assert again == []  # a done card is not looked at, and a story is released once
    kinds_logged = [json.loads(e)["kind"] for e in (state / "line" / "events.jsonl").read_text().splitlines()]
    assert kinds_logged == ["fix_awaiting_deploy", "resumed_after_fix"]  # the stale serve held it first


async def test_a_story_that_blocks_again_on_the_same_signature_after_a_fix_waits_for_a_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = Setup(tmp_path, monkeypatch)
    card, _ = await s.block("KeyError: 'x'")
    [fix] = [i for i in s.store.list(level="story") if fixes.FIX in i.labels]
    # the fix merged and deployed: the story is released with the label, and the card closed
    s.store.update(
        card.key, status="approved", labels=[*s.store.get(card.key).labels, f"resumed_after:{fix.key}"]
    )
    s.store.update(fix.key, status="done")
    st = StoryState.open(s.pipe.stories_dir / card.key, card, s.pipe.repo)
    st.emit("error", traceback=TRACEBACK.replace("line 812", "line 77"))
    await s.pipe._block(st, card, "KeyError: 'x'")
    assert len([i for i in s.store.list(level="story") if fixes.FIX in i.labels]) == 1  # no new card
    assert any(card.key in c.text and "same signature" in c.text for c in s.store.get(fix.key).comments)
    note = s.store.get(card.key).comments[-1].text
    assert f"{fix.key} was deployed" in note and "the fix did not take" in note
    assert kinds(st.path / "events.jsonl", "fix_did_not_take")


def transient_story(store: BoardStore, state: Path, signature: str = "transient:fetch_failed:intake") -> str:
    story = store.create(level="story", title="A story")
    store.update(story.key, status="blocked")
    pack = Pack(
        key=story.key, n=1, reason="the trunk could not be fetched", stage="intake", layer=None, attempt=1,
        run_id=None, subtask=None, role=None, stop_reason=None, refused_calls=0, holds=(), repeats=0,
        continuations=0, steps=0, tokens=0, minutes=0.0, gate_tail="", tracebacks=(), failures=(),
        base_gate_failed=None, base_failing=(), diff_stat="", paths=Paths(state / "e", None, None, ()),
    )  # fmt: skip
    c = Classified("transient", "fetch_failed", "intake", signature, ("fetch_failed",), "fetch_failed")
    write_diagnosis(state, Diagnosis(pack, c, None, "a retry"))
    return story.key


def test_a_transient_block_is_retried_twice_then_waits(tmp_path: Path) -> None:
    store = BoardStore(tmp_path / "board.sqlite")
    state = tmp_path / "state"
    key = transient_story(store, state)
    for n in (1, 2):
        assert fixes.transient_retry(store, state, store.get(key), retries=2) is True
        story = store.get(key)
        assert story.status == "approved" and f"retried:transient:fetch_failed:intake:{n}" in story.labels
        store.update(key, status="blocked")  # it blocked again on the same thing
    assert fixes.transient_retry(store, state, store.get(key), retries=2) is False  # a person's now
    assert store.get(key).status == "blocked"
    kinds_logged = [json.loads(e)["kind"] for e in (state / "line" / "events.jsonl").read_text().splitlines()]
    assert kinds_logged == ["transient_retry", "transient_retry"]
    # content blocks, and blocks with no diagnosis, are not retried
    other = store.create(level="story", title="B")
    store.update(other.key, status="blocked")
    assert fixes.transient_retry(store, state, store.get(other.key), retries=2) is False


async def test_the_beat_runs_the_bookkeeping_first_and_adds_its_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from airlie.engine import EngineHealth
    from airlie.story import factory, line

    store = BoardStore(tmp_path / "board.sqlite")
    cfg = cfg_for(tmp_path)
    monkeypatch.setattr(factory, "make_board", lambda c: LocalBoard(store))

    async def bookkeeping(c: object, s: BoardStore, **kw: object) -> list[str]:
        assert s is store
        return ["AIR-3: transient block, retried"]

    class Down:
        async def health(self) -> EngineHealth:
            return EngineHealth(False, {}, "down")

    monkeypatch.setattr(line, "bookkeep", bookkeeping)
    monkeypatch.setattr(factory, "_engine", lambda c: Down())
    # the fix lines come first, ahead of what the gates and the lanes say
    out = await factory.tick(cfg)
    assert out.startswith("AIR-3: transient block, retried; engine down: start it with")
