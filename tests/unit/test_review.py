"""Review (spec §4a): one call on the cross-family reviewer; JSON verdict + findings; one fix round."""

from pathlib import Path

import pytest

from airlie.config import RoleSpec
from airlie.engine import EngineError, FakeEngine, FakeReply
from airlie.topology.review import Reviewer, nudge_from_review, parse_review, review_message, review_problem

RC = (
    '{"verdict": "request_changes", "findings": ['
    '{"severity": "blocking", "file": "app/orders.py", "line": 41, "claim": "qty == 0 reaches the division", '
    '"evidence": "guard is qty < 0"}, '
    '{"severity": "minor", "file": "app/orders.py", "claim": "name", "evidence": "x"}]}'
)


def test_parse_review_reads_verdict_and_findings() -> None:
    r = parse_review("to=user says:\n" + RC)
    assert r.verdict == "request_changes" and len(r.findings) == 2
    assert r.blocking[0].line == 41 and r.findings[1].line is None


def test_parse_review_unparsable() -> None:
    r = parse_review("Looks fine to me.")
    assert r.verdict == "unparsable" and r.findings == () and r.raw == "Looks fine to me."
    assert parse_review('{"verdict": "maybe"}').verdict == "unparsable"


def test_review_message_caps_diff_and_files() -> None:
    m = review_message("fix it", "d" * 100, "1 failed", {"a.py": "x" * 100}, diff_chars=10, files_chars=10)
    assert "dddddddddd\n[diff truncated" in m and "xxxxxxxxxx\n[file truncated" in m and "1 failed" in m


async def test_reviewer_calls_the_role_once_without_tools(tmp_path: Path) -> None:
    (tmp_path / "reviewer.md").write_text("---\nsummary: no\n---\nYou review.")
    eng = FakeEngine({"reviewer": [FakeReply(text=RC)]})
    role = RoleSpec(prompt=tmp_path / "reviewer.md", model="m", thinking=False, max_tokens=512)
    r = await Reviewer(eng, "reviewer", role).review("fix it", "diff", "tail", {"a.py": "code"})
    assert r.verdict == "request_changes"
    req = eng.requests[0]
    assert (
        req.tools is None and req.messages[0].content == "You review." and "diff" in req.messages[1].content
    )


async def test_a_reviewer_reply_cut_off_with_no_visible_text_is_an_error_not_an_unparsable_review(
    tmp_path: Path,
) -> None:
    """max_tokens ended the reply before the verdict: that is not "unparsable", it is a cut-off (HDS-3)."""
    (tmp_path / "reviewer.md").write_text("You review.")
    cut = FakeReply(reasoning="Let me check each file. ", finish_reason="length")
    role = RoleSpec(prompt=tmp_path / "reviewer.md", model="m", thinking=False, max_tokens=512)
    with pytest.raises(EngineError) as e:
        await Reviewer(FakeEngine({"reviewer": [cut]}), "reviewer", role).review("fix it", "diff", "tail", {})
    assert str(e.value) == (
        "the reply was cut off at max_tokens (512) before it ended, still thinking; "
        "raise roles.reviewer.max_tokens"
    )


async def test_a_reviewer_reply_that_has_text_or_stopped_by_itself_is_read_as_before(tmp_path: Path) -> None:
    (tmp_path / "reviewer.md").write_text("You review.")
    role = RoleSpec(prompt=tmp_path / "reviewer.md", model="m", thinking=False, max_tokens=512)
    half = FakeReply(text='{"verdict": "request_chan', finish_reason="length")
    quiet = FakeReply(reasoning="All of it was thinking. ", finish_reason="stop")
    for reply, raw in ((half, '{"verdict": "request_chan'), (quiet, "")):
        # each is unreadable, so it goes back once (the reply templates' fix turn) and is unreadable again
        engine = FakeEngine({"reviewer": [reply, reply]})
        r = await Reviewer(engine, "reviewer", role).review("fix it", "d", "t", {})
        assert (r.verdict, r.findings, r.raw) == ("unparsable", (), raw) and len(engine.requests) == 2


def test_nudge_lists_blocking_findings_with_lines() -> None:
    text = nudge_from_review(parse_review(RC))
    assert text.startswith("A reviewer found blocking problems")
    assert "app/orders.py:41" in text and "minor" not in text


def test_parse_review_finds_the_verdict_after_prose_with_braces() -> None:
    """AIR-7 layer 02, 2026-10-18: Muse's reasoning leaked in front of its verdict, and the first brace in it
    (`duckdb.query({...})`) made the whole reply unparsable, which skipped the fix round."""
    text = (
        'to=selfWe need to produce verdict JSON. The code builds {"a": 1} then fails.\n'
        '{"verdict": "request_changes", "findings": [{"severity": "blocking", "file": "m.py", "line": 29, '
        '"claim": "wrong params", "evidence": "docs"}]}'
    )
    r = parse_review(text)
    assert r.verdict == "request_changes" and [f.line for f in r.findings] == [29]
    assert parse_review('no verdict here {"x": 1}').verdict == "unparsable"


async def test_an_unreadable_verdict_goes_back_once_with_what_is_wrong_in_the_same_conversation(
    tmp_path: Path,
) -> None:
    """Reply templates (2026-11-02): an unparsable verdict skipped the fix round; now the reviewer is told
    what the schema says is wrong, once, after its own reply, and its second answer is the verdict."""
    (tmp_path / "reviewer.md").write_text("You review.")
    wrong = 'Mostly fine.\n```json\n{"verdict": "approved", "findings": []}\n```'
    eng = FakeEngine({"reviewer": [FakeReply(text=wrong), FakeReply(text=RC)]})
    role = RoleSpec(prompt=tmp_path / "reviewer.md", model="m", thinking=False, max_tokens=512)
    steers: list[str] = []
    r = await Reviewer(eng, "reviewer", role).review("fix it", "diff", "tail", {}, on_steer=steers.append)
    assert r.verdict == "request_changes" and len(r.blocking) == 1 and steers == ["review-fix"]
    first, second = eng.requests
    assert second.messages[: len(first.messages)] == first.messages
    assert [m.role for m in second.messages[-2:]] == ["assistant", "user"]
    assert second.messages[-2].content == wrong
    assert second.messages[-1].content.startswith(
        "Your verdict could not be read: verdict: 'approved' is not one of"
    )


async def test_a_verdict_still_unreadable_after_the_one_fix_is_unparsable(tmp_path: Path) -> None:
    (tmp_path / "reviewer.md").write_text("You review.")
    eng = FakeEngine({"reviewer": [FakeReply(text="Looks fine."), FakeReply(text="Still fine.")]})
    role = RoleSpec(prompt=tmp_path / "reviewer.md", model="m", thinking=False, max_tokens=512)
    r = await Reviewer(eng, "reviewer", role).review("fix it", "diff", "tail", {})
    assert (r.verdict, r.raw) == ("unparsable", "Still fine.") and len(eng.requests) == 2
    assert "no JSON object in the reply" in eng.requests[1].messages[-1].content


def test_review_problem_names_where_the_object_is_wrong() -> None:
    assert review_problem("no braces at all") == "no JSON object in the reply"
    bad = '{"verdict": "request_changes", "findings": [{"severity": "major", "file": "a.py", "claim": "x"}]}'
    assert review_problem(bad).startswith("findings[0].severity: 'major' is not one of")
    assert review_problem('{"findings": []}') == 'the JSON object has no "verdict"'
