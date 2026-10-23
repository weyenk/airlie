"""The contradiction check (spec 2026-10-22-prompt-hub-design, "Contradiction check"), on synthetic texts.

`PromptText`s are built directly and role prompts are written into `tmp_path`, so none of this reads the hub:
the repository's own prompts are checked by `test_prompt_contradictions.py`."""

from __future__ import annotations

from pathlib import Path

import pytest

from airlie.prompts import Kind, PromptError, PromptText
from airlie.prompts.check import Conflict, conflicts, idle_yields, role_tags, subjects
from airlie.skills.library import split_frontmatter


def _text(
    id: str,
    governs: dict[str, str],
    *,
    kind: Kind = "rule",
    roles: tuple[str, ...] = ("coder",),
    yields_to: tuple[str, ...] = (),
) -> PromptText:
    return PromptText(
        id=id,
        kind=kind,
        roles=roles,
        fields=(),
        when="the model stalls" if kind == "steering" else None,
        governs=governs,
        yields_to=yields_to,
        why="because",
        body="text",
        path=f"{'rules' if kind == 'rule' else 'steering'}/{id}.md",
    )


def _role_prompt(tmp_path: Path, name: str, *tags: str) -> Path:
    """`tmp_path/<name>.md` with flat front matter: a summary, then each of `tags` as its own line."""
    path = tmp_path / f"{name}.md"
    front = "\n".join(["summary: the role", *tags])
    path.write_text(f"---\n{front}\n---\n# {name}\n")
    return path


def test_two_rules_that_disagree_conflict(tmp_path: Path) -> None:
    coder = _role_prompt(tmp_path, "coder", "governs.tests.location: named-file")
    rule = _text("x", {"tests.location": "new-file"})
    assert conflicts([rule], {"coder": coder}) == [
        Conflict(
            role="coder",
            subject="tests.location",
            a="role:coder",
            a_value="named-file",
            b="x",
            b_value="new-file",
        )
    ]


def test_two_rules_that_agree_do_not_conflict(tmp_path: Path) -> None:
    coder = _role_prompt(tmp_path, "coder", "governs.tests.location: new-file")
    assert conflicts([_text("x", {"tests.location": "new-file"})], {"coder": coder}) == []


def test_rules_on_different_subjects_do_not_conflict(tmp_path: Path) -> None:
    coder = _role_prompt(tmp_path, "coder", "governs.tests.location: named-file")
    assert conflicts([_text("x", {"reading": "first"})], {"coder": coder}) == []


def test_yields_to_settles_it(tmp_path: Path) -> None:
    coder = _role_prompt(tmp_path, "coder", "governs.tests.location: named-file")
    rule = _text("x", {"tests.location": "new-file"}, yields_to=("role:coder",))
    assert conflicts([rule], {"coder": coder}) == []


def test_a_role_prompt_can_be_the_one_that_yields(tmp_path: Path) -> None:
    coder = _role_prompt(tmp_path, "coder", "governs.tests.location: new-file", "yields_to: x")
    rule = _text("x", {"tests.location": "named-file"})
    assert conflicts([rule], {"coder": coder}) == []


def test_yielding_to_a_third_text_settles_nothing(tmp_path: Path) -> None:
    coder = _role_prompt(tmp_path, "coder", "governs.tests.location: named-file")
    rule = _text("x", {"tests.location": "new-file"}, yields_to=("y",))
    assert len(conflicts([rule], {"coder": coder})) == 1


def test_a_yields_to_that_settles_a_disagreement_is_not_idle(tmp_path: Path) -> None:
    coder = _role_prompt(tmp_path, "coder", "governs.tests.location: new-file", "yields_to: x")
    rule = _text("x", {"tests.location": "named-file"})
    assert idle_yields([rule], {"coder": coder}) == []


def test_yields_to_texts_that_never_meet_is_idle() -> None:
    a = _text("a", {"reading": "named-only"}, kind="steering", roles=("architect",), yields_to=("b",))
    b = _text("b", {"reading": "stop"}, kind="steering", roles=("coder",))
    assert idle_yields([a, b], {}) == [("a", "b")]


def test_yields_to_a_text_that_agrees_is_idle() -> None:
    a = _text("a", {"reading": "stop"}, yields_to=("b",))
    b = _text("b", {"reading": "stop", "writing": "now"})
    assert idle_yields([a, b], {}) == [("a", "b")]


def test_yields_to_across_kinds_is_idle() -> None:
    """Steering is never compared with a rule, so a steering text yielding to a rule settles nothing."""
    a = _text("a", {"reading": "stop"}, kind="steering", yields_to=("b",))
    b = _text("b", {"reading": "first"})
    assert idle_yields([a, b], {}) == [("a", "b")]


def test_yields_to_an_unknown_source_is_idle(tmp_path: Path) -> None:
    coder = _role_prompt(tmp_path, "coder", "governs.reading: first", "yields_to: nowhere")
    assert idle_yields([], {"coder": coder}) == [("role:coder", "nowhere")]


def test_steering_never_conflicts_with_a_rule() -> None:
    rule = _text("read-first", {"reading": "first"})
    steer = _text("stop-reading", {"reading": "stop"}, kind="steering")
    assert conflicts([rule, steer], {}) == []


def test_steering_never_conflicts_with_a_role_prompt(tmp_path: Path) -> None:
    coder = _role_prompt(tmp_path, "coder", "governs.reading: first")
    steer = _text("stop-reading", {"reading": "stop"}, kind="steering")
    assert conflicts([steer], {"coder": coder}) == []


