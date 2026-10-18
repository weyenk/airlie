"""scripts/web_e2e.py: how a role's answer is judged (the model calls are the live run's)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "web_e2e.py"
spec = importlib.util.spec_from_file_location("web_e2e", SCRIPT)
assert spec and spec.loader
we = importlib.util.module_from_spec(spec)
sys.modules["web_e2e"] = we  # the script's dataclasses look their module up by name
spec.loader.exec_module(we)


def test_cited_urls_are_found_in_order_without_sentence_punctuation_or_repeats() -> None:
    text = "See https://a.org/x, and (https://b.org/y). Also https://a.org/x. Then `https://c.org/z`!"
    assert we.cited_urls(text) == ["https://a.org/x", "https://b.org/y", "https://c.org/z"]
    assert we.cited_urls("no links here") == []


def test_two_spellings_of_one_page_are_the_same_url() -> None:
    assert we.normal("HTTPS://Example.ORG/a/b/#top") == we.normal("https://example.org/a/b")
    assert we.normal("https://example.org/a?x=1") != we.normal("https://example.org/a?x=2")


def test_a_cited_url_is_grounded_only_when_a_tool_result_or_fetch_had_it() -> None:
    seen = we.urls_in_results(
        ["[untrusted web text]\nTitle\nhttps://news.example/story-1\nsnippet"], ["https://page.example/read/"]
    )
    cited = ["https://news.example/story-1", "https://invented.example/x", "https://page.example/read"]
    assert we.grounded(cited, seen) == ["https://news.example/story-1", "https://page.example/read"]


def result(**kw: object) -> object:
    base = dict(
        role="chief",
        question="q",
        answer="Story at https://news.example/a",
        calls=[{"tool": "web_search", "arg": "x", "ok": True}],
        seen={"https://news.example/a"},
    )
    return we.RoleResult(**{**base, **kw})


def test_a_role_passes_when_it_searched_and_cited_what_it_found() -> None:
    assert result().passed


def test_a_role_that_answered_from_memory_or_invented_its_link_fails() -> None:
    assert not result(calls=[]).passed  # no web call
    assert not result(
        calls=[{"tool": "web_fetch", "arg": "u", "ok": False}]
    ).passed  # a refused call is no call
    assert not result(answer="From memory, no link.").passed
    assert not result(answer="See https://invented.example/a").passed  # a link no tool ever showed
    assert not result(answer="  ").passed
    assert not result(error="EngineError: down").passed


def test_a_role_with_a_checkable_answer_passes_on_the_check_without_a_cited_url() -> None:
    right = result(answer="request_changes: 0.6.0 does not exist; latest is 0.5.0", seen=set(), oracle=True)
    assert right.passed
    assert not result(answer="approve", seen=set(), oracle=False).passed  # a wrong answer, searched or not
    assert not result(
        answer="request_changes", seen=set(), oracle=True, calls=[]
    ).passed  # right but no search


def test_calls_are_read_from_the_run_directorys_logs(tmp_path: Path) -> None:
    (tmp_path / "web_search.jsonl").write_text(
        json.dumps({"role": "chief", "query": "news", "status": "ok"})
        + "\n"
        + json.dumps({"role": "chief", "query": "again", "status": "error"})
        + "\n"
    )
    (tmp_path / "web_fetch.jsonl").write_text(
        json.dumps({"role": "chief", "url": "http://127.0.0.1/", "status": "refused: loopback address"})
        + "\n"
    )
    calls = we.calls_from_logs(tmp_path)
    assert [(c["tool"], c["arg"], c["ok"]) for c in calls] == [
        ("web_search", "news", True),
        ("web_search", "again", False),
        ("web_fetch", "http://127.0.0.1/", False),
    ]
    assert we.calls_from_logs(tmp_path / "missing") == []


def test_a_team_runs_trace_gives_its_web_results_and_arguments(tmp_path: Path) -> None:
    step = tmp_path / "trace" / "ask"
    step.mkdir(parents=True)
    (step / "step-001.json").write_text(
        json.dumps(
            {
                "u_t": {"name": "web_search", "arguments": json.dumps({"query": "q"})},
                "o_t": "[untrusted web text]\nhttps://n.example/1",
            }
        )
    )
    (step / "step-002.json").write_text(
        json.dumps({"u_t": {"name": "read_file", "arguments": "{}"}, "o_t": "https://ignored.example/"})
    )
    outputs, args = we.trace_results(tmp_path)
    assert outputs == ["[untrusted web text]\nhttps://n.example/1"] and args == ["q"]


def test_the_report_lists_each_role_with_its_calls_answer_and_grounding() -> None:
    from datetime import datetime

    text = we.render([result(), result(role="ux", calls=[])], None, datetime(2026, 10, 18, 9, 0))
    assert "| chief | 1 | 1 | 1 | PASS |" in text and "| ux | 0 | 1 | 1 | FAIL |" in text
    assert "`web_search` 'x'" in text and "> Story at https://news.example/a" in text


def test_the_next_minor_version_is_one_past_the_latest() -> None:
    assert we.next_minor("0.5.0") == "0.6.0" and we.next_minor("1.9.3") == "1.10.0"
