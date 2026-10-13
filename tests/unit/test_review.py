"""Review (spec §4a): one call on the cross-family reviewer; JSON verdict + findings; one fix round."""

from pathlib import Path

from airlie.config import RoleSpec
from airlie.engine import FakeEngine, FakeReply
from airlie.topology.review import Reviewer, nudge_from_review, parse_review, review_message

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


def test_nudge_lists_blocking_findings_with_lines() -> None:
    text = nudge_from_review(parse_review(RC))
    assert text.startswith("A reviewer found blocking problems")
    assert "app/orders.py:41" in text and "minor" not in text