def test_two_steering_texts_that_disagree_conflict() -> None:
    a = _text("keep-reading", {"reading": "first"}, kind="steering")
    b = _text("stop-reading", {"reading": "stop"}, kind="steering")
    assert conflicts([b, a], {}) == [
        Conflict(
            role="coder",
            subject="reading",
            a="keep-reading",
            a_value="first",
            b="stop-reading",
            b_value="stop",
        )
    ]


def test_steering_yields_to_steering() -> None:
    a = _text("keep-reading", {"reading": "first"}, kind="steering", yields_to=("stop-reading",))
    b = _text("stop-reading", {"reading": "stop"}, kind="steering")
    assert conflicts([a, b], {}) == []


def test_any_reaches_every_role_in_role_prompts(tmp_path: Path) -> None:
    coder = _role_prompt(tmp_path, "coder", "governs.tests.location: named-file")
    tester = _role_prompt(tmp_path, "tester", "governs.tests.location: named-file")
    docs = _role_prompt(tmp_path, "docs", "governs.tests.location: new-file")
    rule = _text("x", {"tests.location": "new-file"}, roles=("any",))
    found = conflicts([rule], {"coder": coder, "tester": tester, "docs": docs})
    assert [(c.role, c.a, c.b) for c in found] == [
        ("coder", "role:coder", "x"),
        ("tester", "role:tester", "x"),
    ]


def test_any_reaches_a_role_that_only_other_texts_name() -> None:
    a = _text("a", {"reading": "first"}, roles=("any",))
    b = _text("b", {"reading": "stop"}, roles=("reviewer",))
    assert [c.role for c in conflicts([a, b], {})] == ["reviewer"]


def test_texts_for_different_roles_do_not_conflict(tmp_path: Path) -> None:
    coder = _role_prompt(tmp_path, "coder", "governs.tests.location: named-file")
    rule = _text("x", {"tests.location": "new-file"}, roles=("reviewer",))
    assert conflicts([rule], {"coder": coder}) == []


def test_a_role_prompt_reaches_only_its_own_role(tmp_path: Path) -> None:
    coder = _role_prompt(tmp_path, "coder", "governs.tests.location: named-file")
    tester = _role_prompt(tmp_path, "tester", "governs.tests.location: new-file")
    assert conflicts([], {"coder": coder, "tester": tester}) == []


def test_a_text_for_two_roles_conflicts_once_in_each() -> None:
    a = _text("a", {"reading": "first"}, roles=("coder", "tester"))
    b = _text("b", {"reading": "stop"}, roles=("tester", "coder"))
    assert [(c.role, c.a, c.b) for c in conflicts([a, b], {})] == [("coder", "a", "b"), ("tester", "a", "b")]


def test_conflicts_are_sorted_by_role_subject_then_source(tmp_path: Path) -> None:
    coder = _role_prompt(tmp_path, "coder", "governs.b.subject: 1", "governs.a.subject: 1")
    z = _text("z", {"b.subject": "2", "a.subject": "2"})
    m = _text("m", {"a.subject": "3"})
    found = conflicts([z, m], {"coder": coder})
    assert [(c.subject, c.a, c.b) for c in found] == [
        ("a.subject", "m", "role:coder"),
        ("a.subject", "m", "z"),
        ("a.subject", "role:coder", "z"),
        ("b.subject", "role:coder", "z"),
    ]


def test_role_tags_reads_flat_governs_keys_and_keeps_the_summary(tmp_path: Path) -> None:
    path = _role_prompt(
        tmp_path, "coder", "governs.tests.location: new-file", "yields_to: brief-named-test-file"
    )
    text = path.read_text().replace("summary: the role", "summary: a: b")
    path.write_text(text)
    assert role_tags(path) == ({"tests.location": "new-file"}, ("brief-named-test-file",))
    assert split_frontmatter(text)[0]["summary"] == "a: b"


def test_role_tags_splits_yields_to_on_commas(tmp_path: Path) -> None:
    path = _role_prompt(tmp_path, "coder", "yields_to: a, role:tester ,b,")
    assert role_tags(path) == ({}, ("a", "role:tester", "b"))


def test_role_tags_of_an_untagged_prompt_are_empty(tmp_path: Path) -> None:
    path = tmp_path / "plain.md"
    path.write_text("# no front matter\n")
    assert role_tags(path) == ({}, ())


def test_role_tags_rejects_a_governs_key_with_no_subject(tmp_path: Path) -> None:
    path = _role_prompt(tmp_path, "coder", "governs: tests.location=new-file")
    with pytest.raises(PromptError, match="coder.md.*governs.<subject>"):
        role_tags(path)


def test_subjects_lists_each_subject_with_its_sources(tmp_path: Path) -> None:
    coder = _role_prompt(tmp_path, "coder", "governs.tests.location: named-file", "governs.edits: exact")
    reviewer = _role_prompt(tmp_path, "reviewer", "governs.verdict: json")
    texts = [
        _text("write-now", {"edits": "now", "test.location": "new-file"}, kind="steering"),
        _text("brief", {"tests.location": "new-file"}, roles=("any",)),
        _text("silent", {}),
    ]
    assert subjects(texts, {"reviewer": reviewer, "coder": coder}) == {
        "edits": ["role:coder", "write-now"],
        "test.location": ["write-now"],
        "tests.location": ["brief", "role:coder"],
        "verdict": ["role:reviewer"],
    }
    assert list(subjects(texts, {"reviewer": reviewer, "coder": coder})) == [
        "edits",
        "test.location",
        "tests.location",
        "verdict",
    ]
